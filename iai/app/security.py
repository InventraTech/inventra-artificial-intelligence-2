"""Only the identity source is provisional. Roles/kitchen always come from PostgreSQL."""
import os
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException, Request

from iai.app.domain import DomainError


@dataclass(frozen=True)
class ExecutionContext:
    user_id: str
    session_id: str


execution_context: ContextVar[ExecutionContext | None] = ContextVar("execution_context", default=None)


def current_context() -> ExecutionContext:
    context = execution_context.get()
    if context is None:
        raise DomainError("UNAUTHENTICATED", "Contexto de usuário não foi fornecido pelo servidor.")
    return context


def identity_from_backend(request: Request) -> str:
    """Integration seam: trusted middleware must set request.state.authenticated_user_id.

    No client header/body is trusted. A fixed local mock can be explicitly enabled.
    """
    value = getattr(request.state, "authenticated_user_id", None)
    if value is None and os.getenv("IDENTITY_MODE", "backend") == "mock":
        if os.getenv("APP_ENV", "development") == "production":
            raise HTTPException(503, "Identidade mock não é permitida em produção.")
        value = os.getenv("MOCK_USER_ID")
    if not value:
        raise HTTPException(401, "Integração de identidade pendente: forneça o usuário pelo backend.")
    try:
        return str(UUID(str(value)))
    except ValueError:
        raise HTTPException(401, "Identificador de usuário inválido.") from None


ANALYTICS = {"painel", "desperdicio", "divergencias", "valor_categoria", "ranking_requisicoes",
             "saldo_categoria", "tendencia_categoria", "movimentacao_diaria"}
REQUISITIONS = {"requisicoes", "requisicoes_pendentes", "itens_requisicao"}


def authorize(repo, user_id: str, *, operation: str = "read", resource: str = "",
              lock: bool = False) -> dict:
    try:
        UUID(user_id)
    except ValueError:
        raise DomainError("UNAUTHENTICATED", "Identificador de usuário inválido.") from None
    user = repo.principal(user_id, lock=lock)
    if not user or not user["active"]:
        raise DomainError("FORBIDDEN", "Usuário inexistente ou inativo.")
    if not user["kitchen_id"] or not user["kitchen_active"]:
        raise DomainError("FORBIDDEN", "Usuário sem vínculo com uma cozinha ativa.")
    # Observed access_type families: COMPRADOR_*, ESTOQUISTA_*, SUPERVISOR_*.
    role = user["access_type"].split("_", 1)[0].lower()
    if role not in {"comprador", "estoquista", "supervisor"}:
        raise DomainError("FORBIDDEN", "Perfil sem permissão para as ferramentas do Inventra.")
    allowed = operation == "read"
    if resource in ANALYTICS:
        allowed = allowed and role == "supervisor"
    if resource in REQUISITIONS:
        allowed = allowed and role in {"comprador", "supervisor"}
    if operation == "withdrawal":
        allowed = role == "estoquista"
    elif operation == "purchase":
        allowed = role == "comprador"
    if not allowed:
        raise DomainError("FORBIDDEN", "Seu perfil não permite esta operação.")
    return {**user, "role": role}
