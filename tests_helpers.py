"""Helpers exclusivos da suíte local (não usados pela aplicação)."""

import uuid

from pydantic import SecretStr

from iai.app.core.infra.security import DEMO_IDENTITIES, RequestContext
from iai.app.core.inventra.approvals import create_proposal, decide
from iai.app.core.inventra.domain import (
    ROLE_OPERATIONS,
    Action,
    DecisionInput,
    ProposalInput,
)


def context_for(token="demo-estoquista"):
    return RequestContext(DEMO_IDENTITIES[token], SecretStr(token))


def approved(operation, parameters):
    # Solicita com o perfil mais simples que pode propor a operação.
    requester = next(
        token
        for token, role in (
            ("demo-estoquista", "ESTOQUISTA"),
            ("demo-comprador", "COMPRADOR"),
            ("demo-supervisor", "SUPERVISOR"),
        )
        if operation in ROLE_OPERATIONS[role]
    )
    p = create_proposal(
        ProposalInput(
            action=Action(operation=operation, parameters=parameters),
            justification="Operação autorizada no teste",
            idempotency_key=str(uuid.uuid4()),
        ),
        context_for(requester),
    )
    return decide(
        p["id"],
        DecisionInput(decision="approve", proposal_hash=p["proposal_hash"]),
        context_for(requester),
    )


def jev_evaluation(*, failed=None, route="none", missing="not_identified", fields=()):
    """Resposta TypeSafe roteirizada. Não avalia a qualidade do modelo externo."""
    from iai.app.core.assistant import judge

    answers = {
        key: {"type": "noul", "noul": 0.99 if key in judge.POSITIVE else 0.01}
        for key in (*judge.POSITIVE, *judge.RISKS)
    }
    for key, probability in (failed or {}).items():
        answers[key]["noul"] = probability
    for key, choice, options in (
        ("repair_route", route, judge.ROUTES),
        ("missing_field", missing, {**dict.fromkeys(fields), "not_identified": None}),
    ):
        answers[key] = {
            "type": "choice",
            "choice": choice,
            "confidence": 1.0,
            "probabilities": {option: float(option == choice) for option in options},
        }
    return judge.Evaluation(
        model="jev-1.13.0",
        answers=answers,
        usage={"input_tokens": 10, "output_tokens": 2},
    )


def jev_fidelidade(*, failed=None):
    """Resposta TypeSafe roteirizada para o juiz de texto (4 critérios, todos aprovados por padrão)."""
    from iai.app.core.assistant import judge

    answers = {key: {"type": "noul", "noul": 0.99} for key in judge.FIDELIDADE}
    for key, probability in (failed or {}).items():
        answers[key]["noul"] = probability
    return judge.Evaluation(
        model="jev-1.13.0",
        answers=answers,
        usage={"input_tokens": 10, "output_tokens": 2},
    )
