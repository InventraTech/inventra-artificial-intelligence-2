from fastapi import FastAPI
from pydantic import BaseModel

from iai.app.graph import executar_fluxo_assessor

app = FastAPI(title="IAI - Inteligência Artificial do Inventra")


class ChatRequest(BaseModel):
    session_id: str
    pergunta: str


class ChatResponse(BaseModel):
    resposta: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(payload: ChatRequest) -> ChatResponse:
    resposta = executar_fluxo_assessor(
        pergunta_usuario=payload.pergunta,
        session_id=payload.session_id,
    )
    return ChatResponse(resposta=resposta)
