"""Identidade validada fora dos modelos. JWT vive só no contexto da requisição."""

from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Annotated

import httpx
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from iai.app.core.config import API_URL, AUTH_MODE, HTTP_TIMEOUT


class Identity(BaseModel):
    model_config = ConfigDict(frozen=True)
    user_id: str = Field(min_length=1, max_length=150)
    kitchen_id: int = Field(gt=0, strict=True)
    role: str
    name: str
    mocked: bool = False


@dataclass(frozen=True)
class RequestContext:
    identity: Identity
    token: SecretStr = field(repr=False)
    turn_id: str = ""
    session_id: str = ""


current_context: ContextVar[RequestContext | None] = ContextVar(
    "iai_request", default=None
)
bearer = HTTPBearer(auto_error=False)

# Perfis fixos de demonstração; não aceitamos user_id/papel/cozinha enviados pelo cliente.
DEMO_IDENTITIES = {
    "demo-estoquista": Identity(
        user_id="demo-estoquista",
        kitchen_id=1,
        role="ESTOQUISTA",
        name="Estoquista demonstração",
        mocked=True,
    ),
    "demo-comprador": Identity(
        user_id="demo-comprador",
        kitchen_id=1,
        role="COMPRADOR",
        name="Comprador demonstração",
        mocked=True,
    ),
    "demo-supervisor": Identity(
        user_id="demo-supervisor",
        kitchen_id=1,
        role="SUPERVISOR",
        name="Supervisor demonstração",
        mocked=True,
    ),
    "demo-outra-cozinha": Identity(
        user_id="demo-outra-cozinha",
        kitchen_id=2,
        role="SUPERVISOR",
        name="Outra cozinha demo",
        mocked=True,
    ),
}


def context() -> RequestContext:
    value = current_context.get()
    if value is None:
        raise HTTPException(401, "Contexto autenticado ausente.")
    return value


def authenticate(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> RequestContext:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            401, "Token Bearer necessário.", headers={"WWW-Authenticate": "Bearer"}
        )
    token = credentials.credentials
    if AUTH_MODE == "mock":
        identity = DEMO_IDENTITIES.get(token)
        if identity is None:
            raise HTTPException(401, "Token demonstrativo inválido; autenticação MOCK.")
    else:
        try:
            response = httpx.get(
                f"{API_URL}/api/users/me",
                headers={"Authorization": f"Bearer {token}"},
                timeout=HTTP_TIMEOUT,
                follow_redirects=False,
            )
            if response.status_code in (401, 403):
                raise HTTPException(
                    response.status_code, "Identidade recusada pela API."
                )
            if response.status_code != 200:
                raise HTTPException(503, "Serviço de identidade indisponível.")
            data = response.json()
            if data.get("active") is not True or not data.get("kitchen"):
                raise HTTPException(403, "Usuário inativo ou sem cozinha.")
            role = data["profile"]["accessType"].upper()
            if role not in {"SUPERVISOR", "ESTOQUISTA", "COMPRADOR"}:
                raise HTTPException(403, "Perfil não autorizado.")
            from iai.app.core.infra.privacy import remove_secrets

            identity = Identity(
                user_id=data["id"],
                kitchen_id=data["kitchen"]["id"],
                role=role,
                name=remove_secrets(data["name"]),
            )
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise HTTPException(503, "Não foi possível validar a identidade.") from None
    return RequestContext(identity, SecretStr(token))


def require_supervisor(ctx: RequestContext):
    if ctx.identity.role != "SUPERVISOR":
        raise HTTPException(403, "A decisão exige Supervisor da mesma cozinha.")


Authenticated = Annotated[RequestContext, Depends(authenticate)]
