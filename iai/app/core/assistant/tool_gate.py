"""Fronteira pré-execução: candidato pendente -> validação local -> permissão de uso único.

O middleware intercepta o ToolNode interno de create_agent. A proteção nas funções
também bloqueia chamadas diretas feitas durante um turno live sem esse middleware.
REST/MCP e o demo determinístico não são turnos de agentes live.

O JEV não fica aqui: ele julga o turno depois que o especialista termina e antes de a resposta
chegar ao usuário (revisao.py). Isso é seguro porque toda escrita é uma proposta pendente: nada
muda no estoque até o solicitante aprovar, e uma proposta reprovada é cancelada antes de aparecer.
"""

import inspect
import json
import logging
import operator
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from hashlib import sha256
from threading import RLock
from time import perf_counter
from typing import Annotated, Literal
from uuid import uuid4

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain.agents.middleware.types import hook_config
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command
from pydantic import BaseModel, Field, ValidationError

from iai.app.core.assistant import eventos_do_fluxo, judge
from iai.app.core.config import (
    GEMINI_API_KEY,
    GROQ_API_KEY,
    TYPESAFE_API_KEY,
)
from iai.app.core.infra.privacy import remove_secrets
from iai.app.core.infra.security import context
from iai.app.core.inventra.domain import ROLE_OPERATIONS, Operation

logger = logging.getLogger(__name__)
SAFE_STOP = "Não consegui confirmar os dados para concluir esse pedido. Essa etapa não foi realizada; reformule o pedido para continuar."
MOTIVOS_DE_RECUSA = {
    "ROLE_NOT_ALLOWED": "o seu perfil não pode fazer essa operação.",
    "TOOL_NOT_ALLOWED": "essa ferramenta não está disponível para este pedido.",
    "SECRET_IN_ARGUMENTS": "o pedido contém algo que parece senha ou token; remova e tente de novo.",
    "arguments_grounded": "não consegui identificar com certeza o produto, fornecedor ou lote citado. Diga o nome completo (e a marca ou a unidade).",
    "business_rule_conflict": "esse pedido conflita com uma regra do Inventra.",
    "unsafe_modification": "essa alteração precisa de confirmação humana.",
    "missing_user_information": "falta uma informação que só você sabe.",
}


def mensagem_de_recusa(failed_checks):
    """Texto para o usuário quando o gate para a etapa; nunca traz argumentos nem a conversa."""
    for check in failed_checks or []:
        if check in MOTIVOS_DE_RECUSA:
            return f"Não consegui concluir esse pedido: {MOTIVOS_DE_RECUSA[check]} Essa etapa não foi realizada."
    return SAFE_STOP


UNAVAILABLE = "A consulta está temporariamente indisponível. Essa etapa não foi realizada; tente novamente mais tarde."
PARALLEL_READS = frozenset(
    {"consultar_inventra", "faq_retriever", "buscar_historico", "buscar_documentos"}
)
FIELD_LABELS = {
    "quantity": "a quantidade e a unidade do produto",
    "newQuantity": "a quantidade final desejada e sua unidade",
    "physicalQuantity": "a quantidade física contada e sua unidade",
    "initialQuantity": "a quantidade inicial e sua unidade",
    "productId": "qual produto você deseja utilizar (nome, marca ou unidade)",
    "batchId": "qual lote você deseja utilizar",
    "supplierId": "qual fornecedor você deseja utilizar",
    "suggestedSupplierId": "qual fornecedor você deseja sugerir",
    "expirationDate": "a data de validade",
    "entryDate": "a data de entrada",
    "unitPrice": "o preço por unidade",
    "name": "o nome do produto",
    "cnpj": "o CNPJ do fornecedor",
    "legalName": "a razão social do fornecedor",
}


def merge_calls(previous, updates):
    return {**(previous or {}), **(updates or {})}


