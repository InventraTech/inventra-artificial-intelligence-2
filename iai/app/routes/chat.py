from fastapi import APIRouter
from iai.app.schemas import ChatResponse, ChatRequest
from app.graph import executar_fluxo_assessor

router = APIRouter(tags=["chat"])

@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    resposta = executar_fluxo_assessor(
        pergunta_usuario=payload.pergunta,
        session_id=payload.session_id,
    )
    return ChatResponse(resposta=resposta)