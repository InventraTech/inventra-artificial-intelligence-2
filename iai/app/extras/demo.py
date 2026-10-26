"""Intérprete determinístico LIMITADO, exclusivamente demonstrativo e sem LLM.

O atendimento natural completo usa agentes live. Esta simulação permite provar o
fluxo dos pedidos de ação offline; não é apresentada como capacidade geral de linguagem.
"""

import re

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.graph import END, StateGraph
from langgraph.prebuilt import ToolNode

from iai.app.core.assistant.guardrail import verificar
from iai.app.core.infra.retrieval import normalize
from iai.app.core.infra.security import context
from iai.app.core.inventra.business import gateway
from iai.app.core.inventra.domain import Operation, Query
from iai.app.core.schemas import Estado
from iai.app.core.tools.documentos import buscar_documentos
from iai.app.core.tools.faq import faq_retriever
from iai.app.core.tools.memoria import buscar_historico
from iai.app.core.tools.operational import PROPOSAL_TOOLS, consultar_inventra


def plan_demo_reads(text):
    """Plano determinístico limitado. Só lotes de leituras já independentes."""
    normalized = normalize(text)
    resources = []
    if any(t in normalized for t in ("abaixo", "minimo", "em falta")):
        resources.append("estoque_minimo")
    elif "estoque" in normalized or "quanto" in normalized:
        resources.append("estoque")
    for terms, resource in [
        (("venc", "validade"), "validade"),
        (("requis", "pedido"), "requisicoes"),
        (("fornecedor",), "fornecedores"),
        (("inventario",), "inventarios"),
        (("alerta",), "alertas"),
        (("lote",), "lotes"),
    ]:
        if any(t in normalized for t in terms):
            if resource == "lotes" and "validade" in resources:
                continue
            resources.append(resource)
    if "fornecedores" in resources and "recorrent" in normalized:
        resources[resources.index("fornecedores")] = "fornecedores_recorrentes"
    conceptual = normalized.startswith(("como ", "o que e ", "quem pode")) and not any(
        t in normalized
        for t in (
            " e ",
            "tambem",
            "compare",
            "mostre",
            "distribui",
            "grafico",
            "tabela",
        )
    )
    calls = []
    if not conceptual:
        days = re.search(r"(\d+)\s*dias", normalized)
        for resource in dict.fromkeys(resources):
            query = Query(
                resource=resource,
                days=int(days[1]) if days else 7,
                status="UNDER_REVIEW"
                if resource == "requisicoes" and "pendent" in normalized
                else None,
            )
            calls.append(
                {
                    "name": "consultar_inventra",
                    "args": query.model_dump(mode="json", exclude_none=True),
                }
            )
    if (
        conceptual
        or not calls
        or any(t in normalized for t in ("explique", "como funciona", "regra"))
    ):
        calls.append({"name": "faq_retriever", "args": {"question": text}})
    if any(t in normalized for t in ("conversa anterior", "falamos", "conversamos")):
        if not resources:
            calls = []
        calls.append({"name": "buscar_historico", "args": {"busca": ""}})
    if not conceptual and any(
        t in normalized
        for t in ("documento", "arquivo", "planilha", "contrato", "anexo")
    ):
        if not resources:
            calls = []
        calls.append({"name": "buscar_documentos", "args": {"consulta": text}})
    return [dict(c, id=f"demo-read-{i}") for i, c in enumerate(calls)]


read_graph = StateGraph(Estado)
read_graph.add_node(
    "tools",
    ToolNode([consultar_inventra, faq_retriever, buscar_historico, buscar_documentos]),
)
read_graph.set_entry_point("tools")
read_graph.add_edge("tools", END)
demo_reads = read_graph.compile()


def demo_node(state):
    text = state["messages"][-1].content
    blocked = verificar(text)
    if blocked:
        return {
            "messages": [{"role": "assistant", "content": blocked["mensagem"]}],
            "agentes_chamados": ["demo:filtro_hardcoded"],
        }
    normalized = normalize(text)
    from iai.app.core.assistant.artifacts import (
        KITCHEN_MESSAGE,
        SALES_MESSAGE,
        unsupported_kitchens,
        unsupported_sales,
    )

    if unsupported_sales(text):
        return {
            "messages": [AIMessage(content=SALES_MESSAGE)],
            "agentes_chamados": ["demo:dominio_indisponivel"],
        }
    if unsupported_kitchens(text):
        return {
            "messages": [AIMessage(content=KITCHEN_MESSAGE)],
            "agentes_chamados": ["demo:escopo_cozinha"],
        }
    for word, value in {
        "dez": "10",
        "quarenta": "40",
        "cinco": "5",
        "dois": "2",
        "sete": "7",
    }.items():
        normalized = re.sub(rf"\b{word}\b", value, normalized)
    movements = re.findall(
        r"(\d+(?:[.,]\d+)?)\s*(?:kg|quilos?|quilogramas?)\s+(?:de\s+)?([a-z]+)",
        normalized,
    )
    consume = any(
        word in normalized
        for word in [
            "usei",
            "usando",
            "gastamos",
            "gastei",
            "baixa",
            "consumi",
            "consumo",
        ]
    )
    messages = []
    if consume and movements:
        for quantity, name in movements:
            products = (
                gateway().query(Query(resource="produtos", name=name), context()).data
            )
            products = (
                products.get("content", []) if isinstance(products, dict) else products
            )
            exact = [p for p in products if normalize(p["name"]) == name]
            if len(exact) != 1 or (exact[0].get("unit") or {}).get("symbol") != "kg":
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"Confirme produto e unidade para {name}.",
                    }
                )
                continue
            lots = (
                gateway()
                .query(Query(resource="lotes", product_id=exact[0]["id"]), context())
                .data
            )
            active = [lot for lot in lots if lot["status"] == "ACTIVE"]
            if len(active) != 1:
                messages.append(
                    {
                        "role": "assistant",
                        "content": f"Confirme qual lote de {name} deve ser consumido.",
                    }
                )
                continue
            args = {
                "batchId": active[0]["id"],
                "quantity": quantity.replace(",", "."),
                "justification": "Consumo informado pelo usuário (interpretação DEMO).",
            }
            outcome = PROPOSAL_TOOLS[Operation.CONSUME].invoke(args)
            messages.append(ToolMessage(content=outcome, tool_call_id=f"demo-{name}"))
        messages.append(
            {
                "role": "assistant",
                "content": "Pedidos validados, mas não executados (sem confirmação humana).",
            }
        )
        return {
            "messages": messages,
            "agentes_chamados": ["demo:estoquista", "demo:propostas"],
        }
    planned = AIMessage(content="", tool_calls=plan_demo_reads(text))
    outcomes = [
        m
        for m in demo_reads.invoke(
            {"messages": [planned]}, config={"max_concurrency": 4}
        )["messages"]
        if m.type == "tool"
    ]
    answer = "\n\n".join(f"{m.name}: {m.content}" for m in outcomes)
    return {
        "messages": [planned, *outcomes, AIMessage(content=answer)],
        "agentes_chamados": ["demo:consulta_ou_faq"],
    }


graph = StateGraph(Estado)
graph.add_node("demo", demo_node)
graph.set_entry_point("demo")
graph.add_edge("demo", END)
demo_graph = graph.compile()