class GateState(AgentState, total=False):  # type: ignore[call-arg]  # AgentState é TypedDict; imports externos são omitidos no mypy local.
    jev_calls: Annotated[dict[str, dict], merge_calls]
    jev_events: Annotated[list[dict], operator.add]
    jev_batch: list[str]
    jev_attempt: int
    jev_halt: str | None
    jev_stops: Annotated[list[str], operator.add]
    jev_parallel: bool
    jev_evidence: list[dict]


class ToolCandidate(BaseModel):
    id: str
    call_id: str
    agent: str
    tool: str
    arguments: dict
    fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    user_id: str
    kitchen_id: int = Field(gt=0)
    turn_id: str
    attempt: int = Field(ge=1, le=judge.MAX_ATTEMPTS)
    status: Literal["PENDING_JEV"]
    created_at: datetime


@dataclass
class LiveScope:
    lock: RLock = field(default_factory=RLock)
    executed: set[str] = field(default_factory=set)
    evidence: list[dict] = field(default_factory=list)
    redaction_salt: str = field(default_factory=lambda: uuid4().hex)


scope_var: ContextVar[LiveScope | None] = ContextVar(
    "iai_live_tool_scope", default=None
)
permit_var: ContextVar[dict | None] = ContextVar("iai_jev_permit", default=None)


@contextmanager
def live_tool_scope():
    marker = scope_var.set(LiveScope())
    try:
        yield scope_var.get()
    finally:
        scope_var.reset(marker)


