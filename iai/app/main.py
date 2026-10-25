import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from iai.app.core.config import (
    AUTH_MODE,
    BUSINESS_MODE,
    CORS_ORIGINS,
    LLM_MODE,
    STORAGE_MODE,
)
from iai.app.core.infra.storage import get_db, initialize_storage
from iai.app.core.routes import chat, metrics, queries

# Sem isso o Python só mostra WARNING+: as decisões do JEV (INFO) nunca chegavam aos logs do Render.
logging.basicConfig(
    level=logging.INFO, format="%(levelname)s:%(name)s: %(message)s"
)
for _barulhento in ("httpx", "httpcore", "pymongo", "urllib3"):
    logging.getLogger(_barulhento).setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app):
    try:
        await asyncio.to_thread(initialize_storage)
    except PyMongoError:
        pass
    yield


app = FastAPI(
    title="IAI - Inteligência Artificial do Inventra",
    version="0.2.0",
    description="Consultas e ações validadas. A confirmação humana (HITL) ainda não está implementada. Modos mock são explicitamente identificados.",
    lifespan=lifespan,
)
if CORS_ORIGINS:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=CORS_ORIGINS,
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
app.include_router(chat.router)
app.include_router(queries.router)
app.include_router(metrics.router)


@app.middleware("http")
async def modes(request, call_next):
    response = await call_next(request)
    response.headers["X-IAI-Auth-Mode"] = AUTH_MODE
    response.headers["X-IAI-Business-Mode"] = BUSINESS_MODE
    response.headers["X-IAI-Storage-Mode"] = STORAGE_MODE
    response.headers["X-IAI-LLM-Mode"] = LLM_MODE
    return response


@app.exception_handler(PyMongoError)
async def database_error(request, error):
    return JSONResponse(
        status_code=503,
        content={
            "detail": "Persistência indisponível. Resultado de escrita em andamento pode exigir reconciliação."
        },
    )


@app.get("/health")
def health():
    try:
        get_db().command("ping")
        storage = "ok"
    except PyMongoError:
        storage = "indisponível"
    return {
        "status": "ok" if storage == "ok" else "degraded",
        "storage": storage,
        "modes": {
            "auth": AUTH_MODE,
            "business": BUSINESS_MODE,
            "storage": STORAGE_MODE,
            "llm": LLM_MODE,
        },
        "warning": "Modo demonstrativo não autentica usuários reais nem altera estoque real."
        if "mock" in {AUTH_MODE, BUSINESS_MODE, STORAGE_MODE} or LLM_MODE == "demo"
        else None,
    }
