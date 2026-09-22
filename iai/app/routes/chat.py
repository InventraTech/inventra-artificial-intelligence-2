from fastapi import APIRouter, HTTPException

from iai.app.graph import executar_fluxo_assessor
from iai.app.memory import encerrar_sessao, recuperar_historico, recuperar_mensagem
from iai.app.schemas import (
    ChatRequest,
    ChatResponse,
    EncerrarSessaoRequest,
    EncerrarSessaoResponse,
    HistoricoResponse,
    MensagensResponse,
)

router = APIRouter(tags=["chat"])

@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    resposta = executar_fluxo_assessor(
        pergunta_usuario=payload.pergunta,
        session_id=payload.session_id,
        user_id=payload.user_id
    )
    return ChatResponse(resposta=resposta)

@router.post("/chat/encerrar", response_model=EncerrarSessaoResponse)
def encerrar(payload: EncerrarSessaoRequest) -> EncerrarSessaoResponse:
    resumo = encerrar_sessao(payload.session_id)
    if not resumo:
        raise HTTPException(status_code=404, detail="Sessão não encontrada ou sem mensagens.")
    return EncerrarSessaoResponse(resumo=resumo)

@router.get("/chat/historico/{user_id}", response_model=HistoricoResponse)
def historico(user_id: str, limite: int = 10) -> HistoricoResponse:
    return HistoricoResponse(sessoes=recuperar_historico(user_id, limite=limite))

@router.get("/chat/mensagens/{doc_id}", response_model=MensagensResponse)
def mensagens(doc_id: str) -> MensagensResponse:
    return MensagensResponse(messages=recuperar_mensagem(doc_id))