"""Grafo e agentes reais do LangChain com LLM roteirizado. Não avalia provedor real."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from iai.app.core.assistant import graph, judge, tool_gate
from iai.app.core.assistant.tool_gate import JevToolGate
from iai.app.core.infra.security import current_context
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.domain import Operation
from iai.app.core.tools.operational import PROPOSAL_TOOLS, consultar_inventra
from tests_helpers import context_for, jev_evaluation


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.mark.parametrize("partial", [False, True])
def test_live_graph_executes_tools_and_proposes_without_executing(monkeypatch, partial):
    responses = [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "read",
                    "name": "consultar_inventra",
                    "args": {"resource": "lotes", "product_id": 1},
                }
            ],
        ),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "proposal",
                    "name": "propor_registrar_saida",
                    "args": {
                        "batchId": 1,
                        "quantity": 10,
                        "justification": "Consumo informado",
                    },
                }
            ],
        ),
        AIMessage(content="Pedido validado; nada foi executado."),
    ]
    if partial:
        responses.insert(
            -1,
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "bad-proposal",
                        "name": "propor_registrar_saida",
                        "args": {
                            "batchId": 1,
                            "quantity": 1000,
                            "justification": "Pedido sem saldo",
                        },
                    }
                ],
            ),
        )
    agent = create_agent(
        ScriptedModel(responses=responses),
        tools=[consultar_inventra, PROPOSAL_TOOLS[Operation.CONSUME]],
        middleware=[
            JevToolGate(
                "estoquista", [consultar_inventra, PROPOSAL_TOOLS[Operation.CONSUME]]
            )
        ],
    )
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "estoquista_app", agent)
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {
                "messages": [AIMessage(content="ROUTE=estoquista")]
            }
        ),
    )
    monkeypatch.setattr(
        judge,
        "evaluate",
        lambda state: jev_evaluation(
            fields=state["tool"]["schema"].get("properties", {})
        ),
    )
    monkeypatch.setattr(
        graph,
        "orquestrador_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: AIMessage(content="Nada foi executado.")
        ),
    )
    marker = current_context.set(replace(context_for(), turn_id="live-turn"))
    try:
        answer = graph.executar_fluxo_assessor(
            "Depois do almoço, usei dez quilos de arroz.", "live", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "Nada foi alterado" in answer
    if partial:
        assert "Não consegui concluir" in answer and "saldo insuficiente" in answer
    assert get_db().execution_mock.count_documents({}) == 0
    assert (
        get_db().business_mock.find_one({"_id": "1:batch:1"})["data"]["currentQuantity"]
        == 50
    )


def test_formatter_cannot_change_confirmed_numbers(monkeypatch):
    from langchain_core.messages import HumanMessage, ToolMessage

    monkeypatch.setattr(
        graph,
        "orquestrador_app",
        SimpleNamespace(invoke=lambda *a, **kw: AIMessage(content="Temos 500 kg.")),
    )
    response = graph.no_orquestrador(
        {
            "messages": [
                HumanMessage(content="Saldo?"),
                ToolMessage(content="50 kg", tool_call_id="stock"),
                AIMessage(content="Temos 50 kg."),
            ],
            "rota": "estoquista",
        }
    )
    assert response["messages"][-1]["content"] == "Temos 50 kg."


def test_router_cannot_bypass_tools_when_protocol_is_missing(monkeypatch):
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {
                "messages": [
                    HumanMessage(content="Meu saldo?"),
                    AIMessage(content="Temos 500 kg."),
                ]
            }
        ),
    )
    result = graph.no_roteador({"messages": [HumanMessage(content="Meu saldo?")]}, {})
    assert result["rota"] == "supervisor"
    assert "messages" not in result


def test_provider_exception_does_not_expose_credentials(monkeypatch):
    from fastapi import HTTPException

    monkeypatch.setattr(graph, "LLM_MODE", "live")

    def broken(*a, **kwargs):
        raise RuntimeError("Bearer private-secret-from-sdk")

    monkeypatch.setattr(graph, "fluxo_agentes", SimpleNamespace(invoke=broken))
    marker = current_context.set(replace(context_for(), turn_id="failed-provider"))
    try:
        with pytest.raises(HTTPException) as error:
            graph.executar_fluxo_assessor("Saldo?", "failed", "demo-estoquista")
        assert error.value.status_code == 502 and "private-secret" not in str(
            error.value.detail
        )
        metric = get_db().metrics.find_one({})
        assert metric["status"] == "erro" and metric["error_type"] == "RuntimeError"
        assert "private-secret" not in str(metric)
    finally:
        current_context.reset(marker)


def test_partial_proposal_preserves_user_question_without_exposing_jev(monkeypatch):
    proposal = PROPOSAL_TOOLS[Operation.CONSUME]
    agent = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "rice",
                            "name": proposal.name,
                            "args": {
                                "batchId": 1,
                                "quantity": "10",
                                "justification": "Consumo informado",
                            },
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "meat",
                            "name": proposal.name,
                            "args": {
                                "batchId": 2,
                                "quantity": "40",
                                "justification": "Quantidade não informada",
                            },
                        }
                    ],
                ),
            ]
        ),
        tools=[proposal],
        middleware=[JevToolGate("estoquista", [proposal])],
    )

    def evaluate(state):
        fields = state["tool"]["schema"]["properties"]
        if state["tool"]["arguments"]["batchId"] == 2:
            return jev_evaluation(
                failed={"missing_user_information": 0.99},
                route="user_input",
                missing="quantity",
                fields=fields,
            )
        return jev_evaluation(fields=fields)

    monkeypatch.setattr(judge, "evaluate", evaluate)
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "estoquista_app", agent)
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {
                "messages": [AIMessage(content="ROUTE=estoquista")]
            }
        ),
    )
    marker = current_context.set(replace(context_for(), turn_id="partial-question"))
    try:
        answer = graph.executar_fluxo_assessor(
            "Usei 10 kg de arroz e carne.", "partial-question", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "Nada foi alterado" in answer and "quantidade e a unidade" in answer
    assert "JEV" not in answer and "RETRY_SPECIALIST" not in answer
    assert get_db().execution_mock.count_documents({}) == 0


def test_live_missing_typesafe_key_blocks_tool(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(tool_gate, "JEV_ESCOPO", "todas")
    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr(""))
    agent = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "blocked",
                            "name": "consultar_inventra",
                            "args": {"resource": "lotes"},
                        }
                    ],
                )
            ]
        ),
        tools=[consultar_inventra],
        middleware=[JevToolGate("estoquista", [consultar_inventra])],
    )
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "estoquista_app", agent)
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {
                "messages": [AIMessage(content="ROUTE=estoquista")]
            }
        ),
    )
    marker = current_context.set(replace(context_for(), turn_id="missing-jev"))
    try:
        answer = graph.executar_fluxo_assessor(
            "Mostre os lotes.", "missing-jev", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "indisponível" in answer and "JEV" not in answer
    assert get_db().business_mock.count_documents({}) == 0
    metric = get_db().metrics.find_one({})
    assert metric["status"] == "erro" and metric["jev"][0]["status"] == "REJECT"
    assert "arguments" not in str(metric["jev"])


def test_live_direct_call_cannot_bypass_middleware(monkeypatch):
    from fastapi import HTTPException

    def unprotected_graph(*args, **kwargs):
        consultar_inventra.invoke({"resource": "lotes"})

    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(
        graph, "fluxo_agentes", SimpleNamespace(invoke=unprotected_graph)
    )
    marker = current_context.set(replace(context_for(), turn_id="bypass"))
    try:
        with pytest.raises(HTTPException) as error:
            graph.executar_fluxo_assessor("Lotes?", "bypass", "demo-estoquista")
    finally:
        current_context.reset(marker)
    assert error.value.status_code == 502
    assert get_db().business_mock.count_documents({}) == 0


def test_pergunta_de_faq_percorre_o_grafo_e_responde_sem_jev(monkeypatch):
    """Roteador -> FAQ (busca em código) -> resposta; nada de tool-calling nem de JEV."""
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {"messages": [AIMessage(content="ROUTE=faq")]}
        ),
    )

    def sem_jev(state):
        raise AssertionError("o FAQ não deve passar pelo JEV")

    monkeypatch.setattr(judge, "evaluate", sem_jev)
    monkeypatch.setattr(
        graph,
        "llm_especialista",
        SimpleNamespace(
            invoke=lambda *a, **kw: AIMessage(content="O OCR lê a foto do produto.")
        ),
    )
    marker = current_context.set(replace(context_for(), turn_id="faq-turn"))
    try:
        answer = graph.executar_fluxo_assessor("O que é o OCR?", "faq", "demo-estoquista")
    finally:
        current_context.reset(marker)
    assert "O OCR lê a foto do produto." in answer
    assert "JEV" not in answer and "confirmar" not in answer
