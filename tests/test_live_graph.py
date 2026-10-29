"""Grafo e agentes reais do LangChain com LLM roteirizado. Não avalia provedor real."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage

from iai.app.core.assistant import graph, judge
from iai.app.core.assistant.tool_gate import JevToolGate
from iai.app.core.infra.security import current_context
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.domain import Operation
from iai.app.core.tools.operational import PROPOSAL_TOOLS, consultar_inventra
from tests_helpers import context_for, jev_evaluation, jev_fidelidade


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
                AIMessage(content="Registrei o que consegui."),
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
    assert "AGUARDANDO_APROVACAO" in answer and "quantidade e a unidade" in answer
    assert "JEV" not in answer and "RETRY_SPECIALIST" not in answer
    # A proposta reprovada nasce pendente, mas é cancelada antes de o usuário vê-la.
    assert get_db().approvals.count_documents({"judge_cancelled": {"$ne": True}}) == 1
    assert get_db().approvals.count_documents({"judge_cancelled": True}) == 1
    assert get_db().execution_mock.count_documents({}) == 0


def prepara_especialista(monkeypatch, agent):
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
        "orquestrador_app",
        SimpleNamespace(
            invoke=lambda entrada: AIMessage(
                content=entrada["mensagens"].split(": ", 1)[1]
            )
        ),
    )


def test_proposta_reprovada_e_refeita_chega_ao_usuario_uma_so_vez(monkeypatch):
    proposal = PROPOSAL_TOOLS[Operation.CONSUME]
    chamada = AIMessage(
        content="",
        tool_calls=[
            {
                "id": "saida",
                "name": proposal.name,
                "args": {
                    "batchId": 1,
                    "quantity": "10",
                    "justification": "Consumo informado",
                },
            }
        ],
    )
    agent = create_agent(
        ScriptedModel(
            responses=[
                chamada,
                AIMessage(content="Proposta criada."),
                chamada.model_copy(update={"id": None}),
                AIMessage(content="Proposta refeita."),
            ]
        ),
        tools=[proposal],
        middleware=[JevToolGate("estoquista", [proposal])],
    )
    prepara_especialista(monkeypatch, agent)
    decisoes = iter(
        [
            jev_evaluation(
                failed={"arguments_grounded": 0.1},
                route="agent_repair",
                fields=["batchId", "quantity", "justification"],
            ),
            jev_evaluation(fields=["batchId", "quantity", "justification"]),
        ]
    )
    monkeypatch.setattr(judge, "evaluate", lambda state: next(decisoes))
    marker = current_context.set(replace(context_for(), turn_id="refeita"))
    try:
        answer = graph.executar_fluxo_chat(
            "Usei 10 kg de arroz.", "refeita", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "AGUARDANDO_APROVACAO" in answer.resposta
    assert len(answer.pending_actions) == 1
    assert get_db().approvals.count_documents({"judge_cancelled": {"$ne": True}}) == 1
    assert get_db().approvals.count_documents({"judge_cancelled": True}) == 1
    assert get_db().execution_mock.count_documents({}) == 0
    assert "Revisão interna" not in answer.resposta


def test_resposta_de_leitura_reprovada_e_refeita_com_o_dado_da_tool(monkeypatch):
    agent = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "read",
                            "name": "consultar_inventra",
                            "args": {"resource": "lotes"},
                        }
                    ],
                ),
                AIMessage(content="O arroz tem 99999 unidades."),
                AIMessage(content="Há lotes cadastrados."),
            ]
        ),
        tools=[consultar_inventra],
        middleware=[JevToolGate("estoquista", [consultar_inventra])],
    )
    prepara_especialista(monkeypatch, agent)
    decisoes = iter(
        [jev_fidelidade(failed={"quantities_supported": 0.1}), jev_fidelidade()]
    )
    monkeypatch.setattr(judge, "evaluate_fidelidade", lambda state: next(decisoes))
    marker = current_context.set(replace(context_for(), turn_id="leitura-refeita"))
    try:
        answer = graph.executar_fluxo_assessor(
            "Mostre os lotes.", "leitura-refeita", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "Há lotes cadastrados." in answer and "99999" not in answer
    metric = get_db().metrics.find_one({})
    revisoes = [c for c in metric["jev"] if c["tool"] == "resposta_final"]
    assert [c["decision"] for c in revisoes] == ["RETRY_SPECIALIST", "APPROVE"]


def test_live_missing_typesafe_key_cancels_the_proposal(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr(""))
    proposal = PROPOSAL_TOOLS[Operation.CONSUME]
    agent = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "blocked",
                            "name": proposal.name,
                            "args": {
                                "batchId": 1,
                                "quantity": "10",
                                "justification": "Consumo informado",
                            },
                        }
                    ],
                ),
                AIMessage(content="Proposta criada."),
            ]
        ),
        tools=[proposal],
        middleware=[JevToolGate("estoquista", [proposal])],
    )
    prepara_especialista(monkeypatch, agent)
    marker = current_context.set(replace(context_for(), turn_id="missing-jev"))
    try:
        answer = graph.executar_fluxo_chat(
            "Usei 10 kg de arroz.", "missing-jev", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "indisponível" in answer.resposta and "JEV" not in answer.resposta
    assert answer.pending_actions == []
    assert get_db().approvals.count_documents({"judge_cancelled": {"$ne": True}}) == 0
    assert get_db().execution_mock.count_documents({}) == 0
    metric = get_db().metrics.find_one({})
    assert metric["status"] == "erro" and metric["jev"][-1]["status"] == "REJECT"
    assert "arguments" not in str(metric["jev"])


def test_live_missing_typesafe_key_still_answers_reads(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr(""))
    agent = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "read",
                            "name": "consultar_inventra",
                            "args": {"resource": "lotes"},
                        }
                    ],
                ),
                AIMessage(content="Há lotes cadastrados."),
            ]
        ),
        tools=[consultar_inventra],
        middleware=[JevToolGate("estoquista", [consultar_inventra])],
    )
    prepara_especialista(monkeypatch, agent)
    marker = current_context.set(replace(context_for(), turn_id="read-no-jev"))
    try:
        answer = graph.executar_fluxo_assessor(
            "Mostre os lotes.", "read-no-jev", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert "Há lotes cadastrados." in answer


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


def test_pergunta_de_faq_percorre_o_grafo_e_o_jev_confere_o_texto(monkeypatch):
    """Busca FAQ determinística; o modelo redige e o JEV confere o texto contra o trecho."""
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

    seen = []

    def evaluate(state):
        seen.append(state)
        return jev_fidelidade()

    monkeypatch.setattr(judge, "evaluate_fidelidade", evaluate)
    monkeypatch.setattr(
        graph,
        "llm_especialista",
        SimpleNamespace(
            invoke=lambda *a, **kw: AIMessage(content="O OCR lê a foto do produto.")
        ),
    )
    marker = current_context.set(replace(context_for(), turn_id="faq-turn"))
    try:
        answer = graph.executar_fluxo_assessor(
            "O que é o OCR?", "faq", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)
    assert len(seen) == 1 and seen[0]["evidence"][0]["tool"] == "faq_retriever"
    assert "O OCR lê a foto do produto." in answer
    assert "JEV" not in answer and "confirmar" not in answer
