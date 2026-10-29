"""Apresentação de evidência operacional: o modelo escolhe formato, nunca valores."""

import json
import math
import re
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
    "consumo": "Quantidade consumida",
}
MAX_CATEGORIAS = 12  # acima disso o gráfico mostra só os maiores
TOP_CATEGORIAS = 10
NOTA_CONSUMO = (
    "Estimativa pela baixa acumulada dos lotes (quantidade inicial − atual); "
    "não é o histórico de saídas."
)
NOTA_CONSUMO_REAL = "Soma das baixas registradas no histórico de movimentações."
PEDIDO_VISUAL = ("grafico", "tabela", "pizza", "barra")
VERBOS_DE_ACAO = (
    "baixa",
    "dar baixa",
    "registr",
    "cadastr",
    "propon",
    "propost",
    "ajust",
    "cancel",
    "desativ",
    "editar",
    "abrir",
    "adicion",
    "remov",
    "criar",
    "crie ",
    "usei",
    "gastei",
)


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


def visual_requested(question) -> bool:
    """O usuário pediu explicitamente gráfico, barras, pizza ou tabela."""
    text = normalize(question)
    return any(term in text for term in PEDIDO_VISUAL)


def looks_like_action(question) -> bool:
    """Pedido que altera dados: nunca vira atalho de visualização."""
    text = normalize(question)
    return any(term in text for term in VERBOS_DE_ACAO)


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
        "consumo": row.get("consumedQuantity"),
    }
    result = {}
    for key, value in fields.items():
        if value is not None:
            result[key] = (
                number(value)
                if key in {"quantidade", "minimo", "fisica", "consumo"}
                else remove_secrets(str(value))
            )
    return result


def make_source(resource, outcome):
    """Fonte apresentável a partir de um ToolResult confirmado, ou None se não servir."""
    data = outcome.data
    rows = data.get("content", [data]) if isinstance(data, dict) else data
    if not outcome.ok or not isinstance(rows, list) or not 2 <= len(rows) <= 100:
        return None
    projected = [project(r, resource) for r in rows]
    columns = [key for key in LABELS if all(key in r for r in projected)]
    return {
        "resource": resource,
        "rows": projected,
        "columns": columns,
        "source": outcome.source,
        "mocked": outcome.mocked,
        "incomplete": outcome.incomplete,
        "estimado": resource == "consumo"
        and not str(outcome.message).startswith("Consumo = soma"),
    }


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
            source = make_source(
                call["args"]["resource"],
                ToolResult.model_validate_json(message.content),
            )
            if source:
                sources.append(source)
        except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
            continue
    return sources


def resource_for(question) -> str:
    """Recurso padrão para um pedido de visualização que o modelo não consultou."""
    text = normalize(question)
    if re.search(
        r"\b(sai|saem|saida|saidas|consum\w*|giro|gast\w+|usad\w+|utiliz\w+)\b", text
    ):
        return "consumo"
    if re.search(r"\b(requisic\w+|pedidos? de compra)\b", text):
        return "requisicoes"
    if re.search(r"\b(venc\w+|validade|expir\w+)\b", text):
        return "validade"
    if re.search(r"\b(minimo|abaixo|repor|reposic\w+|acabando)\b", text):
        return "estoque_minimo"
    if re.search(r"\blotes?\b", text):
        return "lotes"
    return "estoque"


def fallback_sources(question):
    """Leitura determinística (somente leitura, escopo autenticado) para o gráfico pedido.

    O modelo às vezes recusa ("não consigo gerar gráficos") ou não consulta nada. O
    desenho é montado pelo backend sobre dados confirmados, então a leitura também é.
    """
    from iai.app.core.infra.security import context
    from iai.app.core.inventra.business import gateway
    from iai.app.core.inventra.domain import Query

    resource = resource_for(question)
    try:
        extra = {"days": 30} if resource == "validade" else {}
        outcome = gateway().query(
            Query(resource=resource, size=100, **extra), context()
        )
        source = make_source(resource, outcome)
    except Exception:  # noqa: BLE001 - visualização é opcional; o texto continua
        return []
    return [source] if source else []


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


def explicit_choice(question, sources):
    """Formato dito pelo próprio usuário ("pizza", "barras", "tabela"): sem consultar LLM."""
    text = normalize(question)
    if "pizza" in text:
        kind = "pie"
    elif "barra" in text:
        kind = "bar"
    elif "tabela" in text:
        kind = "table"
    elif "grafico" in text:
        kind = (
            "pie"
            if re.search(r"distribu|participa|proporc|percentual|fatia", text)
            else "bar"
        )
    else:
        return None
    for i, source in enumerate(sources):
        choice = PresentationChoice(kind=kind, source_id=i)
        if not source["incomplete"] and build_artifact(choice, sources) is not None:
            return choice
    return PresentationChoice(kind=kind, source_id=0)


def choose_presentation(question, sources, *, demo=False, config=None):
    if demo:
        return demo_choice(question, sources)
    explicit = explicit_choice(question, sources)
    if explicit:
        return explicit
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


