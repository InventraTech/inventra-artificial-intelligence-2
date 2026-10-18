import uuid
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile

from iai.app.audio import TAMANHO_MAXIMO_BYTES, transcrever_audio
from iai.app.memory import encerrar_sessao, recuperar_historico, recuperar_mensagem
from iai.app.schemas import (
    ChatAudioResponse,
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


def _executar_no_contexto(
    ctx, pergunta: str, session_id: str, request_id: str | None = None
) -> str:
    from iai.app.graph import executar_fluxo_assessor

    marker = current_context.set(
        replace(ctx, session_id=session_id, turn_id=request_id or str(uuid.uuid4()))
    )
    try:
        return executar_fluxo_assessor(pergunta, session_id, ctx.identity.user_id)
    finally:
        current_context.reset(marker)


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, ctx: Authenticated) -> ChatResponse:
    if payload.user_id != ctx.identity.user_id:
        raise HTTPException(403, "user_id diverge da identidade autenticada.")
    return ChatResponse(
        resposta=_executar_no_contexto(
            ctx, payload.pergunta, payload.session_id, payload.request_id
        )
    )


@router.post("/chat/audio", response_model=ChatAudioResponse)
def chat_audio(
    ctx: Authenticated,
    session_id: Annotated[str, Form(min_length=1, max_length=150)],
    user_id: Annotated[str, Form(min_length=1, max_length=150)],
    audio: Annotated[UploadFile, File()],
    request_id: Annotated[str | None, Form(min_length=1, max_length=100)] = None,
) -> ChatAudioResponse:
    if user_id != ctx.identity.user_id:
        raise HTTPException(403, "user_id diverge da identidade autenticada.")
    conteudo = audio.file.read(TAMANHO_MAXIMO_BYTES + 1)
    texto = transcrever_audio(conteudo, audio.filename or "audio.webm")
    resposta = _executar_no_contexto(ctx, texto, session_id, request_id)
    return ChatAudioResponse(transcricao=texto, resposta=resposta)


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
