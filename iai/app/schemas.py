import operator
from datetime import datetime
from typing import Annotated

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field


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

class Estado(MessagesState):
    agentes_chamados:   Annotated[list[str], operator.add]
    rota:               str
    mapa_pii:           dict

class ResultadoGuardrail(BaseModel):
    bloqueado: bool = Field(description="True se a mensagem for proibida. False se permitida.")
    motivo: str = Field(description="Motivo curto do bloqueio ou permissão.")
    mensagem: str = Field(description="Mensagem educada se bloqueado. Vazio se permitido.")