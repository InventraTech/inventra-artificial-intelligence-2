"""Cliente REST TypeSafe e política tipada do juiz PRÉ-tool. Não gera texto.

Contrato: https://docs.typesafe.ai/api/system-one
HTTP direto reaproveita httpx; não é necessário adicionar SDK.
"""

import math
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

from iai.app.core.config import (
    JEV_APPROVAL_THRESHOLD,
    JEV_MODEL,
    JEV_TIMEOUT,
    TYPESAFE_API_KEY,
)
from iai.app.core.infra.privacy import remove_secrets

Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False, strict=True)]
MAX_ATTEMPTS = 3
POSITIVE = {
    "intent_alignment": "Does this candidate address the user's current request?",
    "tool_appropriateness": "Is this tool suitable for the intended task?",
    "tool_necessity": "Does this call provide information or prepare an action needed for the request?",
    "arguments_grounded": "Are the supplied factual arguments supported by the user or authorized evidence?",
    "agent_tool_compatible": "Is this tool listed as available for the requesting agent?",
}
RISKS = {
    "missing_user_information": "Does this task require an essential fact or business choice only the user can provide?",
    "business_rule_conflict": "Does this candidate violate an INVENTRA rule stated in the context?",
    "unsafe_modification": "Does this candidate attempt a business change without the required human confirmation?",
}
ROUTES = {
    "agent_repair": "Existing context or an authorized lookup can resolve the defect.",
    "user_input": "An essential fact or business choice must be provided by the user.",
    "reject": "The requested action cannot be performed legitimately.",
    "none": "No defect requires correction.",
}


class Noul(BaseModel):
    type: Literal["noul"]
    noul: Probability


class Choice(BaseModel):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, Probability]
    confidence: Probability

    @model_validator(mode="after")
    def valid_distribution(self):
        if (
            self.choice not in self.probabilities
            or not math.isclose(sum(self.probabilities.values()), 1, abs_tol=0.02)
            or self.probabilities[self.choice] < max(self.probabilities.values())
        ):
            raise ValueError("Distribuição Choice inválida.")
        return self


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="ignore")
    model: str = Field(pattern=r"^jev-[A-Za-z0-9_.-]+$", max_length=80)
    answers: dict[str, Annotated[Noul | Choice, Field(discriminator="type")]]
    usage: dict[str, int] = Field(default_factory=dict)


class Decision(BaseModel):
    decision: Literal["APPROVE", "RETRY_SPECIALIST", "ASK_USER", "REJECT"]
    checks: dict[str, float] = Field(default_factory=dict)
    failed_checks: list[str] = Field(default_factory=list)
    missing_field: str | None = None


class JevUnavailable(Exception):
    """Código seguro, nunca texto de SDK, corpo HTTP ou credencial."""


def questions(fields: list[str]):
    result: dict[str, dict] = {
        key: {"type": "noul", "instructions": question}
        for key, question in {**POSITIVE, **RISKS}.items()
    }
    result["repair_route"] = {
        "type": "choice",
        "instructions": "If the candidate is defective, which source can resolve the defect?",
        "criteria": ROUTES,
    }
    result["missing_field"] = {
        "type": "choice",
        "instructions": "Which argument needs clarification from the user, if any?",
        "criteria": {
            **{f: f"Argument {f} from the tool contract." for f in fields},
            "not_identified": "No specific user-provided argument is missing.",
        },
    }
    return result


def evaluate(state: dict) -> Evaluation:
    """Uma requisição, perguntas atômicas, sem retry de escrita ou fail-open."""
    if not TYPESAFE_API_KEY.get_secret_value():
        raise JevUnavailable("JEV_NOT_CONFIGURED")
    query = questions(list(state["tool"]["schema"].get("properties", {})))

    def redact(value):
        if isinstance(value, dict):
            return {key: redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str):
            return remove_secrets(value).replace(
                TYPESAFE_API_KEY.get_secret_value(), "[SEGREDO_REMOVIDO]"
            )
        return value

    try:
        with httpx.Client(timeout=JEV_TIMEOUT, follow_redirects=False) as client:
            response = client.post(
                "https://api.typesafe.ai/v1/systemone",
                headers={
                    "Authorization": f"Bearer {TYPESAFE_API_KEY.get_secret_value()}"
                },
                json={"model": JEV_MODEL, "state": redact(state), "questions": query},
            )
        if response.status_code != 200:
            raise JevUnavailable(f"JEV_HTTP_{response.status_code}")
        parsed = Evaluation.model_validate(response.json())
        if set(parsed.answers) != set(query):
            raise ValueError("Perguntas incompletas.")
        for key in (*POSITIVE, *RISKS):
            if not isinstance(parsed.answers[key], Noul):
                raise TypeError("Tipo incorreto.")
        for key in ("repair_route", "missing_field"):
            answer = parsed.answers[key]
            if not isinstance(answer, Choice) or set(answer.probabilities) != set(
                query[key]["criteria"]
            ):
                raise ValueError("Opções incorretas.")
        return parsed
    except JevUnavailable:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise JevUnavailable("JEV_INVALID_OR_UNAVAILABLE") from None


def policy(
    evaluation: Evaluation, attempt: int, threshold=JEV_APPROVAL_THRESHOLD
) -> Decision:
    """Combina probabilidades em Python; confidence não é P(verdade)."""
    checks = {}
    for key in (*POSITIVE, *RISKS):
        value = evaluation.answers.get(key)
        if not isinstance(value, Noul):
            raise JevUnavailable("JEV_INVALID_ANSWERS")
        checks[key] = value.noul
    failed = [k for k in POSITIVE if checks[k] < threshold]
    failed += [k for k in RISKS if checks[k] > 1 - threshold + 1e-12]
    if not failed:
        return Decision(decision="APPROVE", checks=checks)
    route = evaluation.answers.get("repair_route")
    field = evaluation.answers.get("missing_field")
    if not isinstance(route, Choice) or not isinstance(field, Choice):
        raise JevUnavailable("JEV_INVALID_DIAGNOSIS")
    confident = route.probabilities.get(route.choice, 0) >= threshold
    if (
        confident
        and route.choice == "user_input"
        and checks["missing_user_information"] >= threshold
    ):
        missing = (
            field.choice
            if field.choice != "not_identified"
            and field.probabilities[field.choice] >= threshold
            else None
        )
        return Decision(
            decision="ASK_USER",
            checks=checks,
            failed_checks=failed,
            missing_field=missing,
        )
    retry = confident and route.choice == "agent_repair" and attempt < MAX_ATTEMPTS
    return Decision(
        decision="RETRY_SPECIALIST" if retry else "REJECT",
        checks=checks,
        failed_checks=failed,
    )
