"""Helpers exclusivos da suíte local (não usados pela aplicação)."""

import uuid

from pydantic import SecretStr

from iai.app.approvals import create_proposal, decide
from iai.app.domain import Action, DecisionInput, ProposalInput
from iai.app.security import DEMO_IDENTITIES, RequestContext


def context_for(token="demo-estoquista"):
    return RequestContext(DEMO_IDENTITIES[token], SecretStr(token))


def approved(operation, parameters):
    p = create_proposal(
        ProposalInput(
            action=Action(operation=operation, parameters=parameters),
            justification="Operação autorizada no teste",
            idempotency_key=str(uuid.uuid4()),
        ),
        context_for(),
    )
    return decide(
        p["id"],
        DecisionInput(decision="approve", proposal_hash=p["proposal_hash"]),
        context_for("demo-supervisor"),
    )