def resumo_de_erro(exc):
    """Campos e tipos de erro de validação, sem valores (podem conter dados do usuário)."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(parte) for parte in erro['loc'])}:{erro['type']}"
            for erro in exc.errors()
        )[:300]
    return type(exc).__name__


def canonical(tool, args):
    schema = tool.get_input_schema()
    properties = schema.model_json_schema().get("properties", {})
    if set(args) - set(properties):
        raise ValueError("Argumentos não declarados.")
    return schema.model_validate(args).model_dump(mode="json", exclude={"config"})


def fingerprint(name, args):
    return sha256(
        json.dumps([name, args], sort_keys=True, default=str).encode()
    ).hexdigest()


def protect_tool(tool):
    """Preserva schema e finalidade; só exige permit dentro de turno protegido."""
    original = tool.func
    if getattr(original, "_iai_protected", False):
        return tool

    @wraps(original)
    def guarded(*args, **kwargs):
        if scope_var.get() is not None:
            bound = inspect.signature(original).bind(*args, **kwargs)
            values = {}
            for key, value in bound.arguments.items():
                if key == "config":
                    continue
                if (
                    inspect.signature(original).parameters[key].kind
                    == inspect.Parameter.VAR_KEYWORD
                ):
                    values.update(value)
                else:
                    values[key] = value
            permit = permit_var.get()
            if (
                not permit
                or permit["used"]
                or permit["fingerprint"]
                != fingerprint(tool.name, canonical(tool, values))
            ):
                raise PermissionError("Tool sem autorização prévia válida.")
            permit["used"] = True
        passo = eventos_do_fluxo.passo_da_tool(tool.name)
        if passo is None:
            return original(*args, **kwargs)
        etapa_id = f"{tool.name}:{uuid4().hex[:8]}"
        eventos_do_fluxo.avisar(eventos_do_fluxo.etapa(etapa_id, "inicio", passo[0]))
        try:
            return original(*args, **kwargs)
        finally:
            eventos_do_fluxo.avisar(eventos_do_fluxo.etapa(etapa_id, "fim", passo[1]))

    guarded._iai_protected = True
    tool.func = guarded
    return tool


def sanitized(value):
    """Só dados JSON explícitos; nunca serializar RequestContext/config/system prompt."""
    from iai.app.core.assistant.guardrail import anonimizar_entrada

    secrets = [GEMINI_API_KEY, GROQ_API_KEY, TYPESAFE_API_KEY, context().token]
    salt = scope_var.get().redaction_salt if scope_var.get() else uuid4().hex

    def clean_text(text):
        for secret in secrets:
            if secret.get_secret_value():
                text = text.replace(secret.get_secret_value(), "[SEGREDO_REMOVIDO]")
        cleaned, pii = anonimizar_entrada(text)
        for token, original in pii.items():
            stable = sha256((salt + original).encode()).hexdigest()[:12]
            cleaned = cleaned.replace(token, token.split("_")[0] + "_" + stable + "]")
        return cleaned

    def walk(item):
        if isinstance(item, dict):
            return {
                clean_text(key): walk(val)
                for key, val in item.items()
                if not re.search(
                    r"token|password|senha|secret|api.?key|authorization",
                    key,
                    re.IGNORECASE,
                )
            }
        if isinstance(item, list):
            return [walk(val) for val in item]
        return clean_text(item) if isinstance(item, str) else item

    return walk(json.loads(json.dumps(value, ensure_ascii=False, default=str)))


def event(record, name):
    data = {
        "event": name,
        "at": datetime.now(UTC).isoformat(),
        **{k: record[k] for k in ("id", "agent", "tool", "attempt")},
    }
    detail = {
        key: record[key]
        for key in ("decision", "failed_checks", "missing_field", "error_type")
        if record.get(key)
    }
    # O log do Render precisa mostrar POR QUE o juiz recusou (sem argumentos nem conversa).
    logger.info(
        "%s agent=%s tool=%s attempt=%s %s",
        name,
        record["agent"],
        record["tool"],
        record["attempt"],
        json.dumps(detail, ensure_ascii=False, default=str) if detail else "",
        extra={"jev": data},
    )
    return data


def last_batch(state):
    return [state["jev_calls"][key] for key in state.get("jev_batch", [])]


def clarification(field_name):
    label = FIELD_LABELS.get(field_name)
    return (
        f"Para continuar, preciso que confirme {label}."
        if label
        else "Qual informação do pedido você pode esclarecer para que eu continue sem presumir dados?"
    )


class JevToolGate(AgentMiddleware):
    state_schema = GateState

    def __init__(self, agent, tools):
        self.agent = agent
        self.allowed = {tool.name: tool for tool in tools}

    @hook_config(can_jump_to=["end"])
    def before_model(self, state, runtime):
        batch = last_batch(state)
        # Somente indisponibilidade de uma leitura complementar admite síntese parcial.
        partial = (
            state.get("jev_parallel")
            and any(c["status"] == "EXECUTED" for c in batch)
            and all(not c.get("halt") or c.get("error_type") for c in batch)
        )
        stop = state.get("jev_halt") or (
            None if partial else next((c["halt"] for c in batch if c.get("halt")), None)
        )
        if stop:
            return {
                "messages": [AIMessage(content=stop)],
                "jev_halt": stop,
                "jump_to": "end",
            }
        if any(c["status"] == "RETRY_SPECIALIST" for c in batch):
            return {"jev_attempt": max(c["attempt"] for c in batch) + 1}
        if batch:
            return {"jev_attempt": 1}
        return None

    @hook_config(can_jump_to=["end"])
    def after_model(self, state, runtime):
        message = state["messages"][-1]
        calls = getattr(message, "tool_calls", [])
        if not calls:
            if any(c["status"] == "RETRY_SPECIALIST" for c in last_batch(state)):
                return {
                    "messages": [AIMessage(id=message.id, content=SAFE_STOP)],
                    "jev_halt": SAFE_STOP,
                    "jump_to": "end",
                }
            return None
        identity = context().identity
        # Propostas e leituras que as preparam são etapas sequenciais. Não executar
        # chamadas posteriores de um lote misto com IDs ainda não confirmados.
        sequential = len(calls) > 1 and any(
            c["name"].startswith("propor_") for c in calls
        )
        if sequential:
            calls = calls[:1]
        parallel = len(calls) > 1 and all(c["name"] in PARALLEL_READS for c in calls)
        scope = scope_var.get()
        if scope:
            with scope.lock:
                prior_evidence = list(scope.evidence[-6:])
        else:
            prior_evidence = []
        records = {}
        for call in calls:
            key = str(uuid4())
            record = {
                "id": key,
                "call_id": call["id"],
                "agent": self.agent,
                "tool": call["name"]
                if call["name"] in self.allowed
                else "UNKNOWN_TOOL",
                "arguments": sanitized(call["args"]),
                "fingerprint": fingerprint(call["name"], call["args"]),
                "user_id": identity.user_id,
                "kitchen_id": identity.kitchen_id,
                "turn_id": context().turn_id,
                "attempt": state.get("jev_attempt", 1),
                "status": "PENDING_JEV",
                "created_at": datetime.now(UTC).isoformat(),
            }
            records[key] = ToolCandidate.model_validate(record).model_dump(mode="json")
        update = {
            "jev_calls": records,
            "jev_batch": list(records),
            "jev_parallel": parallel,
            "jev_evidence": prior_evidence,
            "jev_events": [
                event(r, "tool_candidate_created") for r in records.values()
            ],
        }
        if sequential:
            update["messages"] = [message.model_copy(update={"tool_calls": calls})]
        return update

    def wrap_tool_call(self, request, handler):
        scope = scope_var.get() or LiveScope()
        if (
            request.state.get("jev_parallel")
            and request.tool_call["name"] in PARALLEL_READS
        ):
            return self._guard(request, handler, scope)
        with scope.lock:
            return self._guard(request, handler, scope)

    def _guard(self, request, handler, scope):
        call = request.tool_call
        matches = [r for r in last_batch(request.state) if r["call_id"] == call["id"]]
        if len(matches) != 1:
            raise PermissionError("Tool call não capturada ou identificador duplicado.")
        record = dict(matches[0])
        if (
            record["status"] != "PENDING_JEV"
            or record["fingerprint"] != fingerprint(call["name"], call["args"])
            or record["id"] in scope.executed
            or record["agent"] != self.agent
            or record["user_id"] != context().identity.user_id
            or record["kitchen_id"] != context().identity.kitchen_id
        ):
            raise PermissionError("Tool call alterada ou reutilizada.")
        events = [event(record, "validacao_local_iniciada")]
        started = perf_counter()
        decision = judge.Decision(decision="REJECT")
        tool = self.allowed.get(call["name"])
        hard_error = None
        args = None
        halt = None
        try:
            if tool is None or request.tool is not tool:
                hard_error = "TOOL_NOT_ALLOWED"
            elif call["name"].startswith("propor_") and Operation(
                call["name"][7:]
            ) not in ROLE_OPERATIONS.get(context().identity.role, set()):
                hard_error = "ROLE_NOT_ALLOWED"
            raw = json.dumps(call["args"], default=str)
            if raw != remove_secrets(raw) or any(
                secret.get_secret_value() and secret.get_secret_value() in raw
                for secret in (
                    GEMINI_API_KEY,
                    GROQ_API_KEY,
                    TYPESAFE_API_KEY,
                    context().token,
                )
            ):
                hard_error = "SECRET_IN_ARGUMENTS"
            if hard_error:
                decision = judge.Decision(decision="REJECT", failed_checks=[hard_error])
            else:
                try:
                    args = canonical(tool, call["args"])
                    decision = judge.Decision(decision="APPROVE")
                    record["validacao_local"] = True
                except (ValidationError, ValueError, TypeError) as exc:
                    logger.warning(
                        "Argumentos inválidos em %s (%s): %s",
                        call["name"],
                        self.agent,
                        resumo_de_erro(exc),
                    )
                    decision = judge.Decision(
                        decision="RETRY_SPECIALIST"
                        if record["attempt"] < judge.MAX_ATTEMPTS
                        else "REJECT",
                        failed_checks=["SCHEMA_INVALID"],
                    )
        except Exception as exc:  # noqa: BLE001 - fronteira de autorização sempre fail-closed
            # Não copiar str(exc): corpo, SDK ou resposta podem conter credenciais.
            decision = judge.Decision(
                decision="REJECT", failed_checks=["JEV_UNAVAILABLE"]
            )
            record["error_type"] = type(exc).__name__
            halt = UNAVAILABLE
        record.update(decision.model_dump())
        logger.info(
            "Gate %s tool=%s decisao=%s reprovados=%s tentativa=%s erro=%s",
            self.agent,
            record["tool"],
            decision.decision,
            decision.failed_checks,
            record["attempt"],
            record.get("error_type"),
        )
        record["duration_ms"] = round((perf_counter() - started) * 1000, 2)
        record["status"] = decision.decision
        record["decided_at"] = datetime.now(UTC).isoformat()
        if decision.decision == "APPROVE":
            events.append(event(record, "validacao_local_aprovada"))
            # Uma autorização vale só para o nome+argumentos validados desta tentativa.
            permit = permit_var.set(
                {"fingerprint": fingerprint(tool.name, args), "used": False}
            )
            try:
                with scope.lock:
                    if record["id"] in scope.executed:
                        raise PermissionError("Candidato já executado.")
                    scope.executed.add(record["id"])
                events.append(event(record, "tool_execution_started"))
                result = handler(request)
                if not isinstance(result, ToolMessage):
                    raise TypeError("Resultado de tool não suportado pelo gate.")
                record["status"] = "FAILED" if result.status == "error" else "EXECUTED"
                try:
                    payload = json.loads(result.content)
                    if isinstance(payload, dict) and payload.get("ok") is False:
                        record["status"] = "FAILED"
                except (ValueError, TypeError):
                    pass  # FAQ/histórico também devolvem texto, sem contrato JSON.
                evidence_result = sanitized(
                    {"tool": tool.name, "result": result.content}
                )
                with scope.lock:
                    scope.evidence.append(evidence_result)
            except Exception as exc:  # noqa: BLE001 - não repetir resultado potencialmente incerto
                record["status"] = "FAILED"
                record["error_type"] = type(exc).__name__
                halt = "Não consegui confirmar o resultado dessa ferramenta. Consulte suas solicitações antes de repetir."
                result = ToolMessage(
                    content='{"ok":false,"code":"TOOL_FAILED"}',
                    tool_call_id=call["id"],
                    status="error",
                )
            finally:
                events.append(event(record, "tool_execution_finished"))
                permit_var.reset(permit)
        else:
            events.append(event(record, "validacao_local_reprovada"))
            result = ToolMessage(
                content=json.dumps(
                    {
                        "ok": False,
                        "code": "JEV_REJECTED",
                        "decision": decision.decision,
                        "failed_checks": decision.failed_checks,
                        "attempt": record["attempt"],
                        "instruction": "Corrija a chamada com evidência autorizada; não invente dados nem alegue execução.",
                    }
                ),
                tool_call_id=call["id"],
                status="error",
            )
            if decision.decision == "ASK_USER":
                halt = clarification(decision.missing_field)
                events.append(event(record, "tool_user_input_required"))
            elif decision.decision == "REJECT":
                halt = halt or mensagem_de_recusa(decision.failed_checks)
            else:
                events.append(event(record, "tool_retry"))
        record["finished_at"] = datetime.now(UTC).isoformat()
        if halt:
            record["halt"] = halt
        # Mantém a origem também em erros interceptados antes do executor.
        result.name = tool.name if tool else record["tool"]
        update = {
            "messages": [result],
            "jev_calls": {record["id"]: record},
            "jev_events": events,
        }
        # Uma única tool termina o lote; múltiplos halts são reduzidos por lista.
        if halt:
            update["jev_stops"] = [halt]
        return Command(update=update)
