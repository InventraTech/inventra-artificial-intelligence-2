from fastapi import APIRouter, Query

from iai.app.core.infra.security import Authenticated
from iai.app.core.inventra.approvals import (
    create_proposal,
    decide,
    execute_approved,
    get_proposal,
    list_proposals,
    public,
)
from iai.app.core.inventra.domain import DecisionInput, ProposalInput

router = APIRouter(prefix="/approvals", tags=["aprovações"])


@router.post("", status_code=201)
def create(payload: ProposalInput, ctx: Authenticated):
    return create_proposal(payload, ctx)


@router.get("")
def listing(
    ctx: Authenticated,
    status: str | None = Query(None, max_length=50),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    return list_proposals(ctx, status, offset, limit)


@router.get("/inbox")
def inbox(ctx: Authenticated):
    # A inbox é pessoal; a listagem administrativa permanece disponível em GET /approvals.
    return list_proposals(ctx, "AGUARDANDO_APROVACAO", mine=True)


@router.get("/{identifier}")
def detail(identifier: str, ctx: Authenticated):
    return public(get_proposal(identifier, ctx))


@router.post("/{identifier}/decision")
def decision(identifier: str, payload: DecisionInput, ctx: Authenticated):
    return decide(identifier, payload, ctx)


@router.post("/{identifier}/execute")
def execute(identifier: str, ctx: Authenticated):
    # JWT fresco do solicitante; nunca armazenar token aguardando aprovação.
    return execute_approved(identifier, ctx)
