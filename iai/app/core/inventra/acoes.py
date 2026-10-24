"""Ações de escrita: validação e PONTO DE ENTRADA do human in the loop (HITL).

STATUS: o HITL ainda NÃO está implementado. Hoje uma ação pedida pelo agente (`propor_*`) é
validada contra o negócio e devolvida como "não executada". Nada é gravado e nada é executado.

O que já existe e o HITL pode reaproveitar:
- `snapshot(action, ctx)`: confere perfil/IDs/estado dos registros e devolve a foto deles.
- `domain.Action` / `domain.ROLE_OPERATIONS`: operações, parâmetros tipados e quem pode pedir cada uma.
- `business.gateway().execute(action, requester, ctx, key)`: executa a ação no Spring (ou no mock).
  Ele usa o JWT do contexto: quem confirmar precisa executar com a identidade do solicitante.

O que falta (para quem implementar o HITL), tudo dentro de `solicitar_confirmacao`:
1. guardar a ação pendente (ex.: coleção Mongo) com idempotência;
2. avisar/pedir a confirmação de uma pessoa (chat, app, aprovação do Supervisor etc.);
3. ao confirmar, revalidar com `snapshot` e chamar `gateway().execute(...)` UMA vez;
4. devolver ao agente o estado real (pendente/confirmada/recusada/executada).
O gate do JEV (`tool_gate.py`) continua julgando as `propor_*` antes de elas chegarem aqui.
"""

import hashlib
import json
from decimal import Decimal

from fastapi import HTTPException

from iai.app.core.config import business_date
from iai.app.core.infra.privacy import remove_secrets
from iai.app.core.infra.security import RequestContext
from iai.app.core.inventra.business import gateway
from iai.app.core.inventra.domain import ROLE_OPERATIONS, Action, Operation

