"""Apresentação de evidência operacional: o modelo escolhe formato, nunca valores."""

import json
import math
from collections import Counter
from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from iai.app.core.infra.privacy import remove_secrets
from iai.app.core.infra.retrieval import normalize
from iai.app.core.inventra.domain import ToolResult
from iai.app.core.schemas import ChartArtifact, ChartDataset, TableArtifact, TableColumn

SALES_MESSAGE = (
    "O Inventra não possui registros de vendas. Posso gerar uma visualização usando dados "
    "de estoque, requisições, lotes ou inventários. O que você deseja comparar?"
)
KITCHEN_MESSAGE = "Seu acesso está limitado à cozinha autenticada. Não posso comparar dados de outras cozinhas."
LABELS = {
    "produto": "Produto",
    "lote": "Lote",
    "validade": "Validade",
    "quantidade": "Quantidade atual",
    "minimo": "Estoque mínimo",
    "unidade": "Unidade",
    "status": "Status",
    "fornecedor": "Fornecedor",
    "categoria": "Categoria",
    "id": "Identificador",
    "fisica": "Quantidade física",
}


class PresentationChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["none", "table", "bar", "pie"] = "none"
    source_id: int = Field(default=0, ge=0)


def unsupported_sales(question):
    return any(
        word in normalize(question).split()
        for word in ("venda", "vendas", "faturamento")
    )


def unsupported_kitchens(question):
    text = normalize(question)
    return "cozinhas" in text and any(
        t in text for t in ("compar", "entre", "cada", "todas", "selecionadas")
    )


def number(value):
    if value is None or isinstance(value, bool):
        raise ValueError("Quantidade ausente ou inválida.")
    try:
        decimal = Decimal(str(value))
        converted = float(decimal)
        if not math.isfinite(converted) or Decimal(str(converted)) != decimal:
            raise ValueError("Valor sem representação numérica fiel.")
        return int(decimal) if decimal == decimal.to_integral() else converted
    except (InvalidOperation, OverflowError):
        raise ValueError("Valor inválido.") from None


def project(row, resource):
    """Whitelist de campos dos contratos atuais; não preencher valores ausentes."""
    product = row.get("product") or {}
    unit = product.get("unit") or row.get("unit") or {}
    supplier = row.get("supplier") or {}
    category = product.get("category") or row.get("category") or {}
    fields = {
        "produto": product.get("name")
        or (row.get("name") if resource == "produtos" else None),
        "lote": row.get("batchNumber"),
        "validade": row.get("expirationDate"),
        "quantidade": row.get("totalQuantity", row.get("currentQuantity")),
        "minimo": row.get("minStock"),
        "unidade": unit.get("symbol"),
        "status": row.get("status"),
        "fornecedor": supplier.get("legalName") or row.get("legalName"),
        "categoria": category.get("name"),
        "id": row.get("id"),
        "fisica": row.get("physicalQuantity"),
    }
    result = {}
    for key, value in fields.items():
        if value is not None:
            result[key] = (
                number(value)
                if key in {"quantidade", "minimo", "fisica"}
                else remove_secrets(str(value))
            )
    return result


def operational_sources(messages, jev_calls=None):
    """Só resultados desta rodada, associados à chamada e, no live, ao gate."""
    sources = []
    calls = {}
    start = max((i for i, m in enumerate(messages) if m.type == "human"), default=-1)
    authorized = {
        c["call_id"]
        for c in (jev_calls or {}).values()
        if c["status"] == "EXECUTED" and c["tool"] == "consultar_inventra"
    }
    for message in messages[start + 1 :]:
        for call in getattr(message, "tool_calls", []):
            calls[call["id"]] = call
        if (
            message.type != "tool"
            or message.name != "consultar_inventra"
            or message.status == "error"
        ):
            continue
        call = calls.get(message.tool_call_id)
        if not call or call["name"] != "consultar_inventra":
            continue
        if jev_calls is not None and message.tool_call_id not in authorized:
            continue
        try:
            outcome = ToolResult.model_validate_json(message.content)
            data = outcome.data
            rows = data.get("content", [data]) if isinstance(data, dict) else data
            if (
                not outcome.ok
                or not isinstance(rows, list)
                or not 2 <= len(rows) <= 100
            ):
                continue
            projected = [project(r, call["args"]["resource"]) for r in rows]
            columns = [key for key in LABELS if all(key in r for r in projected)]
            sources.append(
                {
                    "resource": call["args"]["resource"],
                    "rows": projected,
                    "columns": columns,
                    "source": outcome.source,
                    "mocked": outcome.mocked,
                    "incomplete": outcome.incomplete,
                }
            )
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return sources


def demo_choice(question, sources):
    text = normalize(question)
    for i, source in enumerate(sources):
        if source["incomplete"]:
            continue
        if any(t in text for t in ("distribui", "participacao", "proporcao", "pizza")):
            return PresentationChoice(kind="pie", source_id=i)
        if "tabela" in text or source["resource"] in {
            "validade",
            "estoque_minimo",
            "contagens",
        }:
            return PresentationChoice(kind="table", source_id=i)
        if "compar" in text or "barras" in text or "grafico" in text:
            return PresentationChoice(kind="bar", source_id=i)
        if len(source["rows"]) >= 5 and len(source["columns"]) >= 3:
            return PresentationChoice(kind="table", source_id=i)
    return PresentationChoice()


