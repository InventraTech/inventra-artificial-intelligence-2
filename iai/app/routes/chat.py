import uuid
from dataclasses import replace

from fastapi import APIRouter, HTTPException, Query

from iai.app.memory import encerrar_sessao, recuperar_historico, recuperar_mensagem
from iai.app.schemas import (
    ChatRequest,
    ChatResponse,
    EncerrarSessaoRequest,
    EncerrarSessaoResponse,
    HistoricoResponse,
    MensagensResponse,
)
from iai.app.security import (
    Authenticated,
    current_context,
)

router = APIRouter(tags=["chat"])


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, ctx: Authenticated) -> ChatResponse:
    if payload.user_id != ctx.identity.user_id:
        raise HTTPException(403, "user_id diverge da identidade autenticada.")
    marker = current_context.set(
        replace(
            ctx,
            session_id=payload.session_id,
            turn_id=payload.request_id or str(uuid.uuid4()),
        )
    )
    try:
        from iai.app.graph import executar_fluxo_assessor

        return ChatResponse(
            resposta=executar_fluxo_assessor(
                payload.pergunta, payload.session_id, ctx.identity.user_id
            )
        )
    finally:
        current_context.reset(marker)


@router.post("/chat/encerrar", response_model=EncerrarSessaoResponse)
def encerrar(payload: EncerrarSessaoRequest, ctx: Authenticated):
    resumo = encerrar_sessao(
        payload.session_id, ctx.identity.user_id, ctx.identity.kitchen_id
    )
    if not resumo:
        raise HTTPException(404, "Sessão não encontrada ou sem mensagens.")
    return EncerrarSessaoResponse(resumo=resumo)


@router.get("/chat/historico/{user_id}", response_model=HistoricoResponse)
def historico(
    user_id: str,
    ctx: Authenticated,
    limite: int = Query(10, ge=1, le=100),
    busca: str = Query("", max_length=200),
):
    if user_id != ctx.identity.user_id:
        raise HTTPException(403, "O histórico pertence a outro usuário.")
    return HistoricoResponse(
        sessoes=recuperar_historico(
            user_id, busca=busca, limite=limite, kitchen_id=ctx.identity.kitchen_id
        )
    )


@router.get("/chat/mensagens/{doc_id}", response_model=MensagensResponse)
def mensagens(doc_id: str, ctx: Authenticated):
    return MensagensResponse(
        messages=recuperar_mensagem(
            doc_id, ctx.identity.user_id, ctx.identity.kitchen_id
        )
    )
