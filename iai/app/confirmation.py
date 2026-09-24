"""Durable, single-use confirmation. Never exposed as an LLM-callable tool.

Mongo is the existing session store, not a second stock audit. An interrupted
PostgreSQL commit is deliberately NOT retried: there is no distributed transaction.
"""
from datetime import datetime, timedelta, timezone
from secrets import token_urlsafe

from pymongo import ReturnDocument

from iai.app.domain import DomainError, failure, result
from iai.app.security import ExecutionContext
from iai.app.services import service


class ConfirmationStore:
    def __init__(self, collection):
        self.collection = collection

    def save(self, doc_id: str, context: ExecutionContext, proposal: dict) -> dict:
        pending = {**proposal, "id": token_urlsafe(24), "status": "pending",
                   "expires_at": datetime.now(timezone.utc) + timedelta(minutes=10)}
        updated = self.collection.update_one(
            {"_id": doc_id, "user_id": context.user_id, "session_id": context.session_id,
             "summary": {"$in": ["", None]},
             "pending_operation.status": {"$ne": "executing"}},
            {"$set": {"pending_operation": pending}})
        if not updated.matched_count:
            raise DomainError("CONFLICT", "Há uma operação em execução nesta sessão.")
        return result(pending["snapshot"], code="CONFIRMATION_REQUIRED", confirmation_id=pending["id"],
                      expires_at=pending["expires_at"].isoformat(),
                      message="Revise o resumo. A operação ainda não foi executada.")

    def confirm(self, context: ExecutionContext, confirmation_id: str, approved: bool,
                inventory_service=service) -> dict:
        selector = {"user_id": context.user_id, "session_id": context.session_id,
                    "pending_operation.id": confirmation_id}
        doc = self.collection.find_one(selector)
        if not doc:
            raise DomainError("NOT_FOUND", "Confirmação não encontrada para este usuário e sessão.")
        pending = doc["pending_operation"]
        if pending["status"] in {"completed", "cancelled", "failed"}:
            return pending["result"]
        if pending["status"] != "pending":
            raise DomainError("INDETERMINATE", "Operação em execução ou sem resultado confirmado. Consulte o histórico antes de repetir.")
        now = datetime.now(timezone.utc)
        if pending["expires_at"].replace(tzinfo=timezone.utc) <= now:
            raise DomainError("EXPIRED_CONFIRMATION", "Resumo expirado. Solicite uma nova proposta.")
        claimed = self.collection.find_one_and_update(
            {**selector, "pending_operation.status": "pending",
             "summary": {"$in": ["", None]},
             "pending_operation.expires_at": {"$gt": now}},
            {"$set": {"pending_operation.status": "executing"}},
            return_document=ReturnDocument.AFTER)
        if not claimed:
            raise DomainError("CONFLICT", "Esta confirmação já foi utilizada.")
        status = "cancelled"
        response = result(None, code="CANCELLED", message="Operação cancelada; nenhuma alteração realizada.")
        if approved:
            try:
                response = inventory_service.execute(context, claimed["pending_operation"])
                status = "completed"
            except DomainError as exc:
                # Connection loss during COMMIT cannot prove rollback. Never enable retry.
                if exc.code in {"DATABASE_UNAVAILABLE", "DATABASE_ERROR"}:
                    raise DomainError("INDETERMINATE", "Não foi possível confirmar o resultado. Consulte as movimentações/requisições antes de repetir.") from exc
                response, status = failure(exc), "failed"
            except Exception as exc:
                raise DomainError("INDETERMINATE", "Não foi possível confirmar o resultado; não repita sem consultar o estado.") from exc
        saved = self.collection.update_one(
            {**selector, "pending_operation.status": "executing"},
            {"$set": {"pending_operation.status": status, "pending_operation.result": response}})
        if not saved.matched_count:
            raise DomainError("INDETERMINATE", "O resultado não pôde ser salvo na sessão. Consulte o estado antes de repetir.")
        return response


def prepare_confirmation(context: ExecutionContext, proposal: dict) -> dict:
    from iai.app.memory import col_sessoes, doc_id_da_sessao, iniciar_sessao
    iniciar_sessao(context.session_id, context.user_id)
    doc_id = doc_id_da_sessao(context.session_id, context.user_id)
    if doc_id is None:
        raise DomainError("SESSION_UNAVAILABLE", "Não foi possível identificar a sessão da proposta.")
    return ConfirmationStore(col_sessoes).save(doc_id, context, proposal)