SEM_HITL = "NAO_EXECUTADA_SEM_CONFIRMACAO_HUMANA"


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def snapshot(action, ctx):
    """Valida existência/estado dos registros citados e devolve uma foto deles (campo `before`)."""
    from iai.app.core.inventra.domain import Query

    p = action.parameters
    targets = []
    mapping = {
        "productId": ("produtos", "id"),
        "supplierId": ("fornecedores", "id"),
        "suggestedSupplierId": ("fornecedores", "id"),
        "batchId": ("lotes", "id"),
        "requisitionId": ("requisicoes", "id"),
        "inventoryId": ("inventarios", "id"),
        "unitId": ("unidades", "id"),
        "categoryId": ("categorias", "id"),
    }
    for key, (resource, field) in mapping.items():
        if key not in p:
            continue
        q = Query(
            resource=resource,
            product_id=p[key] if key == "productId" else None,
            record_id=p[key] if key != "productId" else None,
            size=100,
        )
        outcome = gateway().query(q, ctx)
        if not outcome.ok:
            raise HTTPException(
                503
                if outcome.code in {"SERVICO_INDISPONIVEL", "RESPOSTA_INVALIDA"}
                else 403,
                outcome.message,
            )
        rows = (
            outcome.data.get("content", [])
            if isinstance(outcome.data, dict)
            else outcome.data
        )
        if isinstance(outcome.data, dict) and "id" in outcome.data:
            rows = [outcome.data]
        row = next((r for r in rows if r.get(field) == p[key]), None)
        if row is None:
            raise HTTPException(
                409 if outcome.incomplete else 404,
                f"{key} não localizado na consulta autorizada; confirme o registro.",
            )
        targets.append({"parameter": key, "record": row})
    for key, resource, parent in [
        ("itemId", "itens_requisicao", "requisitionId"),
        ("countId", "contagens", "inventoryId"),
    ]:
        if key in p:
            outcome = gateway().query(
                Query(resource=resource, record_id=p[parent], size=100), ctx
            )
            if not outcome.ok:
                raise HTTPException(
                    409, "Não foi possível validar o registro subordinado."
                )
            row = next((r for r in outcome.data if r.get("id") == p[key]), None)
            if not row:
                raise HTTPException(404, "Registro não pertence ao recurso informado.")
            targets.append({"parameter": key, "record": row})
    records = {t["parameter"]: t["record"] for t in targets}
    op = action.operation
    if op in {Operation.CREATE_BATCH, Operation.ADD_ITEM} and not records[
        "productId"
    ].get("active", True):
        raise HTTPException(409, "Produto desativado não recebe entrada/requisição.")
    if op in {Operation.CONSUME, Operation.RESTOCK, Operation.ADJUST}:
        batch = records["batchId"]
        if (
            batch.get("expirationDate")
            and batch["expirationDate"] < business_date().isoformat()
        ):
            raise HTTPException(409, "Lote vencido não pode ser movimentado.")
        if batch.get("status") in {"EXPIRED", "CANCELLED"}:
            raise HTTPException(409, "Lote indisponível.")
        if op == Operation.CONSUME and (
            batch.get("status") != "ACTIVE"
            or Decimal(p["quantity"]) > Decimal(str(batch["currentQuantity"]))
        ):
            raise HTTPException(409, "Lote não ativo ou saldo insuficiente.")
    if op in {Operation.ADD_ITEM, Operation.REMOVE_ITEM, Operation.CANCEL_REQUISITION}:
        # Toda requisição nasce em rascunho (DRAFT): é nesse estado que recebe/perde itens.
        allowed = (
            {"DRAFT", "UNDER_REVIEW", "APPROVED"}
            if op == Operation.CANCEL_REQUISITION
            else {"DRAFT"}
        )
        if records["requisitionId"]["status"] not in allowed:
            raise HTTPException(409, "Estado da requisição não permite a operação.")
    if (
        op in {Operation.ADD_COUNT, Operation.EDIT_COUNT}
        and records["inventoryId"]["status"] != "OPEN"
    ):
        raise HTTPException(409, "Contagens só podem mudar em inventário aberto.")
    if (
        op == Operation.EDIT_COUNT
        and records["countId"].get(
            "batchId", (records["countId"].get("batch") or {}).get("id")
        )
        != p["batchId"]
    ):
        raise HTTPException(409, "Contagem não corresponde ao lote informado.")
    if op == Operation.CREATE_INVENTORY:
        outcome = gateway().query(Query(resource="inventarios", status="OPEN"), ctx)
        if not outcome.ok:
            raise HTTPException(503, "Não foi possível validar inventários abertos.")
        if outcome.data:
            raise HTTPException(409, "Já existe um inventário aberto nesta cozinha.")
        targets.append({"parameter": "openInventory", "record": None})
    return targets


def solicitar_confirmacao(
    action: Action, justification: str, ctx: RequestContext
) -> dict:
    """PONTO DE ENTRADA DO HITL. Hoje: valida e devolve a ação como NÃO executada.

    Levanta HTTPException se o perfil não pode pedir a ação, se há segredo no texto ou se algum registro citado não
    existe/não está no estado exigido. Não grava nada e não executa nada.
    """
    if action.operation not in ROLE_OPERATIONS.get(ctx.identity.role, set()):
        raise HTTPException(
            403, f"O perfil {ctx.identity.role} não pode propor {action.operation.value}."
        )
    acao = action.model_dump(mode="json")
    texto = json.dumps({"action": acao, "justification": justification}, ensure_ascii=False)
    if texto != remove_secrets(texto):
        raise HTTPException(
            422, "Não inclua tokens ou senhas nos parâmetros ou na justificativa."
        )
    antes = snapshot(action, ctx)
    return {
        "id": digest({"action": acao, "user": ctx.identity.user_id})[:16],
        "status": SEM_HITL,
        "action": acao,
        "before": antes,
        "justification": justification,
        "requester_id": ctx.identity.user_id,
        "kitchen_id": ctx.identity.kitchen_id,
    }
