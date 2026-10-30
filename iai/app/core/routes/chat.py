import asyncio
import json
import logging
import threading
import uuid
from contextvars import copy_context
from dataclasses import replace
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import StreamingResponse

from iai.app.core.assistant.memory import (
    encerrar_sessao,
    recuperar_historico,
    recuperar_mensagem,
)
from iai.app.core.infra.security import (
    Authenticated,
    current_context,
)
from iai.app.core.schemas import (
    ChatAudioResponse,
    ChatRequest,
    ChatResponse,
    EncerrarSessaoRequest,
    EncerrarSessaoResponse,
    HistoricoResponse,
    MensagensResponse,
)
from iai.app.extras.audio import TAMANHO_MAXIMO_BYTES, transcrever_audio

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def executar_no_contexto(
    ctx, pergunta: str, session_id: str, request_id: str | None = None
) -> ChatResponse:
    from iai.app.core.assistant.graph import executar_fluxo_chat

    marker = current_context.set(
        replace(ctx, session_id=session_id, turn_id=request_id or str(uuid.uuid4()))
    )
    try:
        return executar_fluxo_chat(pergunta, session_id, ctx.identity.user_id)
    finally:
        current_context.reset(marker)


@router.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest, ctx: Authenticated) -> ChatResponse:
    if payload.user_id != ctx.identity.user_id:
        raise HTTPException(403, "user_id diverge da identidade autenticada.")
    return executar_no_contexto(
        ctx, payload.pergunta, payload.session_id, payload.request_id
    )


PING_SEGUNDOS = 8
FIM_DO_STREAM = object()


def eventos_ndjson(ctx, payload: ChatRequest):
    """Roda o grafo numa thread própria e entrega cada evento como uma linha NDJSON.

    O contexto da requisição (contextvars) só vale dentro da thread que o criou; por isso o
    gerador inteiro roda lá e a parte assíncrona só lê a fila. Linha em branco = sinal de vida.
    """
    from iai.app.core.assistant.graph import executar_fluxo_chat_stream

    fila: asyncio.Queue = asyncio.Queue()
    cancelado = threading.Event()
    loop = asyncio.get_running_loop()

    def produzir():
        marker = current_context.set(
            replace(
                ctx,
                session_id=payload.session_id,
                turn_id=payload.request_id or str(uuid.uuid4()),
            )
        )
        try:
            for evento in executar_fluxo_chat_stream(
                payload.pergunta, payload.session_id, ctx.identity.user_id, cancelado
            ):
                loop.call_soon_threadsafe(fila.put_nowait, evento)
        except Exception as exc:  # noqa: BLE001 - depois do 200 o erro só pode ser um evento
            logger.error("Stream do chat falhou: %s", type(exc).__name__)
            loop.call_soon_threadsafe(
                fila.put_nowait,
                {"type": "erro", "mensagem": "Tive um erro interno. Tente de novo."},
            )
        finally:
            current_context.reset(marker)
            loop.call_soon_threadsafe(fila.put_nowait, FIM_DO_STREAM)

    async def gerar():
        thread = threading.Thread(
            target=copy_context().run, args=(produzir,), daemon=True
        )
        thread.start()
        try:
            while True:
                try:
                    evento = await asyncio.wait_for(fila.get(), PING_SEGUNDOS)
                except TimeoutError:
                    yield "\n"
                    continue
                if evento is FIM_DO_STREAM:
                    return
                yield json.dumps(evento, ensure_ascii=False) + "\n"
        finally:
            cancelado.set()  # app fechou a conexão: o grafo para de gastar LLM

    return gerar()


@router.post("/chat/stream")
async def chat_stream(payload: ChatRequest, ctx: Authenticated):
    if payload.user_id != ctx.identity.user_id:
        raise HTTPException(403, "user_id diverge da identidade autenticada.")
    return StreamingResponse(
        eventos_ndjson(ctx, payload),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
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
    resposta = executar_no_contexto(ctx, texto, session_id, request_id)
    return ChatAudioResponse(transcricao=texto, **resposta.model_dump())


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