def choose_presentation(question, sources, *, demo=False, config=None):
    if demo:
        return demo_choice(question, sources)
    from iai.app.core.assistant.llms import llm_rapido
    from iai.app.core.assistant.prompts import PRESENTATION_PROMPT

    # Dados/valores nem sequer fazem parte do contrato de escolha.
    catalog = [
        {
            "source_id": i,
            "resource": s["resource"],
            "columns": s["columns"],
            "records": len(s["rows"]),
            "incomplete": s["incomplete"],
        }
        for i, s in enumerate(sources)
    ]
    return PresentationChoice.model_validate(
        llm_rapido.with_structured_output(PresentationChoice).invoke(
            [
                ("system", PRESENTATION_PROMPT),
                (
                    "human",
                    json.dumps(
                        {"question": question, "sources": catalog}, ensure_ascii=False
                    ),
                ),
            ],
            config=config,
        )
    )


def build_artifact(choice, sources):
    if choice.kind == "none" or choice.source_id >= len(sources):
        return None
    source = sources[choice.source_id]
    if source["incomplete"]:
        return None
    rows, columns = source["rows"], source["columns"]
    metadata = {"source": source["source"], "mocked": source["mocked"]}
    if choice.kind == "table":
        required = {
            "validade": {"produto", "validade", "quantidade"},
            "estoque_minimo": {"produto", "quantidade", "minimo"},
            "estoque": {"produto", "quantidade"},
            "contagens": {"fisica"},
        }.get(source["resource"], set())
        if not required.issubset(columns):
            return None
        selected = [c for c in columns if c != "id"][:8]
        if len(selected) < 2:
            return None
        return TableArtifact(
            columns=[TableColumn(key=k, label=LABELS[k]) for k in selected],
            rows=[{k: r[k] for k in selected} for r in rows],
            **metadata,
        )
    # Pizza é contagem de registros por uma categoria, nunca soma de unidades distintas.
    if choice.kind == "pie":
        group = next((k for k in ("status", "categoria") if k in columns), None)
        if not group:
            return None
        counts = Counter(r[group] for r in rows)
        if len(counts) < 2:
            return None
        return ChartArtifact(
            chart_type="pie",
            labels=list(counts),
            datasets=[
                ChartDataset(
                    label="Quantidade de registros", data=list(counts.values())
                )
            ],
            unit="registros",
            **metadata,
        )
    if source["resource"] == "requisicoes" and "status" in columns:
        counts = Counter(r["status"] for r in rows)
        if len(counts) < 2:
            return None
        return ChartArtifact(
            chart_type="bar",
            labels=list(counts),
            datasets=[
                ChartDataset(
                    label="Quantidade de requisições", data=list(counts.values())
                )
            ],
            unit="requisições",
            **metadata,
        )
    category = next(
        (
            k
            for k in ("produto", "lote", "fornecedor")
            if k in columns and len({r[k] for r in rows}) == len(rows)
        ),
        None,
    )
    if not category or "quantidade" not in columns or "unidade" not in columns:
        return None
    if len({r["unidade"] for r in rows}) != 1:
        return None
    return ChartArtifact(
        chart_type="bar",
        labels=[r[category] for r in rows],
        datasets=[
            ChartDataset(label=LABELS[k], data=[r[k] for r in rows])
            for k in ("quantidade", "minimo")
            if k in columns
        ],
        unit=rows[0]["unidade"],
        **metadata,
    )


def present(question, messages, *, demo=False, jev_calls=None, config=None):
    sources = operational_sources(messages, jev_calls)
    requested = any(
        t in normalize(question) for t in ("grafico", "tabela", "pizza", "barras")
    )
    if not sources:
        return (
            [],
            "Não há registros confirmados suficientes para essa visualização."
            if requested and not unsupported_sales(question)
            else None,
        )
    try:
        choice = choose_presentation(question, sources, demo=demo, config=config)
        artifact = build_artifact(choice, sources)
    except Exception:  # noqa: BLE001 - apresentação opcional não invalida uma consulta
        return [], "Não foi possível montar a visualização com os dados confirmados."
    if artifact is None:
        return (
            [],
            "Os dados confirmados são insuficientes ou incompatíveis para essa visualização."
            if requested
            else None,
        )
    if artifact.type == "table":
        fields = ", ".join(c.label.lower() for c in artifact.columns)
        description = f"A tabela apresenta {fields}, permitindo consultar os campos de cada registro."
    else:
        fields = " e ".join(d.label.lower() for d in artifact.datasets)
        description = f"O gráfico apresenta {fields} por categoria, permitindo visualizar {'a distribuição dos registros' if artifact.chart_type == 'pie' else 'as diferenças entre os registros'}."
    if artifact.mocked:
        description = "[DADOS MOCK — demonstração] " + description
    return [artifact], description
