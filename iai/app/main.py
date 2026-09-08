from fastapi import FastAPI
from iai.app.graph import executar_fluxo_assessor
from iai.app.schemas import ChatRequest, ChatResponse

app = FastAPI(
    title="IAI - Inteligência Artificial do Inventra",
    description="Assistente do ecossistema Inventra focado na gestão de estoques de alimentos e redução de desperdício (ODS 12).",
    version="0.1.0"
    )

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