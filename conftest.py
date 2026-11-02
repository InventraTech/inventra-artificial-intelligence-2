import os

os.environ.setdefault("GEMINI_API_KEY", "test-key")
os.environ.setdefault("GROQ_API_KEY", "test-key")
# Chaves reais do iai/.env nunca entram nos testes: o rodízio só enxerga as chaves fake de cima.
for nome in (
    "OPEN_AI_KEY",
    "OPENAI_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "OPENROUTER_API_KEY",
    *(f"{base}_{n}" for base in ("GEMINI_API_KEY", "GROQ_API_KEY") for n in range(2, 21)),
):
    os.environ[nome] = ""
os.environ["IAI_AUTH_MODE"] = "mock"
os.environ["IAI_BUSINESS_MODE"] = "mock"
os.environ["IAI_STORAGE_MODE"] = "mock"
os.environ["IAI_LLM_MODE"] = "demo"
os.environ["JEV_ESCOPO"] = "todas"
os.environ["LANGSMITH_TRACING"] = "false"
os.environ["CORS_ORIGINS"] = "http://localhost:3000,http://localhost:5173"

import pytest


@pytest.fixture(autouse=True)
def rodizio_sem_espera():
    """Falha de um teste não pode deixar uma chave "em espera" para o teste seguinte."""
    from iai.app.core.assistant.rodizio import registro

    registro.limpar()
    yield
    registro.limpar()


@pytest.fixture(autouse=True)
def isolated_database():
    from iai.app.core.infra.storage import get_db, initialize_storage

    db = get_db()
    for collection in db.list_collection_names():
        db.drop_collection(collection)
    initialize_storage()
    yield
