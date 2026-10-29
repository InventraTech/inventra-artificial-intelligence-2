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
from tests_helpers import context_for


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


@pytest.fixture(autouse=True)
def sem_jev(monkeypatch):
    """O gate valida localmente; o JEV só entra depois do especialista (revisao.py)."""

    def proibido(*args, **kwargs):
        pytest.fail("O gate não deve chamar o JEV.")

    monkeypatch.setattr(judge, "evaluate", proibido)
    monkeypatch.setattr(judge, "consultar", proibido)


def invoke(agent, text="Consulte o arroz"):
    return agent.invoke(
        {"messages": [HumanMessage(content=text)]}, config={"max_concurrency": 1}
    )


def test_pending_state_precedes_validation_and_tool_executes_once(
    authenticated, lookup
):
    lookup, executions = lookup
    seen_pending = []
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
    assert seen_pending and executions == ["arroz"]


def test_valid_read_runs_with_local_validation_only(authenticated, lookup):
    lookup, executions = lookup
    result = invoke(make_agent([turn(call()), AIMessage(content="ok")], [lookup]))
    record = next(iter(result["jev_calls"].values()))
    assert executions == ["arroz"]
    assert record["status"] == "EXECUTED" and record["validacao_local"] is True


def test_gate_does_not_depend_on_the_jev_key(monkeypatch, authenticated, lookup):
    from pydantic import SecretStr

    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr(""))
    lookup, executions = lookup
    invoke(make_agent([turn(call()), AIMessage(content="ok")], [lookup]))
    assert executions == ["arroz"]


@pytest.mark.parametrize("args", [{}, {"product": "arroz", "undeclared": True}])
def test_invalid_schema_is_repaired_up_to_the_limit_and_never_executes(
    authenticated, lookup, args
):
    lookup, executions = lookup
    result = invoke(
        make_agent(
            [
                turn(call(args=args, identifier=str(i)))
                for i in range(judge.MAX_ATTEMPTS)
            ],
            [lookup],
        )
    )
    assert executions == []
    assert all(
        record["failed_checks"] == ["SCHEMA_INVALID"]
        for record in result["jev_calls"].values()
    )
    assert list(result["jev_calls"].values())[-1]["decision"] == "REJECT"
    assert result["jev_halt"]


def test_secret_in_arguments_is_blocked(authenticated, lookup):
    lookup, executions = lookup
    result = invoke(
        make_agent(
            [turn(call(args={"product": tool_gate_token()}))],
            [lookup],
        )
    )
    assert executions == []
    record = next(iter(result["jev_calls"].values()))
    assert record["failed_checks"] == ["SECRET_IN_ARGUMENTS"]


def tool_gate_token():
    from iai.app.core.assistant import tool_gate

    return tool_gate.context().token.get_secret_value()


def test_multiple_valid_calls_all_execute(authenticated, lookup):
    lookup, executions = lookup
    result = invoke(
        make_agent(
            [
                turn(call(), call(args={"product": "carne"}, identifier="second")),
                AIMessage(content="ok"),
            ],
            [lookup],
        )
    )
    assert sorted(executions) == ["arroz", "carne"] and len(result["jev_calls"]) == 2


def test_unknown_tool_is_rejected(authenticated, lookup):
    lookup, executions = lookup
    result = invoke(make_agent([turn(call(name="tool_nao_ligada"))], [lookup]))
    assert executions == []
    assert next(iter(result["jev_calls"].values()))["failed_checks"] == [
        "TOOL_NOT_ALLOWED"
    ]


def test_proposal_only_registers_pending_and_never_executes(authenticated):
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
    assert get_db().execution_mock.count_documents({}) == 0
    assert get_db().approvals.count_documents({"status": "AGUARDANDO_APROVACAO"}) == 1


def test_role_veto_blocks_the_proposal():
    marker = current_context.set(replace(context_for("demo-comprador"), turn_id="role"))
    proposal = PROPOSAL_TOOLS[Operation.CONSUME]
    try:
        result = invoke(
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
    assert get_db().execution_mock.count_documents({}) == 0
    assert get_db().approvals.count_documents({}) == 0
    assert next(iter(result["jev_calls"].values()))["failed_checks"] == [
        "ROLE_NOT_ALLOWED"
    ]


@pytest.mark.parametrize(
    "target,args",
    [
        (faq_retriever, {"question": "Como funciona o Inventra?"}),
        (buscar_historico, {"busca": "arroz"}),
    ],
)
def test_faq_and_history_also_pass_through_gate(authenticated, target, args):
    result = invoke(
        make_agent([turn(call(target.name, args)), AIMessage(content="ok")], [target])
    )
    assert next(iter(result["jev_calls"].values()))["status"] == "EXECUTED"


def test_direct_tool_call_is_not_a_live_bypass(authenticated, lookup):
    lookup, executions = lookup
    with pytest.raises(PermissionError):
        lookup.invoke({"product": "arroz"})
    assert executions == []


def test_credentials_and_pii_are_not_kept_in_the_gate_records(authenticated, lookup):
    lookup, _ = lookup
    result = invoke(
        make_agent([turn(call()), AIMessage(content="ok")], [lookup]),
        "Consulte arroz. Bearer TOPSECRET email pessoa@example.com senha=private",
    )
    assert "TOPSECRET" not in str(result["jev_calls"])
    assert "pessoa@example.com" not in str(result["jev_calls"])


def test_single_use_permission_blocks_executor_retry(authenticated, lookup):
    lookup, executions = lookup

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


def test_arguments_cannot_change_after_validation(authenticated, lookup):
    lookup, executions = lookup

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


def test_tool_reported_error_is_not_a_successful_execution(authenticated):
    @tool
    def unavailable_lookup(product: str) -> str:
        """Consulta o produto; neste teste o serviço de negócio está indisponível."""
        return '{"ok":false,"code":"SERVICO_INDISPONIVEL"}'

    protect_tool(unavailable_lookup)
    agent = make_agent(
        [turn(call("unavailable_lookup")), AIMessage(content="Serviço indisponível")],
        [unavailable_lookup],
    )
    result = invoke(agent)
    record = next(iter(result["jev_calls"].values()))
    assert record["decision"] == "APPROVE" and record["status"] == "FAILED"
