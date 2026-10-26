import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import ValidationError

from iai.app.core.assistant import artifacts as a
from iai.app.core.infra.storage import get_db
from iai.app.core.schemas import ChatResponse
from iai.app.main import app

AUTH = {"Authorization": "Bearer demo-estoquista"}


def chat(question):
    response = TestClient(app).post(
        "/chat",
        headers=AUTH,
        json={
            "session_id": "visual",
            "user_id": "demo-estoquista",
            "pergunta": question,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def evidence(rows, *, resource="estoque", incomplete=False, ok=True):
    return [
        HumanMessage(content="Compare"),
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "read",
                    "name": "consultar_inventra",
                    "args": {"resource": resource},
                }
            ],
        ),
        ToolMessage(
            name="consultar_inventra",
            tool_call_id="read",
            content=json.dumps(
                {
                    "ok": ok,
                    "code": "OK",
                    "message": "fonte",
                    "source": "Spring",
                    "mocked": False,
                    "incomplete": incomplete,
                    "data": rows,
                }
            ),
        ),
    ]


def stock_rows():
    return [
        {"product": {"name": name, "unit": {"symbol": "kg"}}, "totalQuantity": quantity}
        for name, quantity in [("Arroz", 12.125), ("Carne", 8)]
    ]


def test_demo_bar_copies_authorized_stock_exactly():
    result = chat("Compare as quantidades em estoque em um gráfico de barras")
    (artifact,) = result["artifacts"]
    assert artifact["type"] == "chart" and artifact["chart_type"] == "bar"
    assert artifact["labels"] == ["Arroz", "Carne", "Tomate"]
    assert artifact["datasets"][0]["data"] == [50, 100, 5]
    assert artifact["unit"] == "kg" and artifact["mocked"] is True
    assert "50" not in result["resposta"] and "100" not in result["resposta"]
    assert result["pending_actions"] == []


def test_demo_table_is_useful_without_explicit_table_request():
    result = chat(
        "Mostre os lotes próximos do vencimento com produto validade e quantidade"
    )
    (artifact,) = result["artifacts"]
    assert artifact["type"] == "table"
    assert {c["key"] for c in artifact["columns"]} >= {
        "produto",
        "quantidade",
        "validade",
    }
    assert [r["quantidade"] for r in artifact["rows"]] == [50, 100, 5]
    assert all(r["validade"] for r in artifact["rows"])


def test_demo_pie_counts_real_requisitions_without_fabricated_categories():
    chat("Mostre as requisições")  # inicializa fixtures
    get_db().business_mock.insert_one(
        {
            "_id": "1:requisition:2",
            "kitchen": 1,
            "kind": "requisition",
            "data": {"id": 2, "status": "APPROVED", "requester": {"id": "outro"}},
        }
    )
    result = chat("Como estão distribuídas as requisições por status?")
    (artifact,) = result["artifacts"]
    assert artifact["chart_type"] == "pie"
    assert dict(zip(artifact["labels"], artifact["datasets"][0]["data"])) == {
        "UNDER_REVIEW": 1,
        "APPROVED": 1,
    }


@pytest.mark.parametrize(
    "question",
    [
        "Quanto arroz temos?",
        "Como funciona o inventário?",
        "Olá",
        "Gere um gráfico de vendas",
    ],
)
def test_simple_or_unsupported_questions_have_no_visual(question):
    result = chat(question)
    assert result["artifacts"] == []
    if "vendas" in question:
        assert "não possui registros de vendas" in result["resposta"]


def test_action_returns_exact_persisted_review_and_never_a_chart():
    result = chat("Usei 2 kg de arroz")
    assert result["artifacts"] == []
    (action,) = result["pending_actions"]
    saved = get_db().approvals.find_one({"_id": action["id"]})
    assert (
        action["parameters"]
        == saved["action"]["parameters"]
        == {"batchId": 1, "quantity": "2"}
    )
    assert action["proposal_hash"] == saved["proposal_hash"]
    assert action["before"] == saved["before"]
    assert action["status"] == "AGUARDANDO_APROVACAO"
    assert get_db().execution_mock.count_documents({}) == 0


