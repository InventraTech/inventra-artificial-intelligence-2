import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from iai.app.approvals import expire_pending
from iai.app.config import (
    AUTH_MODE,
    BUSINESS_MODE,
    CORS_ORIGINS,
    LLM_MODE,
    STORAGE_MODE,
)
from iai.app.metrics import summarize
from iai.app.routes import approvals, chat, queries
from iai.app.security import Authenticated
from iai.app.storage import get_db, initialize_storage


async def expiration_worker():
    while True:
        try:
            await asyncio.to_thread(expire_pending)
        except PyMongoError:
            pass  # /health informa indisponibilidade; não converter execução em sucesso
        await asyncio.sleep(30)


@asynccontextmanager
async def lifespan(app):
    try:
        await asyncio.to_thread(initialize_storage)
    except PyMongoError:
        pass
    task = asyncio.create_task(expiration_worker())
    yield
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


app = FastAPI(
    title="IAI - Inteligência Artificial do Inventra",
    version="0.2.0",
    description="Consultas e propostas aprovadas. Modos mock são explicitamente identificados.",
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
app.include_router(approvals.router)
app.include_router(queries.router)


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


@app.get("/metrics", tags=["métricas"])
def metrics(ctx: Authenticated):
    return summarize(ctx.identity)
