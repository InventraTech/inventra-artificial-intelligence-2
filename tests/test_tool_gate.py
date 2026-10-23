from dataclasses import replace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import tool

from iai.app.core.assistant import judge
from iai.app.core.assistant.tool_gate import JevToolGate, live_tool_scope, protect_tool
from iai.app.core.infra.security import current_context
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.domain import Operation
from iai.app.core.tools.faq import faq_retriever
from iai.app.core.tools.memoria import buscar_historico
from iai.app.core.tools.operational import PROPOSAL_TOOLS
from tests_helpers import context_for, jev_evaluation


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


def call(name="lookup", args=None, identifier="call-1"):
    return {
        "name": name,
        "args": args if args is not None else {"product": "arroz"},
        "id": identifier,
    }


def turn(*calls):
    return AIMessage(content="", tool_calls=list(calls))


@pytest.fixture
def authenticated():
    marker = current_context.set(replace(context_for(), turn_id="jev-test-turn"))
    with live_tool_scope():
        yield
    current_context.reset(marker)


@pytest.fixture
def lookup():
    invocations = []

    @tool
    def lookup(product: str) -> str:
        """Consulta um produto informado pelo usuário."""
        invocations.append(product)
        return "produto confirmado: " + product

    return protect_tool(lookup), invocations


def make_agent(responses, tools, name="estoquista"):
    return create_agent(
        ScriptedModel(responses=responses),
        tools=tools,
        middleware=[JevToolGate(name, tools)],
    )


def approve(monkeypatch):
    seen = []

    def evaluate(state):
        seen.append(state)
        assert state["candidate"]["status"] == "PENDING_JEV"
        return jev_evaluation(fields=state["tool"]["schema"].get("properties", {}))

    monkeypatch.setattr(judge, "evaluate", evaluate)
    return seen


def invoke(agent, text="Consulte o arroz"):
    return agent.invoke(
        {"messages": [HumanMessage(content=text)]}, config={"max_concurrency": 1}
    )


def test_pending_state_precedes_judgment_and_tool_executes_once(
    monkeypatch, authenticated, lookup
):
    lookup, executions = lookup
    seen_pending = []

    def evaluate(state):
        assert seen_pending and executions == []
        return jev_evaluation(fields=["product"])

    monkeypatch.setattr(judge, "evaluate", evaluate)
    agent = make_agent(
        [turn(call()), AIMessage(content="Produto localizado.")], [lookup]
    )
    for update in agent.stream(
        {"messages": [HumanMessage(content="Consulte arroz")]}, stream_mode="updates"
    ):
        for data in update.values():
            if isinstance(data, dict) and any(
                c["status"] == "PENDING_JEV" for c in data.get("jev_calls", {}).values()
            ):
                seen_pending.append(data)
    assert executions == ["arroz"]


def test_rejection_repairs_and_rejudges_before_execution(
    monkeypatch, authenticated, lookup
):
    lookup, executions = lookup
    calls = []

    def evaluate(state):
        calls.append(state)
        return (
            jev_evaluation(
                failed={"arguments_grounded": 0.1},
                route="agent_repair",
                fields=["product"],
            )
            if len(calls) == 1
            else jev_evaluation(fields=["product"])
        )

    monkeypatch.setattr(judge, "evaluate", evaluate)
    agent = make_agent(
        [
            turn(call(args={"product": "inventado"})),
            turn(call(identifier="corrected")),
            AIMessage(content="ok"),
        ],
        [lookup],
    )
    result = invoke(agent)
    assert executions == ["arroz"] and len(calls) == 2
    assert [c["status"] for c in result["jev_calls"].values()] == [
        "RETRY_SPECIALIST",
        "EXECUTED",
    ]
    assert [c["attempt"] for c in result["jev_calls"].values()] == [1, 2]


def test_three_rejections_never_execute(monkeypatch, authenticated, lookup):
    lookup, executions = lookup
    monkeypatch.setattr(
        judge,
        "evaluate",
        lambda state: jev_evaluation(
            failed={"tool_appropriateness": 0.1},
            route="agent_repair",
            fields=["product"],
        ),
    )
    result = invoke(
        make_agent([turn(call(identifier=str(i))) for i in range(3)], [lookup])
    )
    assert executions == []
    assert len(result["jev_calls"]) == 3 and result["jev_halt"]
    assert max(c["attempt"] for c in result["jev_calls"].values()) == 3


