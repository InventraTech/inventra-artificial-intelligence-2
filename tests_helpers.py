"""Helpers exclusivos da suíte local (não usados pela aplicação)."""

from pydantic import SecretStr

from iai.app.core.infra.security import DEMO_IDENTITIES, RequestContext
from iai.app.core.inventra.business import MockGateway
from iai.app.core.inventra.domain import ROLE_OPERATIONS, Action


def context_for(token="demo-estoquista"):
    return RequestContext(DEMO_IDENTITIES[token], SecretStr(token))


def approved(operation, parameters):
    """Executa direto no adaptador MOCK (sem fluxo de confirmação humana).

    Serve só para testar as regras de negócio do mock; devolve {"status", "result"}.
    """
    token = next(
        token
        for token, role in (
            ("demo-estoquista", "ESTOQUISTA"),
            ("demo-comprador", "COMPRADOR"),
            ("demo-supervisor", "SUPERVISOR"),
        )
        if operation in ROLE_OPERATIONS[role]
    )
    ctx = context_for(token)
    outcome = MockGateway().execute(
        Action(operation=operation, parameters=parameters),
        {"user_id": ctx.identity.user_id, "kitchen_id": ctx.identity.kitchen_id},
        ctx,
        f"teste:{operation}:{id(parameters)}",
    )
    return {
        "status": "CONCLUIDA" if outcome.ok else "FALHOU",
        "result": outcome.model_dump(mode="json"),
    }


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
