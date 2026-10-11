import operator
from datetime import datetime
from typing import Annotated, Literal

from langgraph.graph import MessagesState
from pydantic import BaseModel


class ChatRequest(BaseModel):
    session_id: str
    pergunta: str
    user_id: str

class ChatResponse(BaseModel):
    resposta: str

class EncerrarSessaoRequest(BaseModel):
    session_id: str

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
    resolucao: Resolucao
    roi: Roi
    cenarios: list[Cenario]
    premissas: Premissas

class Estado(MessagesState):
    agentes_chamados:     Annotated[list[str], operator.add]
    rota:                 str
    mapa_pii:             dict
    tentativas_juiz:      int
    repetir_especialista: bool

class ResultadoGuardrail(BaseModel):
    bloqueado: bool 
    motivo: str 
    mensagem: str 

class ResultadoJuiz(BaseModel):
    confianca: float
    motivo: str