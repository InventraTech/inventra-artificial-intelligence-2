import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, Literal
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from pymongo.errors import PyMongoError

from iai.app.core.config import (
    CENARIOS_USUARIOS_SEMANAIS,
    LLM_MODE,
    PERIODOS,
    PRECOS_MODELOS_USD_POR_MILHAO,
    REQUISICOES_POR_USUARIO_SEMANA,
    VALOR_POR_RESOLUCAO_USD,
)
from iai.app.core.infra.storage import CollectionProxy
from iai.app.core.schemas import (
    Cenario,
    Custo,
    CustoModelo,
    Erros,
    Latencia,
    LatenciaNo,
    MetricasResponse,
    Premissas,
    Resolucao,
    Roi,
)

# Sem I/O no import; o índice é criado em storage.initialize_storage.
col_metricas = CollectionProxy("metrics")

PREFIXOS_GUARDRAIL_ENTRADA = ("guardrail_insulto:", "guardrail_escopo:")


def custo_usd(modelo: str, entrada: int, saida: int) -> float:
    preco_entrada, preco_saida = PRECOS_MODELOS_USD_POR_MILHAO.get(modelo, (0.0, 0.0))
    return (entrada * preco_entrada + saida * preco_saida) / 1_000_000


class ColetorMetricas(BaseCallbackHandler):
    """
    Callback do LangChain/LangGraph que acumula, para UMA requisição: a latência de cada nó
    do grafo, cada chamada de LLM (modelo, tokens, custo, latência) e os erros de LLM/ferramenta. Vai em config["callbacks"] de cada invoke, então duas
    requisições simultâneas nunca misturam a conta. O texto do prompt não é guardado.
    """

    def __init__(self) -> None:
        self.nos: list[dict[str, Any]] = []
        self.chamadas: list[dict[str, Any]] = []
        self.erros: list[str] = []
        self.agentes_chamados: list[str] = []
        self._nos_em_andamento: dict[UUID, tuple[str, float]] = {}
        self._chamadas_em_andamento: dict[UUID, dict[str, Any]] = {}

    def on_chain_start(self, serialized, inputs, *, run_id, metadata=None, name=None, **kwargs):
        metadata = metadata or {}
        no = metadata.get("langgraph_node")
        if no and name == no and "|" not in metadata.get("langgraph_checkpoint_ns", ""):
            self._nos_em_andamento[run_id] = (no, perf_counter())

    def _fechar_no(self, run_id: UUID, erro: bool, saida: Any = None) -> None:
        item = self._nos_em_andamento.pop(run_id, None)
        if item:
            no, inicio = item
            if isinstance(saida, dict):
                self.agentes_chamados += saida.get("agentes_chamados", [])
            self.nos.append({"no": no, "ms": round((perf_counter() - inicio) * 1000, 1), "erro": erro})

    def on_chain_end(self, outputs, *, run_id, **kwargs):
        self._fechar_no(run_id, erro=False, saida=outputs)

    def on_chain_error(self, error, *, run_id, **kwargs):
        self._fechar_no(run_id, erro=True)
        self.erros.append(f"chain:{type(error).__name__}")

    def on_chat_model_start(self, serialized, messages, *, run_id, metadata=None, **kwargs):
        self._chamadas_em_andamento[run_id] = {
            "t0":     perf_counter(),
            "modelo": (metadata or {}).get("ls_model_name", "desconhecido"),
        }

    def on_llm_end(self, response: LLMResult, *, run_id, **kwargs):
        abertura = self._chamadas_em_andamento.pop(run_id, None)
        if abertura is None:
            return

        entrada = saida = 0
        modelo = abertura["modelo"]
        for geracoes in response.generations:
            for geracao in geracoes:
                mensagem = getattr(geracao, "message", None)
                uso = getattr(mensagem, "usage_metadata", None) or {}
                entrada += uso.get("input_tokens", 0)
                saida += uso.get("output_tokens", 0)
                modelo_resposta = (getattr(mensagem, "response_metadata", None) or {}).get("model_name")
                if modelo_resposta:
                    modelo = str(modelo_resposta).removeprefix("models/")

        self.chamadas.append({
            "modelo":         modelo,
            "tokens_entrada": entrada,
            "tokens_saida":   saida,
            "custo_usd":      custo_usd(modelo, entrada, saida),
            "latencia_ms":    round((perf_counter() - abertura["t0"]) * 1000, 1),
        })

    def on_llm_error(self, error, *, run_id, **kwargs):
        self._chamadas_em_andamento.pop(run_id, None)
        self.erros.append(f"llm:{type(error).__name__}")

    def on_tool_error(self, error, *, run_id, **kwargs):
        self.erros.append(f"tool:{type(error).__name__}")


