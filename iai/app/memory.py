import uuid
from datetime import datetime, timezone
from typing import Any

from pymongo import MongoClient

from iai.app.config import MONGO_CONNECTION
from iai.app.llms import llm_rapido
from iai.app.prompts import PROMPT_RESUMO
from iai.app.schemas import MensagemHistorico, SessaoResumo

mongo: MongoClient[dict[str, Any]] = MongoClient(MONGO_CONNECTION.get_secret_value())
db = mongo["dbIAI"]
col_sessoes = db["sessions"]

col_sessoes.create_index("session_id")
col_sessoes.create_index([("user_id", 1), ("started_at", -1)])

sessoes_ativas: dict = {}

def agora() -> datetime:
    return datetime.now(timezone.utc)

def formatar_conversa(mensagens: list[dict]) -> str:
    linhas = []
    for msg in mensagens:
        linhas.append(f"{msg['role']}: {msg['content']}")
    return "\n".join(linhas)

def _extrair_texto(conteudo: str | list) -> str:
    if isinstance(conteudo, str):
        return conteudo
    return " ".join(
        item if isinstance(item, str) else str(item.get("text", ""))
        for item in conteudo
    )

def gerar_resumo(mensagens: list[dict]) -> str:
    conversa = formatar_conversa(mensagens)
    resposta = llm_rapido.invoke(PROMPT_RESUMO.format(conversa=conversa))
    return _extrair_texto(resposta.content).strip()

def doc_id_da_sessao(session_id: str) -> str | None:
    doc_id = sessoes_ativas.get(session_id)
    if doc_id:
        return doc_id

    doc = col_sessoes.find_one(
        {"session_id": session_id, "resumo": {"$in": ["", None]}},
        {"_id": 1},
        sort=[("started_at", -1)]
    )
    if not doc:
        return None

    sessoes_ativas[session_id] = doc["_id"]
    return doc["_id"]

def iniciar_sessao(session_id: str, user_id: str = "user_test") -> None:
    if doc_id_da_sessao(session_id):
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

    sessoes_ativas[session_id] = doc_id

def salvar_mensagem(session_id: str, role: str, content: str, user_id: str = "user_test") -> None:
    iniciar_sessao(session_id, user_id=user_id)
    doc_id = doc_id_da_sessao(session_id)

    col_sessoes.update_one(
        {"_id": doc_id},
        {
            "$push": {"messages": {"role": role, "content": content}},
            "$set": {"updated_at": agora()}
        }
    )

def encerrar_sessao(session_id: str) -> str:
    doc_id = doc_id_da_sessao(session_id)

    if not doc_id:
        return ""

    doc = col_sessoes.find_one({"_id": doc_id})
    
    if not doc or not doc.get("messages"):
        sessoes_ativas.pop(session_id, None)
        return ""

    resumo = gerar_resumo(doc["messages"])

    col_sessoes.update_one(
        {"_id": doc_id},
        {"$set": {"summary": resumo, "updated_at": agora()}}
    )

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

def recuperar_mensagem(doc_id: str) -> list[MensagemHistorico]:
    doc = col_sessoes.find_one({"_id": doc_id}, {"messages": 1})
    if not doc:
        return []
    return [MensagemHistorico(role=m["role"], content=m["content"]) for m in doc["messages"]]