# Conversões exatas dentro da mesma grandeza. Litro e quilo NÃO se convertem: dependem da
# densidade do produto (1 L de óleo não pesa 1 kg), então ficam em gráficos separados.
FATORES = {
    "massa": {
        "mg": Decimal("0.000001"),
        "g": Decimal("0.001"),
        "gr": Decimal("0.001"),
        "kg": Decimal(1),
        "t": Decimal(1000),
    },
    "volume": {
        "ml": Decimal("0.001"),
        "cl": Decimal("0.01"),
        "dl": Decimal("0.1"),
        "l": Decimal(1),
        "lt": Decimal(1),
        "litro": Decimal(1),
        "litros": Decimal(1),
    },
    "contagem": {
        "un": Decimal(1),
        "und": Decimal(1),
        "unid": Decimal(1),
        "unidade": Decimal(1),
        "unidades": Decimal(1),
    },
}
CHAVES_DE_QUANTIDADE = ("quantidade", "minimo", "consumo")


def grandeza(unit):
    """(grandeza, fator para a unidade-base). Unidade desconhecida só combina com ela mesma."""
    symbol = normalize(str(unit)).strip(". ")
    for name, table in FATORES.items():
        if symbol in table:
            return name, table[symbol]
    return f"outra:{symbol}", Decimal(1)


def convert_row(row, from_unit, to_unit):
    if from_unit == to_unit:
        return row
    ratio = grandeza(from_unit)[1] / grandeza(to_unit)[1]
    converted = dict(row, unidade=to_unit)
    for key in CHAVES_DE_QUANTIDADE:
        if key in row:
            value = (Decimal(str(row[key])) * ratio).quantize(Decimal("0.001"))
            converted[key] = number(value)
    return converted


def dominant_unit(rows, value_key):
    """Só quantidades da mesma grandeza são comparáveis; dentro dela, converte para uma unidade.

    kg, g e mg viram uma só unidade (idem L e mL). Massa, volume e contagem nunca se misturam:
    fica a grandeza com mais itens. Devolve (unidade, linhas_convertidas, nota) ou None; a nota
    explica o que foi convertido e o que ficou de fora.
    """
    groups = {}
    for row in rows:
        if value_key in row and "unidade" in row:
            groups.setdefault(grandeza(row["unidade"])[0], []).append(row)
    if not groups:
        return None

    def weight(item):
        return (
            len(item[1]),
            sum(r[value_key] * float(grandeza(r["unidade"])[1]) for r in item[1]),
        )

    _, members = max(groups.items(), key=weight)
    counts = Counter(r["unidade"] for r in members)
    unit = max(counts, key=lambda u: (counts[u], grandeza(u)[1]))
    kept = [convert_row(r, r["unidade"], unit) for r in members]
    notes = []
    if len(counts) > 1:
        notes.append(f"Valores convertidos para {unit}.")
    left_out = len(rows) - len(kept)
    if left_out:
        others = sorted({r["unidade"] for r in rows if "unidade" in r} - set(counts))
        reason = f" ({', '.join(others)})" if others else ""
        subject = (
            "1 item ficou de fora por estar"
            if left_out == 1
            else f"{left_out} itens ficaram de fora por estarem"
        )
        notes.append(
            f"{subject} em outra grandeza ou sem unidade{reason}; não converto litro "
            "em quilo sem saber a densidade do produto."
        )
    return unit, kept, " ".join(notes) or None


def top_categories(rows, value_key):
    """Poucos itens ficam como vieram; muitos viram os maiores (legível no celular)."""
    if len(rows) <= MAX_CATEGORIAS:
        return rows, None
    biggest = sorted(rows, key=lambda r: -r[value_key])[:TOP_CATEGORIAS]
    return biggest, f"Mostrando os {TOP_CATEGORIAS} maiores de {len(rows)} itens."


def join_notes(*notes):
    text = " ".join(n for n in notes if n)
    # O contrato limita a nota a 300 caracteres; cortar é melhor que perder o gráfico.
    return (text if len(text) <= 300 else text[:297] + "...") or None


def unique_category(rows):
    return next(
        (
            k
            for k in ("produto", "lote", "fornecedor")
            if all(k in r for r in rows) and len({r[k] for r in rows}) == len(rows)
        ),
        None,
    )


