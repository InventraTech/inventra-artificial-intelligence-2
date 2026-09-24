import re
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query

from iai.app.confirmation import ConfirmationStore
from iai.app.database import transaction
from iai.app.graph import executar_fluxo_assessor
from iai.app.memory import (
    col_sessoes,
    encerrar_sessao,
    recuperar_historico,
    recuperar_mensagem,
    salvar_mensagem,
)
from iai.app.presentation import render_results
from iai.app.repository import Repository
from iai.app.schemas import (
    ChatRequest,
    ChatResponse,
    ConfirmationRequest,
    EncerrarSessaoRequest,
    EncerrarSessaoResponse,
    HistoricoResponse,
    MensagensResponse,
)
from iai.app.security import ExecutionContext, authorize, identity_from_backend

router = APIRouter(tags=["chat"])

Identity = Annotated[str, Depends(identity_from_backend)]


def check_user(user_id: str):
    with transaction() as conn:
        authorize(Repository(conn), user_id)


def confirm_operation(payload: ConfirmationRequest, user_id: str) -> dict:
    check_user(user_id)
    context = ExecutionContext(user_id, payload.session_id)
    response = ConfirmationStore(col_sessoes).confirm(context, payload.confirmation_id, payload.approved)
    salvar_mensagem(payload.session_id, "iai", render_results([response]), user_id=user_id)
    return response

@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, user_id: Identity) -> ChatResponse:
    if payload.user_id is not None and payload.user_id != user_id:
        raise HTTPException(403, "O usuário informado não corresponde à identidade do servidor.")
    check_user(user_id)
    confirmation = re.fullmatch(r"(confirmar|cancelar)\s+([A-Za-z0-9_-]{32})", payload.pergunta.strip(), re.IGNORECASE)
    if confirmation:
        response = confirm_operation(ConfirmationRequest(
            session_id=payload.session_id, confirmation_id=confirmation[2],
            approved=confirmation[1].lower() == "confirmar"), user_id)
        return ChatResponse(resposta=render_results([response]))
    resposta = executar_fluxo_assessor(
        pergunta_usuario=payload.pergunta,
        session_id=payload.session_id,
        user_id=user_id
    )
    return ChatResponse(resposta=resposta)

@router.post("/chat/encerrar", response_model=EncerrarSessaoResponse)
def encerrar(payload: EncerrarSessaoRequest, user_id: Identity) -> EncerrarSessaoResponse:
    check_user(user_id)
    resumo = encerrar_sessao(payload.session_id, user_id)
    if not resumo:
        raise HTTPException(status_code=404, detail="Sessão não encontrada ou sem mensagens.")
    return EncerrarSessaoResponse(resumo=resumo)

@router.get("/chat/historico/{user_id}", response_model=HistoricoResponse)
def historico(user_id: str, identity: Identity, limite: int = Query(10, ge=1, le=100)) -> HistoricoResponse:
    if user_id != identity:
        raise HTTPException(403, "Histórico de outro usuário não é permitido.")
    check_user(identity)
    return HistoricoResponse(sessoes=recuperar_historico(user_id, limite=limite))

@router.get("/chat/mensagens/{doc_id}", response_model=MensagensResponse)
def mensagens(doc_id: str, user_id: Identity) -> MensagensResponse:
    check_user(user_id)
    return MensagensResponse(messages=recuperar_mensagem(doc_id, user_id))


@router.post("/chat/confirmar")
def confirmar(payload: ConfirmationRequest, user_id: Identity) -> dict:
    return confirm_operation(payload, user_id)