def test_kitchen_comparison_never_silently_becomes_own_stock_chart():
    result = chat("Compare o estoque das cozinhas")
    assert result["artifacts"] == []
    assert "limitado à cozinha autenticada" in result["resposta"]


def test_missing_required_expiration_does_not_become_an_incomplete_table():
    rows = [
        {"product": {"name": name}, "currentQuantity": 10, "expirationDate": date}
        for name, date in [("Arroz", "2026-10-28"), ("Carne", None)]
    ]
    assert (
        a.present(
            "Lotes vencendo em tabela", evidence(rows, resource="validade"), demo=True
        )[0]
        == []
    )


@pytest.mark.parametrize(
    "alteration", ["missing", "incomplete", "units", "invalid", "empty", "failed"]
)
def test_unusable_sources_never_get_filled_in(alteration):
    rows = stock_rows()
    if alteration == "missing":
        rows[1].pop("totalQuantity")
    if alteration == "units":
        rows[1]["product"]["unit"]["symbol"] = "litros"
    if alteration == "invalid":
        rows[1]["totalQuantity"] = "NaN"
    if alteration == "empty":
        rows = []
    visual, _ = a.present(
        "Compare em barras",
        evidence(
            rows, incomplete=alteration == "incomplete", ok=alteration != "failed"
        ),
        demo=True,
    )
    assert visual == []


def test_only_current_authorized_tools_are_artifact_sources():
    messages = evidence(stock_rows())
    assert a.operational_sources(messages, {}) == []
    permit = {
        "id": {"call_id": "read", "tool": "consultar_inventra", "status": "EXECUTED"}
    }
    assert len(a.operational_sources(messages, permit)) == 1
    assert (
        a.operational_sources(messages + [HumanMessage(content="outra rodada")], permit)
        == []
    )
    messages[2].name = "faq_retriever"
    assert a.operational_sources(messages) == []


def test_live_selects_format_with_structured_output_and_cannot_supply_values(
    monkeypatch,
):
    from iai.app.core.assistant import llms

    seen = []

    def structured(schema):
        assert schema is a.PresentationChoice

        def invoke(messages, **kwargs):
            seen.append(messages)
            return {"kind": "bar", "source_id": 0}

        return SimpleNamespace(invoke=invoke)

    monkeypatch.setattr(
        llms, "llm_rapido", SimpleNamespace(with_structured_output=structured)
    )
    visual, text = a.present("Compare", evidence(stock_rows()))
    assert visual[0].datasets[0].data == [12.125, 8]
    assert "12.125" not in str(seen) and "Arroz" not in str(seen)
    assert "12.125" not in text
    with pytest.raises(ValidationError):
        a.PresentationChoice(kind="bar", data=[999])


def test_presentation_failures_and_invalid_choices_keep_text_available(monkeypatch):
    monkeypatch.setattr(
        a,
        "choose_presentation",
        lambda *args, **kw: a.PresentationChoice(kind="table", source_id=999),
    )
    assert a.present("tabela", evidence(stock_rows()))[0] == []

    def fail(*args, **kwargs):
        raise TimeoutError()

    monkeypatch.setattr(a, "choose_presentation", fail)
    visual, message = a.present("tabela", evidence(stock_rows()))
    assert visual == [] and "não foi possível" in message.lower()


def test_api_schema_rejects_extra_artifacts_and_bad_dimensions():
    good = {
        "type": "chart",
        "chart_type": "bar",
        "source": "Spring",
        "mocked": False,
        "labels": ["A", "B"],
        "datasets": [{"label": "q", "data": [1, 2]}],
    }
    assert len(ChatResponse(resposta="ok", artifacts=[good]).artifacts) == 1
    for value in (
        [good, good],
        [dict(good, chart_type="line")],
        [dict(good, datasets=[{"label": "q", "data": [1]}])],
        [dict(good, datasets=[{"label": "q", "data": [True, 2]}])],
    ):
        with pytest.raises(ValidationError):
            ChatResponse(resposta="ok", artifacts=value)


