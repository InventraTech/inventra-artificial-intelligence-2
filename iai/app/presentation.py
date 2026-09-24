"""Only tool/FAQ facts reach the user; model prose cannot assert a database success."""
import json
from pathlib import Path

from iai.app.config import FAQ_PATH

LABELS = {
    "id_product": "Produto ID", "product_name": "Produto", "product_brand": "Marca",
    "id_batch": "Lote ID", "batch_number": "Lote", "current_quantity": "Saldo",
    "total_current_quantity": "Saldo total", "unit_symbol": "Unidade",
    "expiration_date": "Validade", "nearest_expiration_date": "Próxima validade",
    "expiration_status": "Situação da validade", "days_until_expiration": "Dias até vencer",
    "supplier_name": "Fornecedor", "id_supplier": "Fornecedor ID", "category_name": "Categoria",
    "kitchen_name": "Cozinha", "status": "Status", "stock_status": "Situação do estoque",
    "quantity": "Quantidade", "min_stock": "Estoque mínimo", "max_stock": "Estoque máximo",
    "unit_price": "Preço unitário", "batch_value": "Valor do lote",
    "id_requisition": "Requisição ID", "created_at": "Criado em", "reason": "Motivo",
}


def faq_entries(path: Path = FAQ_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as source:
        return [json.loads(line) for line in source if line.strip()]


def render_proposal(response: dict) -> str:
    data = response["data"]
    if data["operation"] == "withdrawal":
        product, batch = data["product"], data["batch"]
        lines = ["Revise a baixa de estoque:",
                 f"Produto: {product['name']} (ID {product['id_product']}); marca: {product['brand'] or 'não informada'}.",
                 f"Cozinha: {data['kitchen_name']}; lote: {batch['batch_number']} (ID {batch['id_batch']}).",
                 f"Consumo: {data['quantity']} {data['unit']}.",
                 f"Saldo: {batch['current_quantity']} → {data['balance_after']} {data['unit']}."]
    else:
        lines = [f"Revise a requisição de compra para {data['kitchen_name']}:"]
        for item in data["items"]:
            supplier = item["supplier"]
            lines.append(f"- {item['product']['name']} (ID {item['product_id']}): {item['quantity']} {item['product']['symbol']}; "
                         f"fornecedor: {supplier['legal_name'] if supplier else 'não informado'}; "
                         f"preço estimado unitário: {item['estimated_price'] if item['estimated_price'] is not None else 'não informado'}; "
                         f"observação: {item['note'] or 'não informada'}.")
        lines.extend([f"Motivo: {data['reason'] or 'não informado'}.", "Status inicial: UNDER_REVIEW."])
    lines.extend(["Nenhuma alteração foi executada.",
                  f"Para confirmar, envie: confirmar {response['confirmation_id']}",
                  f"Para cancelar, envie: cancelar {response['confirmation_id']}",
                  "A confirmação expira em dez minutos."])
    return "\n".join(lines)


def render_results(responses: list[dict]) -> str:
    if not responses:
        return "Não recebi dados verificáveis. Informe o produto, lote ou consulta desejada."
    # A proposal must be rendered in full, unaltered by the LLM.
    pending = [r for r in responses if r.get("code") == "CONFIRMATION_REQUIRED"]
    if pending:
        return render_proposal(pending[-1])
    sections = []
    for response in responses:
        if not response.get("success"):
            sections.append(response.get("message", "A operação não foi concluída."))
            if response.get("data"):
                sections.append(json.dumps(response["data"], ensure_ascii=False, default=str))
            continue
        if response.get("code") == "CANCELLED":
            sections.append(response["message"])
            continue
        data = response.get("data")
        if not data:
            sections.append("A consulta foi concluída e não encontrou resultados para esses filtros.")
            continue
        if response.get("code") == "COMPLETED":
            sections.append("Operação concluída e confirmada pelo banco.")
        if response.get("message"):
            sections.append(response["message"])
        for row in data if isinstance(data, list) else [data]:
            sections.append("; ".join(f"{LABELS.get(key, key)}: {value if value is not None else 'não informado'}"
                                      for key, value in row.items()))
        if response.get("truncated"):
            sections.append("Há mais resultados. Refine os filtros para consultar os demais.")
    return "\n\n".join(sections)
