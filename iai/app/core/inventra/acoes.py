"""Entrada das tools de ação: reutiliza HITL durável, sem executar no chat.

Preserva o ponto de integração da main, o snapshot e a política de idempotência
já usada pelas propostas. Confirmação/execução continuam no serviço approvals.
"""

import json
from hashlib import sha256

from fastapi import HTTPException

from iai.app.core.infra.privacy import remove_secrets
from iai.app.core.infra.security import RequestContext
from iai.app.core.inventra.approvals import create_proposal, digest, snapshot
from iai.app.core.inventra.domain import Action, ProposalInput

__all__ = ["digest", "snapshot", "solicitar_confirmacao"]


def solicitar_confirmacao(
    action: Action, justification: str, ctx: RequestContext
) -> dict:
    serialized = json.dumps(
        {"action": action.model_dump(mode="json"), "justification": justification},
        ensure_ascii=False,
    )
    if serialized != remove_secrets(serialized):
        raise HTTPException(
            422, "Não inclua tokens ou senhas nos parâmetros ou na justificativa."
        )
    fingerprint = sha256(
        json.dumps(action.model_dump(mode="json"), sort_keys=True).encode()
    ).hexdigest()
    key = sha256(f"{ctx.turn_id}:{fingerprint}".encode()).hexdigest()
    return create_proposal(
        ProposalInput(action=action, justification=justification, idempotency_key=key),
        ctx,
    )