def test_full_live_graph_builds_one_artifact_only_from_gate_authorized_result(
    monkeypatch,
):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    from iai.app.core.assistant import graph, judge
    from iai.app.core.assistant.tool_gate import JevToolGate
    from iai.app.core.tools.faq import faq_retriever
    from iai.app.core.tools.operational import consultar_inventra
    from tests_helpers import jev_evaluation

    class Model(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    tools = [consultar_inventra, faq_retriever]
    specialist = create_agent(
        Model(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "stock",
                            "name": "consultar_inventra",
                            "args": {"resource": "estoque"},
                        },
                        {
                            "id": "faq",
                            "name": "faq_retriever",
                            "args": {"question": "estoque mínimo"},
                        },
                    ],
                ),
                AIMessage(content="Estoque confirmado; regra consultada."),
            ]
        ),
        tools=tools,
        middleware=[JevToolGate("estoquista", tools)],
    )
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *args, **kw: {
                "messages": [AIMessage(content="ROUTE=estoquista")]
            }
        ),
    )
    monkeypatch.setattr(graph, "estoquista_app", specialist)
    monkeypatch.setattr(
        graph,
        "orquestrador_app",
        SimpleNamespace(invoke=lambda *args, **kw: AIMessage(content="Confirmado")),
    )
    monkeypatch.setattr(
        judge,
        "evaluate",
        lambda state: jev_evaluation(
            fields=state["tool"]["schema"].get("properties", {})
        ),
    )
    monkeypatch.setattr(
        a, "choose_presentation", lambda *args, **kw: a.PresentationChoice(kind="bar")
    )
    result = chat("Compare as quantidades em estoque e explique as regras")
    (artifact,) = result["artifacts"]
    assert artifact["datasets"][0]["data"] == [50, 100, 5]
    metric = get_db().metrics.find_one({})
    assert len(metric["jev"]) == 2 and all(
        c["status"] == "EXECUTED" for c in metric["jev"]
    )


def test_grouped_requisition_bar_and_category_pie_are_derived_counts():
    messages = evidence(
        [
            {"id": 1, "status": "APPROVED"},
            {"id": 2, "status": "UNDER_REVIEW"},
            {"id": 3, "status": "APPROVED"},
        ],
        resource="requisicoes",
    )
    visual, _ = a.present("Compare requisições em barras", messages, demo=True)
    assert visual[0].datasets[0].data == [2, 1]
    messages = evidence(
        [{"product": {"category": {"name": name}}} for name in ["A", "B", "A"]],
        resource="lotes",
    )
    visual, _ = a.present("Distribuição em pizza", messages, demo=True)
    assert visual[0].labels == ["A", "B"] and visual[0].datasets[0].data == [2, 1]


@pytest.mark.parametrize(
    "rows",
    [
        [{"id": 1}, {"id": 2}],
        [{"id": 1, "status": "OPEN"}, {"id": 2, "status": "OPEN"}],
    ],
)
def test_no_chart_for_missing_or_single_distribution_category(rows):
    assert (
        a.present(
            "Distribuição em pizza", evidence(rows, resource="inventarios"), demo=True
        )[0]
        == []
    )


def test_artifact_schema_rejects_mismatched_columns_duplicate_labels_and_invalid_pie():
    from iai.app.core.schemas import ChartArtifact, TableArtifact

    with pytest.raises(ValidationError):
        TableArtifact(
            source="Spring",
            mocked=False,
            columns=[{"key": "a", "label": "A"}],
            rows=[{"a": 1}, {"b": 2}],
        )
    for labels, data in [
        (["A", "A"], [1, 2]),
        (["A", "B"], [-1, 2]),
        (["A", "B"], [0, 0]),
    ]:
        with pytest.raises(ValidationError):
            ChartArtifact(
                source="Spring",
                mocked=False,
                chart_type="pie",
                labels=labels,
                datasets=[{"label": "Registros", "data": data}],
            )


@pytest.mark.parametrize(
    "value", [None, True, "texto", "Infinity", "1.123456789123456789"]
)
def test_invalid_or_inexact_chart_numbers_are_not_coerced(value):
    with pytest.raises(ValueError):
        a.number(value)
