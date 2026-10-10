from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from iai.app.config import CORS_ORIGINS
from iai.app.routes import chat, metrics

app = FastAPI(
    title="IAI - Inteligência Artificial do Inventra",
    description="Assistente do ecossistema Inventra focado na gestão de estoques de alimentos e redução de desperdício (ODS 12).",
    version="0.1.1"
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

app.include_router(chat.router)
app.include_router(metrics.router)

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