def status_da_requisicao(agentes_chamados: list[str], falhou: bool) -> str:
    """erro = exceção; bloqueado = guardrail de entrada recusou (sistema funcionando, não é erro)."""
    if falhou or "jev:indisponivel" in agentes_chamados:
        return "erro"
    for nome in agentes_chamados:
        if nome.startswith(PREFIXOS_GUARDRAIL_ENTRADA) and not nome.endswith(":aprovado"):
            return "bloqueado"
    return "ok"


def foi_resolvida(agentes_chamados: list[str]) -> bool:
    """Execução autorizada concluída; reconhece marcador legado de métricas antigas."""
    return "jev:concluido" in agentes_chamados or "juiz:aprovado" in agentes_chamados


def registrar_requisicao(
    coletor: ColetorMetricas,
    inicio: float,
    session_id: str,
    identity: Any,
    estado_final: dict | None,
    falhou: bool,
    error_type: str | None = None,
) -> None:
    """Persiste o turno. Nunca propaga erro: métrica não pode derrubar o chat.

    Guarda só dados técnicos (latência, tokens, custo, nomes de agentes); nunca o
    conteúdo da conversa nem o motivo do juiz."""
    agentes = (estado_final or {}).get("agentes_chamados") or coletor.agentes_chamados
    status = status_da_requisicao(agentes, falhou)
    try:
        col_metricas.insert_one({
            "timestamp":         datetime.now(UTC),
            "session_id":        session_id,
            "user_id":           identity.user_id,
            "kitchen_id":        identity.kitchen_id,
            "latencia_total_ms": round((perf_counter() - inicio) * 1000, 1),
            "status":            status,
            "nos":               coletor.nos,
            "chamadas":          coletor.chamadas,
            "tokens_entrada":    sum(c["tokens_entrada"] for c in coletor.chamadas),
            "tokens_saida":      sum(c["tokens_saida"] for c in coletor.chamadas),
            "custo_usd":         sum(c["custo_usd"] for c in coletor.chamadas),
            "erros_componentes": coletor.erros + [
                "jev:" + call["error_type"] for call in (estado_final or {}).get("jev_calls", {}).values()
                if call.get("error_type")
            ],
            "resolvida":         status == "ok" and foi_resolvida(agentes),
            "agentes_chamados":  [str(a).split(":")[0] for a in agentes],
            "llm_mode":          LLM_MODE,
            "jev": [{key: call[key] for key in (
                "id", "agent", "tool", "attempt", "status", "decision", "checks",
                "failed_checks", "duration_ms", "model", "usage", "error_type",
                "created_at", "decided_at", "finished_at",
            ) if key in call} for call in (estado_final or {}).get("jev_calls", {}).values()],
            "jev_events": (estado_final or {}).get("jev_events", []),
            "error_type":        error_type,
        })
    except PyMongoError:
        pass


def percentil_95(valores: list[float]) -> float:
    """Nearest-rank: o menor valor que cobre 95% das amostras."""
    if not valores:
        return 0.0
    ordenados = sorted(valores)
    return ordenados[max(0, math.ceil(0.95 * len(ordenados)) - 1)]


def _media(valores: list[float]) -> float:
    return sum(valores) / len(valores) if valores else 0.0


def _razao(numerador: float, denominador: float) -> float | None:
    return numerador / denominador if denominador else None


