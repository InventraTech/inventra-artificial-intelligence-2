import operator
from typing import Annotated

from langgraph.graph import MessagesState
from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    session_id: str
    pergunta: str

class ChatResponse(BaseModel):
    resposta: str

class Estado(MessagesState):
    agentes_chamados:   Annotated[list[str], operator.add]
    rota:               str
    mapa_pii:           dict

class ResultadoGuardrail(BaseModel):
    bloqueado: bool = Field(description="True se a mensagem for proibida. False se permitida.")
    motivo: str = Field(description="Motivo curto do bloqueio ou permissão.")
    mensagem: str = Field(description="Mensagem educada se bloqueado. Vazio se permitido.")