def test_missing_user_fact_asks_without_repair_or_execution(
    monkeypatch, authenticated, lookup
):
    lookup, executions = lookup
    monkeypatch.setattr(
        judge,
        "evaluate",
        lambda state: jev_evaluation(
            failed={"missing_user_information": 0.99},
            route="user_input",
            missing="product",
            fields=["product"],
        ),
    )
    result = invoke(make_agent([turn(call())], [lookup]))
    assert executions == [] and result["jev_halt"]
    assert "JEV" not in result["messages"][-1].content
    assert len(result["jev_calls"]) == 1


def test_schema_veto_cannot_be_overridden_by_jev(monkeypatch, authenticated, lookup):
    lookup, executions = lookup
    approve(monkeypatch)
    result = invoke(
        make_agent([turn(call(args={}, identifier=str(i))) for i in range(3)], [lookup])
    )
    assert executions == []
    assert all(
        c["failed_checks"] == ["SCHEMA_INVALID"] for c in result["jev_calls"].values()
    )


def test_unavailable_jev_does_not_retry_specialist_or_execute(
    monkeypatch, authenticated, lookup
):
    lookup, executions = lookup

    def broken(state):
        raise judge.JevUnavailable("Bearer private-server-secret")

    monkeypatch.setattr(judge, "evaluate", broken)
    result = invoke(make_agent([turn(call())], [lookup]))
    assert executions == [] and len(result["jev_calls"]) == 1
    assert "indisponível" in result["messages"][-1].content
    assert "private-server-secret" not in str(result)


def test_multiple_calls_each_require_approval(monkeypatch, authenticated, lookup):
    lookup, executions = lookup

    def evaluate(state):
        if state["tool"]["arguments"]["product"] == "carne":
            return jev_evaluation(failed={"intent_alignment": 0.1}, fields=["product"])
        return jev_evaluation(fields=["product"])

    monkeypatch.setattr(judge, "evaluate", evaluate)
    result = invoke(
        make_agent(
            [turn(call(), call(args={"product": "carne"}, identifier="second"))],
            [lookup],
        )
    )
    assert executions == ["arroz"] and len(result["jev_calls"]) == 2


def test_unknown_tool_is_rejected_before_evaluation(monkeypatch, authenticated, lookup):
    lookup, executions = lookup
    seen = approve(monkeypatch)
    result = invoke(make_agent([turn(call(name="tool_nao_ligada"))], [lookup]))
    assert executions == [] and seen == []
    assert next(iter(result["jev_calls"].values()))["failed_checks"] == [
        "TOOL_NOT_ALLOWED"
    ]


def test_approved_proposal_still_requires_human_approval(monkeypatch, authenticated):
    approve(monkeypatch)
    proposal = PROPOSAL_TOOLS[Operation.CONSUME]
    result = invoke(
        make_agent(
            [
                turn(
                    call(
                        proposal.name,
                        {
                            "batchId": 1,
                            "quantity": "10",
                            "justification": "Consumo informado",
                        },
                    )
                ),
                AIMessage(content="aguarda"),
            ],
            [proposal],
        ),
        "Usei 10 kg de arroz",
    )
    assert next(iter(result["jev_calls"].values()))["status"] == "EXECUTED"
    assert get_db().approvals.find_one({})["status"] == "AGUARDANDO_APROVACAO"
    assert get_db().execution_mock.count_documents({}) == 0


def test_role_veto_does_not_call_jev(monkeypatch, authenticated):
    marker = current_context.set(replace(context_for("demo-comprador"), turn_id="role"))
    seen = approve(monkeypatch)
    proposal = PROPOSAL_TOOLS[Operation.CONSUME]
    try:
        invoke(
            make_agent(
                [
                    turn(
                        call(
                            proposal.name,
                            {
                                "batchId": 1,
                                "quantity": "10",
                                "justification": "Consumo",
                            },
                        )
                    )
                ],
                [proposal],
            )
        )
    finally:
        current_context.reset(marker)
    assert seen == [] and get_db().approvals.count_documents({}) == 0


@pytest.mark.parametrize(
    "target,args",
    [
        (faq_retriever, {"question": "Como funciona o Inventra?"}),
        (buscar_historico, {"busca": "arroz"}),
    ],
)
def test_faq_and_history_also_pass_through_gate(
    monkeypatch, authenticated, target, args
):
    seen = approve(monkeypatch)
    result = invoke(
        make_agent([turn(call(target.name, args)), AIMessage(content="ok")], [target])
    )
    assert (
        len(seen) == 1
        and next(iter(result["jev_calls"].values()))["status"] == "EXECUTED"
    )