def gerar_metricas(
    periodo: Literal["semana", "mes"] = "semana",
    usuarios: Sequence[int] | None = None,
    identity: Any = None,
) -> MetricasResponse:
    """
    Métricas de observabilidade/SRE. `periodo` define a janela de histórico observada
    (semana = 7 dias, mes = 30) e o horizonte da projeção de custo. `usuarios` define os
    cenários de usuários semanais (padrão: 100 e 1000). Com `identity`, só conta a cozinha
    do usuário; quem não é SUPERVISOR vê apenas as próprias requisições.
    """
    dias, semanas = PERIODOS[periodo]
    desde = datetime.now(UTC) - timedelta(days=dias)
    filtro: dict[str, Any] = {"timestamp": {"$gte": desde}}
    if identity is not None:
        filtro["kitchen_id"] = identity.kitchen_id
        if identity.role != "SUPERVISOR":
            filtro["user_id"] = identity.user_id
    turnos = list(col_metricas.find(filtro))
    total = len(turnos)

    latencias = [t["latencia_total_ms"] for t in turnos]
    por_no: dict[str, list[float]] = defaultdict(list)
    por_modelo: dict[str, list[dict]] = defaultdict(list)
    for t in turnos:
        for n in t["nos"]:
            por_no[n["no"]].append(n["ms"])
        for c in t["chamadas"]:
            por_modelo[c["modelo"]].append(c)

    custo_total = sum(t["custo_usd"] for t in turnos)
    custo_medio = custo_total / total if total else 0.0
    resolucoes = sum(1 for t in turnos if t["resolvida"])
    taxa_resolucao = resolucoes / total if total else 0.0
    retorno = resolucoes * VALOR_POR_RESOLUCAO_USD

    cenarios = []
    for qtd in usuarios or CENARIOS_USUARIOS_SEMANAIS:
        requisicoes = qtd * REQUISICOES_POR_USUARIO_SEMANA * semanas
        custo = requisicoes * custo_medio
        valor = requisicoes * taxa_resolucao * VALOR_POR_RESOLUCAO_USD
        cenarios.append(Cenario(
            usuarios=qtd,
            requisicoes=round(requisicoes),
            custo_usd=round(custo, 4),
            retorno_usd=round(valor, 4),
            roi=_razao(valor - custo, custo),
        ))

    return MetricasResponse(
        periodo=periodo,
        dias_observados=dias,
        requisicoes=total,
        latencia=Latencia(
            total_media_ms=round(_media(latencias)),
            total_p95_ms=round(percentil_95(latencias)),
            por_no=[
                LatenciaNo(no=no, chamadas=len(v), media_ms=round(_media(v)), p95_ms=round(percentil_95(v)))
                for no, v in sorted(por_no.items())
            ],
        ),
        erros=Erros(
            indice_erros=round(sum(1 for t in turnos if t["status"] == "erro") / total, 4) if total else 0.0,
            indice_erros_componentes=round(sum(1 for t in turnos if t["erros_componentes"]) / total, 4) if total else 0.0,
            bloqueios=sum(1 for t in turnos if t["status"] == "bloqueado"),
        ),
        custo=Custo(
            total_usd=round(custo_total, 6),
            medio_por_requisicao_usd=round(custo_medio, 6),
            tokens_entrada=sum(t["tokens_entrada"] for t in turnos),
            tokens_saida=sum(t["tokens_saida"] for t in turnos),
            por_modelo=[
                CustoModelo(
                    modelo=modelo,
                    chamadas=len(itens),
                    tokens_entrada=sum(i["tokens_entrada"] for i in itens),
                    tokens_saida=sum(i["tokens_saida"] for i in itens),
                    custo_usd=round(sum(i["custo_usd"] for i in itens), 6),
                )
                for modelo, itens in sorted(por_modelo.items())
            ],
        ),
        resolucao=Resolucao(
            resolucoes=resolucoes,
            taxa_resolucao=round(taxa_resolucao, 4),
            custo_por_resolucao_usd=_razao(custo_total, resolucoes),
        ),
        roi=Roi(
            valor_por_resolucao_usd=VALOR_POR_RESOLUCAO_USD,
            retorno_usd=retorno,
            roi=_razao(retorno - custo_total, custo_total),
        ),
        cenarios=cenarios,
        premissas=Premissas(
            requisicoes_por_usuario_semana=REQUISICOES_POR_USUARIO_SEMANA,
            valor_por_resolucao_usd=VALOR_POR_RESOLUCAO_USD,
        ),
    )
