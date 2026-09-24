import operator
from datetime import datetime
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    pergunta: str = Field(min_length=1, max_length=8000)
    user_id: str | None = None  # legacy consistency check, never an identity source

class ChatResponse(BaseModel):
    resposta: str

class ConfirmationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=128)
    confirmation_id: str = Field(pattern=r"^[A-Za-z0-9_-]{32}$")
    approved: bool = Field(strict=True)

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

class Estado(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    agentes_chamados:   Annotated[list[str], operator.add]
    rota:               str
    mapa_pii:           dict
    tool_results:      list[dict]

class FAQSelection(BaseModel):
    faq_index: int | None = Field(description="Índice 1-based da resposta do FAQ, ou null se não há resposta.")

class ActionSelection(BaseModel):
    missing_fields: list[Literal["produto", "quantidade", "unidade", "lote", "intencao"]] = Field(
        default_factory=list, description="Dados que precisam ser esclarecidos pelo usuário; não invente valores.")

class ResultadoGuardrail(BaseModel):
    bloqueado: bool = Field(description="True se a mensagem for proibida. False se permitida.")
    motivo: str = Field(description="Motivo curto do bloqueio ou permissão.")
    mensagem: str = Field(description="Mensagem educada se bloqueado. Vazio se permitido.")
