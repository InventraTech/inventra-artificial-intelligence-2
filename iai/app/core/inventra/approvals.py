"""Proposta imutável, decisão autenticada e execução única por compare-and-set Mongo."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi import HTTPException
from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from iai.app.core.config import BUSINESS_MODE, business_date
from iai.app.core.infra.security import RequestContext
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.business import gateway
from iai.app.core.inventra.domain import (
    ROLE_OPERATIONS,
    Action,
    DecisionInput,
    Operation,
    ProposalInput,
)

PENDING = "AGUARDANDO_APROVACAO"
TERMINAL = {"CONCLUIDA", "RECUSADA", "FALHOU", "EXPIRADA", "RESULTADO_INCERTO"}


def now():
    return datetime.now(UTC)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()


def public(document):
    return {"id": document["_id"], **{k: v for k, v in document.items() if k != "_id"}}


def snapshot(action, ctx):
    """Valida existência e captura o alvo antes da proposta e antes de executar."""
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


def create_proposal(payload: ProposalInput, ctx: RequestContext):
    if payload.action.operation not in ROLE_OPERATIONS.get(ctx.identity.role, set()):
        raise HTTPException(
            403,
            f"O perfil {ctx.identity.role} não pode propor {payload.action.operation.value}.",
        )
    col = get_db().approvals
    owner = ctx.identity.model_dump()
    action = payload.action.model_dump(mode="json")
    key = digest([owner["user_id"], owner["kitchen_id"], payload.idempotency_key])
    existing = col.find_one({"_id": key})
    if existing:
        if (
            existing["action"] != action
            or existing["justification"] != payload.justification
        ):
            raise HTTPException(409, "Chave idempotente já usada para outra proposta.")
        expire_pending()
        return public(col.find_one({"_id": key}))
    before = snapshot(payload.action, ctx)
    created = now()
    immutable = {
        "action": action,
        "requester": owner,
        "before": before,
        "justification": payload.justification,
    }
    doc = {
        "_id": key,
        "requester_id": owner["user_id"],
        "kitchen_id": owner["kitchen_id"],
        "idempotency_key": payload.idempotency_key,
        **immutable,
        "proposal_hash": digest(immutable),
        "status": PENDING,
        "created_at": created,
        "expires_at": created + timedelta(days=7),
        "decided_by": None,
        "decision": None,
        "decided_at": None,
        "result": None,
        "execution_error": None,
        "technical_executor": "iai",
        "business_mode": BUSINESS_MODE,
        "confirmation_policy": "requester",
        "notification": {"channel": "requester_inbox", "available": True},
        "audit": [
            {"event": "PROPOSTA_CRIADA", "at": created, "actor": owner["user_id"]}
        ],
    }
    try:
        col.insert_one(doc)
    except DuplicateKeyError:
        existing = col.find_one({"_id": key})
        if (
            existing["action"] != action
            or existing["justification"] != payload.justification
        ):
            raise HTTPException(409, "Chave idempotente em conflito.") from None
        doc = existing
    return public(doc)


def expire_pending():
    moment = now()
    get_db().approvals.update_many(
        {
            "status": "EM_EXECUCAO",
            "execution_started_at": {"$lte": moment - timedelta(minutes=10)},
        },
        {
            "$set": {
                "status": "RESULTADO_INCERTO",
                "execution_error": "Execução interrompida; exige reconciliação e não será repetida.",
            },
            "$push": {
                "audit": {"event": "RESULTADO_INCERTO", "at": moment, "actor": "iai"}
            },
        },
    )
    return get_db().approvals.update_many(
        {
            "status": {"$in": [PENDING, "APROVADA_PENDENTE_INTEGRACAO"]},
            "expires_at": {"$lte": moment},
        },
        {
            "$set": {"status": "EXPIRADA"},
            "$push": {"audit": {"event": "EXPIRADA", "at": moment, "actor": "iai"}},
        },
    )


def get_proposal(identifier, ctx):
    expire_pending()
    doc = get_db().approvals.find_one(
        {"_id": identifier, "kitchen_id": ctx.identity.kitchen_id}
    )
    if not doc or (
        ctx.identity.role != "SUPERVISOR"
        and doc["requester_id"] != ctx.identity.user_id
    ):
        raise HTTPException(404, "Solicitação não encontrada no seu escopo.")
    return doc


def list_proposals(ctx, status=None, offset=0, limit=20, *, mine=False):
    expire_pending()
    query = {"kitchen_id": ctx.identity.kitchen_id}
    if mine or ctx.identity.role != "SUPERVISOR":
        query["requester_id"] = ctx.identity.user_id
    if status:
        query["status"] = status
    rows = (
        get_db().approvals.find(query).sort("created_at", -1).skip(offset).limit(limit)
    )
    return {
        "items": [public(d) for d in rows],
        "total": get_db().approvals.count_documents(query),
    }


def decide(identifier: str, payload: DecisionInput, ctx: RequestContext):
    col = get_db().approvals
    doc = get_proposal(identifier, ctx)
    require_requester(doc, ctx)
    if payload.proposal_hash != doc["proposal_hash"]:
        raise HTTPException(409, "A decisão não corresponde à proposta imutável.")
    immutable = {k: doc[k] for k in ("action", "requester", "before", "justification")}
    if digest(immutable) != doc["proposal_hash"]:
        raise HTTPException(409, "Integridade da proposta inválida.")
    if doc["status"] != PENDING:
        if (
            doc["decision"] == payload.decision
            and doc["decided_by"] == ctx.identity.user_id
        ):
            return public(doc)  # clique duplicado não dispara execução
        raise HTTPException(409, "Solicitação já decidida ou expirada.")
    status = (
        "RECUSADA" if payload.decision == "reject" else "APROVADA_PENDENTE_INTEGRACAO"
    )
    moment = now()
    claimed = col.find_one_and_update(
        {
            "_id": identifier,
            "status": PENDING,
            "proposal_hash": payload.proposal_hash,
            "expires_at": {"$gt": moment},
        },
        {
            "$set": {
                "status": status,
                "decided_by": ctx.identity.user_id,
                "decision": payload.decision,
                "decision_reason": payload.reason,
                "decided_at": moment,
            },
            "$push": {
                "audit": {"event": status, "actor": ctx.identity.user_id, "at": moment}
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        raise HTTPException(409, "Solicitação foi decidida ou expirou simultaneamente.")
    if payload.decision == "reject":
        return public(claimed)
    return execute_approved(identifier, ctx)


def execute_approved(identifier, ctx):
    col = get_db().approvals
    doc = get_proposal(identifier, ctx)
    require_requester(doc, ctx)
    if doc["status"] in TERMINAL or doc["status"] == "EM_EXECUCAO":
        return public(doc)
    if doc["status"] != "APROVADA_PENDENTE_INTEGRACAO":
        raise HTTPException(409, "A operação ainda não foi aprovada.")
    if doc.get("decided_by") != doc["requester_id"]:
        raise HTTPException(
            409, "Decisão legada de terceiro não confirma a ação. Crie nova proposta."
        )
    immutable = {k: doc[k] for k in ("action", "requester", "before", "justification")}
    if digest(immutable) != doc["proposal_hash"]:
        raise HTTPException(
            409, "Proposta alterada depois da decisão; execução bloqueada."
        )
    if doc["business_mode"] != BUSINESS_MODE:
        col.update_one(
            {"_id": identifier, "status": doc["status"]},
            {
                "$set": {
                    "execution_error": "INTEGRACAO_PENDENTE: proposta pertence a outro modo de negócio. Crie uma proposta no ambiente correto."
                }
            },
        )
        return public(col.find_one({"_id": identifier}))
    if BUSINESS_MODE == "spring" and doc["action"]["operation"] in {
        Operation.EDIT_COUNT,
        Operation.REMOVE_ITEM,
    }:
        col.update_one(
            {"_id": identifier, "status": doc["status"]},
            {
                "$set": {
                    "execution_error": "INTEGRACAO_PENDENTE: edição de contagem ou remoção lógica de item não disponível."
                }
            },
        )
        return public(col.find_one({"_id": identifier}))
    action = Action.model_validate(doc["action"])
    # Os dados aprovados não mudam. Alvo alterado exige nova proposta/decisão.
    try:
        before = snapshot(action, ctx)
    except HTTPException as exc:
        # Indisponibilidade antes do envio não perde uma decisão já registrada.
        col.update_one(
            {"_id": identifier, "status": doc["status"]},
            {
                "$set": {
                    "execution_error": f"VALIDACAO_PENDENTE: {exc.detail}",
                    "status": "FALHOU"
                    if exc.status_code in {400, 404, 409}
                    else doc["status"],
                }
            },
        )
        return public(col.find_one({"_id": identifier}))
    if digest(before) != digest(doc["before"]):
        col.update_one(
            {"_id": identifier, "status": doc["status"]},
            {
                "$set": {
                    "status": "FALHOU",
                    "execution_error": "CONFLITO: registro alterado desde a proposta.",
                }
            },
        )
        return public(col.find_one({"_id": identifier}))
    claimed = col.find_one_and_update(
        {
            "_id": identifier,
            "status": "APROVADA_PENDENTE_INTEGRACAO",
            "expires_at": {"$gt": now()},
        },
        {
            "$set": {"status": "EM_EXECUCAO", "execution_started_at": now()},
            "$push": {"audit": {"event": "EM_EXECUCAO", "at": now(), "actor": "iai"}},
        },
        return_document=ReturnDocument.AFTER,
    )
    if not claimed:
        return public(get_proposal(identifier, ctx))
    try:
        outcome = gateway().execute(action, doc["requester"], ctx, identifier)
        state = (
            "CONCLUIDA"
            if outcome.ok
            else "RESULTADO_INCERTO"
            if outcome.code == "RESULTADO_INCERTO"
            else "FALHOU"
        )
        updates = {
            "status": state,
            "result": outcome.model_dump(mode="json"),
            "execution_error": None if outcome.ok else outcome.code,
            "executed_at": now(),
        }
    except Exception as exc:  # noqa: BLE001 - efeito externo incerto deve bloquear repetição
        # Não repetir: a falha pode ter ocorrido após a gravação externa.
        state = "RESULTADO_INCERTO"
        updates = {
            "status": state,
            "execution_error": "Falha após iniciar execução; requer reconciliação.",
            "executed_at": now(),
            "exception_type": type(exc).__name__,
        }
    col.update_one(
        {"_id": identifier, "status": "EM_EXECUCAO"},
        {
            "$set": updates,
            "$push": {"audit": {"event": state, "at": now(), "actor": "iai"}},
        },
    )
    return public(col.find_one({"_id": identifier}))


def require_requester(doc, ctx):
    """Identidade e permissões atuais, inclusive em decisões/execuções repetidas."""
    if (
        doc["requester_id"] != ctx.identity.user_id
        or doc["kitchen_id"] != ctx.identity.kitchen_id
    ):
        raise HTTPException(
            403, "Somente o solicitante pode confirmar ou executar esta ação."
        )
    if Operation(doc["action"]["operation"]) not in ROLE_OPERATIONS.get(
        ctx.identity.role, set()
    ):
        raise HTTPException(403, "Seu perfil atual não permite esta operação.")
