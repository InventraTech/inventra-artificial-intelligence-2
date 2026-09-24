from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pymongo.errors import PyMongoError

from iai.app.domain import DomainError, failure
from iai.app.routes import chat

app = FastAPI(
    title="IAI - Inteligência Artificial do Inventra",
    description="Assistente do ecossistema Inventra focado na gestão de estoques de alimentos e redução de desperdício (ODS 12).",
    version="0.1.1"
    )

app.include_router(chat.router)


@app.exception_handler(DomainError)
async def domain_error_handler(request, exc: DomainError):
    status = {"FORBIDDEN": 403, "UNAUTHENTICATED": 401, "NOT_FOUND": 404,
              "DATABASE_UNAVAILABLE": 503, "TIMEOUT": 504}.get(exc.code, 409)
    return JSONResponse(status_code=status, content=failure(exc))


@app.exception_handler(PyMongoError)
async def session_error_handler(request, exc: PyMongoError):
    return JSONResponse(status_code=503, content={"success": False, "code": "SESSION_UNAVAILABLE",
        "message": "Sessão indisponível. Nenhuma confirmação de sucesso foi obtida; consulte o estado antes de repetir uma operação."})

@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
