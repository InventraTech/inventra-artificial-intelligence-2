"""Grafo e agentes reais do LangChain com LLM roteirizado. Não avalia provedor real."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from iai.app import graph, judge
from iai.app.domain import Operation
from iai.app.schemas import ResultadoJuiz
from iai.app.security import current_context
from iai.app.storage import get_db
from iai.app.tools.operational import PROPOSAL_TOOLS, consultar_inventra
from tests_helpers import context_for


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
        AIMessage(content="Proposta criada, aguardando aprovação."),
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
        middleware=[judge.jev_antes_da_tool],
    )
    monkeypatch.setattr(
        judge,
        "avaliar_chamada_tool",
        lambda *a: ResultadoJuiz(confianca=0.95, motivo="Chamada simulada aprovada"),
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
        graph,
        "avaliar_alucinacao",
        lambda *a: ResultadoJuiz(confianca=0.95, motivo="Fatos simulados confirmados"),
    )
    monkeypatch.setattr(
        graph,
        "orquestrador_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: AIMessage(content="Aguarda aprovação.")
        ),
    )
    marker = current_context.set(replace(context_for(), turn_id="live-turn"))
    try:
        answer = graph.executar_fluxo_assessor(
            "Depois do almoço, usei dez quilos de arroz.", "live", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "AGUARDANDO_APROVACAO" in answer
    if partial:
        assert "Etapas não concluídas" in answer and "saldo insuficiente" in answer
    assert get_db().approvals.count_documents({}) == 1
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
    monkeypatch.setattr(
        graph,
        "avaliar_alucinacao",
        lambda *a: ResultadoJuiz(confianca=0.1, motivo="Quantidade alterada"),
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
