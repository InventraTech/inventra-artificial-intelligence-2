from dataclasses import replace
from threading import Barrier

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from iai.app.core.assistant import judge
from iai.app.core.assistant.tool_gate import JevToolGate, live_tool_scope, protect_tool
from iai.app.core.infra.security import context, current_context
from iai.app.extras.demo import plan_demo_reads
from tests_helpers import context_for, jev_evaluation


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name, args, identifier):
    return {"name": name, "args": args, "id": identifier}


def agent(responses, tools):
    return create_agent(
        ScriptedModel(responses=responses),
        tools=tools,
        middleware=[JevToolGate("estoquista", tools)],
    )


def run(app, token="demo-estoquista"):
    ctx = replace(
        context_for(token), turn_id="parallel-turn", session_id="parallel-session"
    )
    marker = current_context.set(ctx)
    try:
        with live_tool_scope():
            return app.invoke(
                {
                    "messages": [
                        HumanMessage(content="Mostre o estoque e explique as regras")
                    ]
                },
                config={"max_concurrency": 4},
            )
    finally:
        current_context.reset(marker)


def test_live_fanout_overlaps_tools_and_fanin_preserves_context():
    tool_barrier = Barrier(2)
    observed = []

    def record(name):
        ctx = context()
        observed.append(
            (
                name,
                ctx.identity.user_id,
                ctx.identity.kitchen_id,
                ctx.token.get_secret_value(),
                ctx.session_id,
                ctx.turn_id,
            )
        )
        tool_barrier.wait(timeout=5)

    @tool
    def consultar_inventra(resource: str) -> str:
        """Consulta estoque autorizado."""
        record("operacional")
        return '{"ok":true,"saldo":12}'

    @tool
    def faq_retriever(question: str) -> str:
        """Explica regras documentadas."""
        record("faq")
        return "Regra documental confirmada"

    tools = [protect_tool(t) for t in (consultar_inventra, faq_retriever)]
    app = agent(
        [
            AIMessage(
                content="",
                tool_calls=[
                    call("consultar_inventra", {"resource": "estoque"}, "read"),
                    call("faq_retriever", {"question": "estoque mínimo"}, "faq"),
                ],
            ),
            AIMessage(content="Saldo e regra recebidos"),
        ],
        tools,
    )
    result = run(app)
    # As duas leituras só passam da barreira se rodarem ao mesmo tempo.
    assert len(observed) == 2
    assert all(
        v[1:]
        == (
            "demo-estoquista",
            1,
            "demo-estoquista",
            "parallel-session",
            "parallel-turn",
        )
        for v in observed
    )
    assert {m.name for m in result["messages"] if m.type == "tool"} == {
        "consultar_inventra",
        "faq_retriever",
    }
    assert result["messages"][-1].content == "Saldo e regra recebidos"
    assert len(result["jev_calls"]) == 2
    assert all(c["status"] == "EXECUTED" for c in result["jev_calls"].values())
    assert result["jev_parallel"] is True
    assert all(c["validacao_local"] is True for c in result["jev_calls"].values())


@pytest.mark.parametrize("essential_fails", [False, True])
def test_partial_source_failure_preserves_the_successful_read(essential_fails):
    invoked = []
    failed = "consultar_inventra" if essential_fails else "faq_retriever"

    @tool
    def consultar_inventra(resource: str) -> str:
        """Consulta estoque."""
        if failed == "consultar_inventra":
            raise RuntimeError("serviço fora do ar")
        invoked.append("consultar_inventra")
        return '{"ok":true,"saldo":12}'

    @tool
    def faq_retriever(question: str) -> str:
        """Consulta FAQ."""
        if failed == "faq_retriever":
            raise RuntimeError("serviço fora do ar")
        invoked.append("faq_retriever")
        return "FAQ disponível"

    final = (
        "Não foi possível confirmar estoque; FAQ disponível"
        if essential_fails
        else "Saldo confirmado; FAQ indisponível"
    )
    result = run(
        agent(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        call("consultar_inventra", {"resource": "estoque"}, "read"),
                        call("faq_retriever", {"question": "regra"}, "faq"),
                    ],
                ),
                AIMessage(content=final),
            ],
            [protect_tool(t) for t in (consultar_inventra, faq_retriever)],
        )
    )
    assert failed not in invoked and len(invoked) == 1
    assert result["messages"][-1].content == final
    assert not result.get("jev_halt")
    assert {c["status"] for c in result["jev_calls"].values()} == {"EXECUTED", "FAILED"}


def test_mixed_proposal_batch_is_sequenced_before_any_mutation(monkeypatch):
    from iai.app.core.infra.storage import get_db
    from iai.app.core.inventra.domain import Operation
    from iai.app.core.tools.operational import PROPOSAL_TOOLS, consultar_inventra

    def evaluate(state):
        return jev_evaluation(fields=state["tool"]["schema"].get("properties", {}))

    monkeypatch.setattr(judge, "evaluate", evaluate)
    result = run(
        agent(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        call("consultar_inventra", {"resource": "lotes"}, "lookup"),
                        call(
                            "propor_registrar_saida",
                            {"batchId": 1, "quantity": 2, "justification": "uso"},
                            "premature",
                        ),
                    ],
                ),
                AIMessage(content="Confirme a quantidade antes de propor"),
            ],
            [consultar_inventra, PROPOSAL_TOOLS[Operation.CONSUME]],
        )
    )
    assert len(result["jev_calls"]) == 1
    assert result["jev_parallel"] is False
    assert get_db().approvals.count_documents({}) == 0
    assert get_db().execution_mock.count_documents({}) == 0


def test_simple_demo_plan_has_one_source_and_composite_plan_fans_out():
    simple = plan_demo_reads("Quanto arroz temos em estoque?")
    assert len(simple) == 1 and simple[0]["name"] == "consultar_inventra"
    combined = plan_demo_reads(
        "Mostre o estoque e explique como funciona o estoque mínimo"
    )
    assert {c["name"] for c in combined} == {"consultar_inventra", "faq_retriever"}
    history = plan_demo_reads("Mostre o estoque e o que falamos na conversa anterior")
    assert {c["name"] for c in history} == {"consultar_inventra", "buscar_historico"}


def test_simultaneous_requests_do_not_mix_contexts(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    barrier = Barrier(2)

    @tool
    def consultar_inventra(resource: str) -> str:
        """Retorna identidade autorizada sem token."""
        barrier.wait(timeout=5)
        return context().identity.user_id

    monkeypatch.setattr(
        judge, "evaluate", lambda state: jev_evaluation(fields=["resource"])
    )
    protect_tool(consultar_inventra)

    # Cada invocação usa um modelo roteirizado próprio, como o estado independente dos agentes reais.
    def invoke(token):
        own = agent(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        call("consultar_inventra", {"resource": "estoque"}, "one")
                    ],
                ),
                AIMessage(content="ok"),
            ],
            [consultar_inventra],
        )
        result = run(own, token)
        return [m.content for m in result["messages"] if m.type == "tool"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, ["demo-estoquista", "demo-outra-cozinha"]))
    assert results == [["demo-estoquista"], ["demo-outra-cozinha"]]