def build_artifact(choice, sources):
    if choice.kind == "none" or choice.source_id >= len(sources):
        return None
    source = sources[choice.source_id]
    if source["incomplete"]:
        return None
    rows, columns = source["rows"], source["columns"]
    resource = source["resource"]
    ranking = resource == "consumo"
    metadata = {"source": source["source"], "mocked": source["mocked"]}
    base_note = None
    if ranking:
        base_note = NOTA_CONSUMO if source.get("estimado") else NOTA_CONSUMO_REAL
    if choice.kind == "table":
        required = {
            "validade": {"produto", "validade", "quantidade"},
            "estoque_minimo": {"produto", "quantidade", "minimo"},
            "estoque": {"produto", "quantidade"},
            "contagens": {"fisica"},
            "consumo": {"produto", "consumo"},
        }.get(resource, set())
        if not required.issubset(columns):
            return None
        wanted = [c for c in columns if c != "id"]
        if ranking:
            order = ["produto", "consumo", "unidade", "quantidade", "categoria"]
            wanted = [c for c in order if c in wanted]
        selected = wanted[:8]
        if len(selected) < 2:
            return None
        return TableArtifact(
            columns=[TableColumn(key=k, label=LABELS[k]) for k in selected],
            rows=[{k: r[k] for k in selected} for r in rows],
            note=base_note,
            **metadata,
        )
    # Pizza de contagem: registros por categoria/status (nunca soma de unidades distintas).
    if choice.kind == "pie":
        group = next((k for k in ("status", "categoria") if k in columns), None)
        if group and not ranking:
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
        # Pizza de participação: fatia de cada produto no total, na mesma unidade.
        value_key = "consumo" if ranking else "quantidade"
        picked = dominant_unit(rows, value_key)
        if not picked:
            return None
        unit, kept, unit_note = picked
        kept = [r for r in kept if r[value_key] > 0 and "produto" in r]
        if len(kept) < 2 or len({r["produto"] for r in kept}) != len(kept):
            return None
        kept, top_note = top_categories(kept, value_key)
        return ChartArtifact(
            chart_type="pie",
            labels=[r["produto"] for r in kept],
            datasets=[
                ChartDataset(label=LABELS[value_key], data=[r[value_key] for r in kept])
            ],
            unit=unit,
            note=join_notes(base_note, unit_note, top_note),
            **metadata,
        )
    if resource == "requisicoes" and "status" in columns:
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
    value_key = "consumo" if ranking else "quantidade"
    picked = dominant_unit(rows, value_key)
    if not picked:
        return None
    unit, kept, unit_note = picked
    if len(kept) < 2:
        return None
    if ranking:
        kept = sorted(kept, key=lambda r: -r["consumo"])
    kept, top_note = top_categories(kept, value_key)
    category = unique_category(kept)
    if not category:
        return None
    series = [value_key]
    if not ranking and "minimo" in columns:
        series.append("minimo")
    return ChartArtifact(
        chart_type="bar",
        labels=[r[category] for r in kept],
        datasets=[
            ChartDataset(label=LABELS[k], data=[r[k] for r in kept]) for k in series
        ],
        unit=unit,
        note=join_notes(base_note, unit_note, top_note),
        **metadata,
    )


def describe(artifact, resource):
    """Texto montado pelo backend: cita o formato e os nomes, nunca os valores."""
    if artifact.type == "table":
        if resource == "consumo":
            description = "A tabela mostra o consumo estimado de cada produto, do maior para o menor."
        else:
            fields = ", ".join(c.label.lower() for c in artifact.columns)
            description = f"A tabela apresenta {fields}, permitindo consultar os campos de cada registro."
    elif resource == "consumo":
        nomes = artifact.labels[:3]
        lista = ", ".join(nomes[:-1]) + (" e " if len(nomes) > 1 else "") + nomes[-1]
        description = (
            "Os produtos que mais saem, pela baixa acumulada nos lotes, do maior "
            f"para o menor: {lista}."
        )
    else:
        fields = " e ".join(d.label.lower() for d in artifact.datasets)
        description = f"O gráfico apresenta {fields} por categoria, permitindo visualizar {'a distribuição dos registros' if artifact.chart_type == 'pie' else 'as diferenças entre os registros'}."
    if artifact.mocked:
        description = "[DADOS MOCK — demonstração] " + description
    return description


def present(
    question, messages, *, demo=False, jev_calls=None, config=None, fetch_missing=False
):
    """Monta no máximo um artefato. `fetch_missing` permite ao backend ler os dados
    quando o usuário pediu a visualização e o modelo não consultou (ou recusou)."""
    requested = visual_requested(question)
    can_fetch = fetch_missing and requested and not unsupported_sales(question)
    sources = operational_sources(messages, jev_calls)
    fetched = False
    if not sources and can_fetch:
        sources, fetched = fallback_sources(question), True
    if not sources:
        return (
            [],
            "Não há registros confirmados suficientes para essa visualização."
            if requested and not unsupported_sales(question)
            else None,
        )
    prefix = ""
    try:
        choice = choose_presentation(question, sources, demo=demo, config=config)
        artifact = build_artifact(choice, sources)
        if artifact is None and can_fetch and not fetched:
            more = fallback_sources(question)
            if more:
                sources = more
                choice = choose_presentation(
                    question, sources, demo=demo, config=config
                )
                artifact = build_artifact(choice, sources)
        if artifact is None and can_fetch and choice.kind in {"bar", "pie"}:
            table = PresentationChoice(kind="table", source_id=choice.source_id)
            artifact = build_artifact(table, sources)
            if artifact is not None:
                prefix = "Não deu para montar o gráfico com esses dados, então mostrei uma tabela. "
    except Exception:  # noqa: BLE001 - apresentação opcional não invalida uma consulta
        return [], "Não foi possível montar a visualização com os dados confirmados."
    if artifact is None:
        return (
            [],
            "Os dados confirmados são insuficientes ou incompatíveis para essa visualização."
            if requested
            else None,
        )
    return [artifact], prefix + describe(
        artifact, sources[choice.source_id]["resource"]
    )