def test_direct_tool_call_is_not_a_live_bypass(authenticated, lookup):
    lookup, executions = lookup
    with pytest.raises(PermissionError):
        lookup.invoke({"product": "arroz"})
    assert executions == []


def test_credentials_and_pii_are_not_sent_to_jev(monkeypatch, authenticated, lookup):
    lookup, _ = lookup
    seen = approve(monkeypatch)
    result = invoke(
        make_agent([turn(call()), AIMessage(content="ok")], [lookup]),
        "Consulte arroz. Bearer TOPSECRET email pessoa@example.com senha=private",
    )
    assert (
        seen and "TOPSECRET" not in str(seen) and "pessoa@example.com" not in str(seen)
    )
    assert "demo-estoquista" not in str(seen)
    assert "TOPSECRET" not in str(result["jev_calls"])


def test_single_use_permission_blocks_executor_retry(
    monkeypatch, authenticated, lookup
):
    lookup, executions = lookup
    approve(monkeypatch)

    class RepeatingExecutor(JevToolGate):
        def wrap_tool_call(self, request, handler):
            def repeat(req):
                handler(req)
                return handler(req)

            return super().wrap_tool_call(request, repeat)

    agent = create_agent(
        ScriptedModel(responses=[turn(call())]),
        tools=[lookup],
        middleware=[RepeatingExecutor("estoquista", [lookup])],
    )
    result = invoke(agent)
    assert executions == ["arroz"]
    assert next(iter(result["jev_calls"].values()))["status"] == "FAILED"


def test_arguments_cannot_change_after_approval(monkeypatch, authenticated, lookup):
    lookup, executions = lookup
    approve(monkeypatch)

    class TamperingExecutor(JevToolGate):
        def wrap_tool_call(self, request, handler):
            def tamper(req):
                return handler(
                    req.override(
                        tool_call={**req.tool_call, "args": {"product": "carne"}}
                    )
                )

            return super().wrap_tool_call(request, tamper)

    agent = create_agent(
        ScriptedModel(responses=[turn(call())]),
        tools=[lookup],
        middleware=[TamperingExecutor("estoquista", [lookup])],
    )
    result = invoke(agent)
    assert executions == []
    assert next(iter(result["jev_calls"].values()))["status"] == "FAILED"


def test_anonymization_preserves_distinct_values(authenticated):
    from iai.app.core.assistant.tool_gate import sanitized

    result = sanitized(
        {
            "user": "um@example.com",
            "candidate": "outro@example.com",
            "same": "um@example.com",
        }
    )
    assert result["user"] == result["same"]
    assert result["user"] != result["candidate"]
    assert "example.com" not in str(result)


def test_all_live_agents_have_the_gate():
    from iai.app.core.assistant import graph

    for agent in (
        graph.router_app,
        graph.estoquista_app,
        graph.comprador_app,
        graph.supervisor_app,
        graph.faq_app,
    ):
        assert any(
            "JevToolGate.after_model" in name for name in agent.get_graph().nodes
        )


def test_known_opaque_keys_are_removed_before_evaluation(
    monkeypatch, authenticated, lookup
):
    from pydantic import SecretStr

    from iai.app.core.assistant import tool_gate

    lookup, _ = lookup
    monkeypatch.setattr(
        tool_gate, "TYPESAFE_API_KEY", SecretStr("private-typesafe-key")
    )
    seen = approve(monkeypatch)
    invoke(
        make_agent([turn(call()), AIMessage(content="ok")], [lookup]),
        "Consulte arroz; private-typesafe-key",
    )
    assert seen and "private-typesafe-key" not in str(seen)


def test_tool_reported_error_is_not_a_successful_execution(monkeypatch, authenticated):
    @tool
    def unavailable_lookup(product: str) -> str:
        """Consulta o produto; neste teste o serviço de negócio está indisponível."""
        return '{"ok":false,"code":"SERVICO_INDISPONIVEL"}'

    protect_tool(unavailable_lookup)
    approve(monkeypatch)
    agent = make_agent(
        [turn(call("unavailable_lookup")), AIMessage(content="Serviço indisponível")],
        [unavailable_lookup],
    )
    result = invoke(agent)
    record = next(iter(result["jev_calls"].values()))
    assert record["decision"] == "APPROVE" and record["status"] == "FAILED"
