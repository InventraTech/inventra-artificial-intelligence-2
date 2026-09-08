from fastapi import FastAPI

from iai.app.routes import chat

app = FastAPI(
    title="IAI - Inteligência Artificial do Inventra",
    description="Assistente do ecossistema Inventra focado na gestão de estoques de alimentos e redução de desperdício (ODS 12).",
    version="0.1.1"
    )

app.include_router(chat.router)

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}