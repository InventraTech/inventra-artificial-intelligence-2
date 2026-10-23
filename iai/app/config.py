import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import SecretStr

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
FAQ_PATH = DATA_DIR / "faq_inventra.jsonl"

load_dotenv(BASE_DIR / ".env")
# Compatibilidade com a configuração local mantida junto ao código da aplicação.
# Ambiente do processo e iai/.env têm prioridade; não copiar ou sobrescrever segredos.
load_dotenv(BASE_DIR / "app" / ".env")

GEMINI_API_KEY = SecretStr(os.getenv("GEMINI_API_KEY", ""))
GROQ_API_KEY = SecretStr(os.getenv("GROQ_API_KEY", ""))
TYPESAFE_API_KEY = SecretStr(os.getenv("TYPESAFE_API_KEY", ""))
JEV_MODEL = os.getenv("JEV_MODEL", "jev-latest")
JEV_HTTP_TIMEOUT = float(os.getenv("JEV_HTTP_TIMEOUT", "10"))
MONGO_CONNECTION = SecretStr(os.getenv("MONGO_CONNECTION", "mongodb://localhost:27017"))

OBRIGATORIAS = {
    "GEMINI_API_KEY": GEMINI_API_KEY,
    "GROQ_API_KEY": GROQ_API_KEY,
    "TYPESAFE_API_KEY": TYPESAFE_API_KEY,
    "MONGO_CONNECTION": MONGO_CONNECTION,
}

AUTH_MODE = os.getenv("IAI_AUTH_MODE", "spring")
BUSINESS_MODE = os.getenv("IAI_BUSINESS_MODE", "spring")
STORAGE_MODE = os.getenv("IAI_STORAGE_MODE", "mongo")
LLM_MODE = os.getenv("IAI_LLM_MODE", "live")
API_URL = os.getenv("INVENTRA_API_URL", "http://localhost:8080").rstrip("/")
HTTP_TIMEOUT = float(os.getenv("INVENTRA_HTTP_TIMEOUT", "10"))
CORS_ORIGINS = [
    o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()
]

for nome, modo, permitidos in (
    ("auth", AUTH_MODE, {"mock", "spring"}),
    ("business", BUSINESS_MODE, {"mock", "spring"}),
    ("storage", STORAGE_MODE, {"mock", "mongo"}),
    ("llm", LLM_MODE, {"demo", "live"}),
):
    if modo not in permitidos:
        raise ValueError(f"Modo inválido para {nome}.")
if BUSINESS_MODE == "spring" and AUTH_MODE != "spring":
    raise ValueError(
        "A API real exige autenticação Spring; identidade mock não é aceita."
    )
api_address = urlsplit(API_URL)
if (
    api_address.scheme not in {"http", "https"}
    or not api_address.hostname
    or api_address.username
    or api_address.password
    or api_address.query
    or api_address.fragment
):
    raise ValueError("INVENTRA_API_URL inválida; não use credenciais na URL.")
if api_address.scheme != "https" and api_address.hostname not in {
    "localhost",
    "127.0.0.1",
    "::1",
}:
    raise ValueError("JWT exige HTTPS fora de localhost.")


def business_date():
    """Dia operacional de Brasília (UTC-3); timestamps/auditoria permanecem UTC."""
    return datetime.now(timezone(timedelta(hours=-3))).date()


def validar_config() -> list[str]:
    """Devolve a lista de problemas de configuração (vazia = tudo certo)."""
    problemas = []
    for nome, valor in OBRIGATORIAS.items():
        if not valor.get_secret_value():
            problemas.append(f"Variável ausente no .env: {nome}")
    return problemas
