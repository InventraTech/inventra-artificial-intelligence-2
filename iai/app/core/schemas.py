import operator
from datetime import datetime
from typing import Annotated, Literal

from langgraph.graph import MessagesState
from pydantic import BaseModel, ConfigDict, Field, model_validator

Number = (
    Annotated[int, Field(strict=True)]
    | Annotated[float, Field(strict=True, allow_inf_nan=False)]
)
Cell = str | Number | None


class ArtifactBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    mocked: bool
    note: str | None = Field(default=None, max_length=300)


class TableColumn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str


class TableArtifact(ArtifactBase):
    type: Literal["table"] = "table"
    columns: list[TableColumn] = Field(min_length=1, max_length=8)
    rows: list[dict[str, Cell]] = Field(min_length=2, max_length=100)

    @model_validator(mode="after")
    def valid_columns(self):
        keys = [c.key for c in self.columns]
        if len(set(keys)) != len(keys) or any(set(r) != set(keys) for r in self.rows):
            raise ValueError("Colunas e registros da tabela incompatíveis.")
        return self


class ChartDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    data: list[Number] = Field(min_length=2, max_length=100)


class ChartArtifact(ArtifactBase):
    type: Literal["chart"] = "chart"
    chart_type: Literal["bar", "pie"]
    labels: list[str] = Field(min_length=2, max_length=100)
    datasets: list[ChartDataset] = Field(min_length=1, max_length=2)
    unit: str | None = None

    @model_validator(mode="after")
    def valid_data(self):
        if any(len(d.data) != len(self.labels) for d in self.datasets):
            raise ValueError("Labels e valores incompatíveis.")
        if len(set(self.labels)) != len(self.labels):
            raise ValueError("Categorias ambíguas.")
        if self.chart_type == "pie" and (
            len(self.datasets) != 1
            or any(v < 0 for v in self.datasets[0].data)
            or sum(self.datasets[0].data) <= 0
        ):
            raise ValueError("Distribuição inválida.")
        return self


Artifact = Annotated[TableArtifact | ChartArtifact, Field(discriminator="type")]


class PendingAction(BaseModel):
    id: str
    proposal_hash: str
    operation: str
    parameters: dict
    justification: str
    status: str
    expires_at: datetime
    before: list[dict]


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=150)
    pergunta: str = Field(min_length=1, max_length=10000)
    user_id: str = Field(min_length=1, max_length=150)
    request_id: str | None = Field(default=None, min_length=1, max_length=100)


class ChatResponse(BaseModel):
    resposta: str
    artifacts: list[Artifact] = Field(default_factory=list, max_length=1)
    pending_actions: list[PendingAction] = Field(default_factory=list)


class ChatAudioResponse(ChatResponse):
    transcricao: str


class EncerrarSessaoRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=150)


class EncerrarSessaoResponse(BaseModel):
    resumo: str


class SessaoResumo(BaseModel):
    doc_id: str
    started_at: datetime
    summary: str


class HistoricoResponse(BaseModel):
    sessoes: list[SessaoResumo]


class MensagemHistorico(BaseModel):
    role: str
    content: str


class MensagensResponse(BaseModel):
    messages: list[MensagemHistorico]


class LatenciaNo(BaseModel):
    no: str
    chamadas: int
    media_ms: int
    p95_ms: int


class Latencia(BaseModel):
    total_media_ms: int
    total_p95_ms: int
    por_no: list[LatenciaNo]


class Erros(BaseModel):
    indice_erros: float
    indice_erros_componentes: float
    bloqueios: int


class CustoModelo(BaseModel):
    modelo: str
    chamadas: int
    tokens_entrada: int
    tokens_saida: int
    custo_usd: float


class Custo(BaseModel):
    total_usd: float
    medio_por_requisicao_usd: float
    tokens_entrada: int
    tokens_saida: int
    por_modelo: list[CustoModelo]


class Jev(BaseModel):
    chamadas: int
    reprovacoes: int
    indisponiveis: int
    latencia_media_ms: int
    latencia_p95_ms: int
    tokens_entrada: int
    custo_usd: float


class Resolucao(BaseModel):
    resolucoes: int
    taxa_resolucao: float
    custo_por_resolucao_usd: float | None


class Roi(BaseModel):
    valor_por_resolucao_usd: float
    retorno_usd: float
    roi: float | None


class Cenario(BaseModel):
    usuarios: int
    requisicoes: float
    custo_usd: float
    retorno_usd: float
    roi: float | None


class Premissas(BaseModel):
    requisicoes_por_usuario_semana: float
    valor_por_resolucao_usd: float


class MetricasResponse(BaseModel):
    periodo: Literal["semana", "mes"]
    dias_observados: int
    requisicoes: int
    latencia: Latencia
    erros: Erros
    custo: Custo
    jev: Jev
    resolucao: Resolucao
    roi: Roi
    cenarios: list[Cenario]
    premissas: Premissas


def unir_dicts(atual: dict | None, novo: dict | None) -> dict:
    return {**(atual or {}), **(novo or {})}


def primeiro_valor(atual: str | None, novo: str | None) -> str | None:
    return atual or novo


class Estado(MessagesState):
    agentes_chamados: Annotated[list[str], operator.add]
    rota: str
    mapa_pii: dict
    # Especialistas em paralelo escrevem no mesmo passo: cada campo precisa de um reducer.
    jev_calls: Annotated[dict, unir_dicts]
    jev_events: Annotated[list[dict], operator.add]
    jev_halt: Annotated[str | None, primeiro_valor]
    # Resposta final de cada especialista da rodada (a síntese junta quando são vários).
    respostas_parciais: Annotated[list[dict], operator.add]


class ResultadoGuardrail(BaseModel):
    bloqueado: bool
    motivo: str
    mensagem: str
