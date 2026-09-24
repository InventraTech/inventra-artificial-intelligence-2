import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from iai.app.config import MONGO_CONNECTION
from iai.app.domain import DomainError
from iai.app.schemas import MensagemHistorico, SessaoResumo

mongo: MongoClient[dict[str, Any]] = MongoClient(
    MONGO_CONNECTION.get_secret_value(), connect=False, serverSelectionTimeoutMS=5000,
    tz_aware=True,
)
db = mongo["dbIAI"]
col_sessoes = db["sessions"]

sessoes_ativas: dict[str, str] = {}

def agora() -> datetime:
    return datetime.now(timezone.utc)

def gerar_resumo(mensagens: list[dict]) -> str:
    """Extract a recorded assistant response; never infer that a write happened."""
    respostas = [m["content"] for m in mensagens if m.get("role") == "iai" and m.get("content")]
    if not respostas:
        return "Sessão sem resposta registrada do assistente."
    ultima = respostas[-1]
    if len(ultima) > 1200:
        return "Trecho da última resposta registrada (consulte o histórico completo):\n" + ultima[:1200] + "…"
    return "Última resposta registrada:\n" + ultima


def doc_id_da_sessao(session_id: str, user_id: str = "user_test") -> str | None:
    key = f"{user_id}:{session_id}"
    doc_id = sessoes_ativas.get(key)
    if doc_id:
        return doc_id

    doc = col_sessoes.find_one(
        {"session_id": session_id, "user_id": user_id, "summary": {"$in": ["", None]}},
        {"_id": 1},
        sort=[("started_at", -1)]
    )
    if not doc:
        return None

    sessoes_ativas[key] = doc["_id"]
    return doc["_id"]

def iniciar_sessao(session_id: str, user_id: str = "user_test") -> None:
    if doc_id_da_sessao(session_id, user_id):
        return

    doc_id = str(uuid.uuid4())
    agora_dt = agora()

    col_sessoes.insert_one({
        "_id": doc_id,
        "session_id": session_id,
        "user_id": user_id,
        "started_at": agora_dt,
        "updated_at": agora_dt,
        "summary": "",
        "messages": []
    })

    sessoes_ativas[f"{user_id}:{session_id}"] = doc_id

def salvar_mensagem(session_id: str, role: str, content: str, user_id: str = "user_test") -> None:
    iniciar_sessao(session_id, user_id=user_id)
    doc_id = doc_id_da_sessao(session_id, user_id)

    col_sessoes.update_one(
        {"_id": doc_id},
        {
            "$push": {"messages": {"role": role, "content": content}},
            "$set": {"updated_at": agora()}
        }
    )

def encerrar_sessao(session_id: str, user_id: str = "user_test") -> str:
    doc_id = doc_id_da_sessao(session_id, user_id)

    if not doc_id:
        return ""

    doc = col_sessoes.find_one({"_id": doc_id})

    if not doc or not doc.get("messages"):
        return ""

    if doc.get("pending_operation", {}).get("status") == "executing":
        raise DomainError("CONFLICT", "Aguarde a conclusão da operação antes de encerrar a sessão.")

    resumo = gerar_resumo(doc["messages"])

    updated = col_sessoes.update_one(
        {"_id": doc_id, "pending_operation.status": {"$ne": "executing"}},
        {"$set": {"summary": resumo, "updated_at": agora()},
         "$unset": {"pending_operation": ""}}
    )

    if not updated.matched_count:
        raise DomainError("CONFLICT", "Uma operação começou durante o encerramento da sessão.")
    sessoes_ativas.pop(f"{user_id}:{session_id}", None)

    return resumo

def recuperar_historico(user_id: str, busca: str = "", limite: int = 10) -> list[SessaoResumo]:
    filtro = {"user_id": user_id, "summary": {"$nin": ["", None]}}
    docs = (
        col_sessoes
        .find(filtro, {"summary": 1, "started_at": 1})
        .sort("started_at", -1)
        .limit(limite)
    )

    return [
        SessaoResumo(doc_id=doc["_id"], started_at=doc["started_at"], summary=doc["summary"])
        for doc in docs
    ]

def recuperar_mensagem(doc_id: str, user_id: str = "user_test") -> list[MensagemHistorico]:
    doc = col_sessoes.find_one({"_id": doc_id, "user_id": user_id}, {"messages": 1})
    if not doc:
        return []
    return [MensagemHistorico(role=m["role"], content=m["content"]) for m in doc["messages"]]
