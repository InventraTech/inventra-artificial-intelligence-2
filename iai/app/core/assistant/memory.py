"""Histórico Mongo com propriedade por usuário/cozinha; nenhuma conexão no import."""

import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from pymongo.errors import DuplicateKeyError

from iai.app.core.config import LLM_MODE
from iai.app.core.infra.retrieval import rank
from iai.app.core.schemas import MensagemHistorico, SessaoResumo
from iai.app.core.infra.storage import CollectionProxy

col_sessoes = CollectionProxy("sessions")
sessoes_ativas: dict = {}


def agora():
    return datetime.now(UTC)


def formatar_conversa(mensagens):
    return "\n".join(
        f"{m['role']}: {m['content']}" for m in mensagens if m["role"] != "judge"
    )


def gerar_resumo(mensagens):
    if LLM_MODE == "demo":
        return "Resumo DEMO (sem LLM): " + formatar_conversa(mensagens)[-1500:]
    from iai.app.core.assistant.guardrail import anonimizar_entrada
    from iai.app.core.assistant.llms import llm_rapido
    from iai.app.core.assistant.prompts import PROMPT_RESUMO

    conversa, _ = anonimizar_entrada(formatar_conversa(mensagens))
    reply = llm_rapido.invoke(PROMPT_RESUMO.format(conversa=conversa))
    if not isinstance(reply.content, str):
        return " ".join(
            str(c.get("text", "")) for c in reply.content if isinstance(c, dict)
        )
    return reply.content.strip()


def doc_id_da_sessao(session_id, user_id="user_test", kitchen_id=1):
    doc = col_sessoes.find_one(
        {
            "session_id": session_id,
            "user_id": user_id,
            "kitchen_id": kitchen_id,
            "active": True,
        }
    )
    return doc["_id"] if doc else None


def iniciar_sessao(session_id, user_id="user_test", kitchen_id=1):
    if doc_id_da_sessao(session_id, user_id, kitchen_id):
        return
    try:
        col_sessoes.insert_one(
            {
                "_id": str(uuid.uuid4()),
                "session_id": session_id,
                "user_id": user_id,
                "kitchen_id": kitchen_id,
                "active": True,
                "started_at": agora(),
                "updated_at": agora(),
                "summary": "",
                "messages": [],
            }
        )
    except DuplicateKeyError:
        pass  # índice composto resolve criação concorrente da mesma sessão


def salvar_mensagem(session_id, role, content, user_id="user_test", kitchen_id=1):
    iniciar_sessao(session_id, user_id, kitchen_id)
    identifier = doc_id_da_sessao(session_id, user_id, kitchen_id)
    col_sessoes.update_one(
        {"_id": identifier, "user_id": user_id, "kitchen_id": kitchen_id},
        {
            "$push": {"messages": {"role": role, "content": content}},
            "$set": {"updated_at": agora()},
        },
    )


def salvar_mensagem_juiz(
    session_id, conteudo, confianca, user_id="user_test", kitchen_id=1
):
    iniciar_sessao(session_id, user_id, kitchen_id)
    identifier = doc_id_da_sessao(session_id, user_id, kitchen_id)
    col_sessoes.update_one(
        {"_id": identifier, "user_id": user_id, "kitchen_id": kitchen_id},
        {
            "$push": {
                "messages": {
                    "role": "judge",
                    "content": conteudo,
                    "confianca": confianca,
                }
            },
            "$set": {"updated_at": agora()},
        },
    )


def encerrar_sessao(session_id, user_id="user_test", kitchen_id=1):
    identifier = doc_id_da_sessao(session_id, user_id, kitchen_id)
    if not identifier:
        return ""
    doc = col_sessoes.find_one(
        {"_id": identifier, "user_id": user_id, "kitchen_id": kitchen_id}
    )
    if not doc or not doc.get("messages"):
        return ""
    summary = gerar_resumo(doc["messages"])
    col_sessoes.update_one(
        {"_id": identifier, "user_id": user_id, "kitchen_id": kitchen_id},
        {"$set": {"summary": summary, "active": False, "updated_at": agora()}},
    )
    return summary


def recuperar_historico(user_id, busca="", limite=10, kitchen_id=1):
    rows = list(
        col_sessoes.find(
            {
                "user_id": user_id,
                "kitchen_id": kitchen_id,
                "summary": {"$nin": ["", None]},
            }
        )
        .sort("started_at", -1)
        .limit(200)
    )
    if busca.strip():
        rows = [rows[i] for i, _ in rank(busca, [d["summary"] for d in rows], limite)]
    return [
        SessaoResumo(doc_id=d["_id"], started_at=d["started_at"], summary=d["summary"])
        for d in rows[:limite]
    ]


def recuperar_mensagem(doc_id, user_id=None, kitchen_id=1):
    if user_id is None:
        raise HTTPException(401, "Identidade necessária para recuperar mensagens.")
    filtro = {"_id": doc_id, "user_id": user_id, "kitchen_id": kitchen_id}
    doc = col_sessoes.find_one(filtro, {"messages": 1})
    if not doc:
        return []
    return [
        MensagemHistorico(role=m["role"], content=m["content"])
        for m in doc["messages"]
        if m["role"] != "judge"
    ]


def mensagens_ativas(session_id, user_id, kitchen_id):
    identifier = doc_id_da_sessao(session_id, user_id, kitchen_id)
    return recuperar_mensagem(identifier, user_id, kitchen_id) if identifier else []
