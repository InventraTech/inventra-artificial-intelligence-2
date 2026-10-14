"""Intérprete determinístico LIMITADO, exclusivamente demonstrativo e sem LLM.

O atendimento natural completo usa agentes live. Esta simulação permite provar o
fluxo de aprovação offline; não é apresentada como capacidade geral de linguagem.
"""

import re

from langchain_core.messages import ToolMessage
from langgraph.graph import END, StateGraph

from iai.app.business import gateway
from iai.app.domain import Operation, Query
from iai.app.guardrail import verificar
from iai.app.retrieval import normalize
from iai.app.schemas import Estado
from iai.app.security import context
from iai.app.tools.faq import faq_retriever
from iai.app.tools.operational import PROPOSAL_TOOLS


def demo_node(state):
    text = state["messages"][-1].content
    blocked = verificar(text)
    if blocked:
        return {
            "messages": [{"role": "assistant", "content": blocked["mensagem"]}],
            "agentes_chamados": ["demo:filtro_hardcoded"],
        }
    normalized = normalize(text)
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
                "content": "Confira as propostas demonstrativas; falta de dados não é execução.",
            }
        )
        return {
            "messages": messages,
            "agentes_chamados": ["demo:estoquista", "demo:propostas"],
        }
    resource = None
    if any(term in normalized for term in ["abaixo", "minimo", "em falta"]):
        resource = "estoque_minimo"
    elif any(term in normalized for term in ["venc", "validade"]):
        resource = "validade"
    elif "requis" in normalized or "pedido" in normalized:
        resource = "requisicoes"
    elif "fornecedor" in normalized:
        resource = (
            "fornecedores_recorrentes" if "recorrent" in normalized else "fornecedores"
        )
    elif "produto" in normalized:
        resource = "produtos"
    elif "inventario" in normalized:
        resource = "inventarios"
    elif "alerta" in normalized:
        resource = "alertas"
    elif "lote" in normalized:
        resource = "lotes"
    if resource and not any(
        term in normalized for term in ["como ", "o que e ", "quem pode"]
    ):
        found_days = re.search(r"(\d+)\s*dias", normalized)
        q = Query(
            resource=resource,
            days=int(found_days[1]) if found_days else 7,
            status="UNDER_REVIEW"
            if resource == "requisicoes" and "pendent" in normalized
            else None,
        )
        outcome = gateway().query(q, context())
        answer = outcome.model_dump_json()
    else:
        answer = faq_retriever.invoke(text)
    return {
        "messages": [{"role": "assistant", "content": answer}],
        "agentes_chamados": ["demo:consulta_ou_faq"],
    }


graph = StateGraph(Estado)
graph.add_node("demo", demo_node)
graph.set_entry_point("demo")
graph.add_edge("demo", END)
demo_graph = graph.compile()
