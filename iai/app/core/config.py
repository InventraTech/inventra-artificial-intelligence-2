import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import SecretStr

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
FAQ_PATH = DATA_DIR / "faq_inventra.jsonl"

load_dotenv(BASE_DIR / ".env")
# Configuração local legada; variáveis do processo e iai/.env têm prioridade.
load_dotenv(BASE_DIR / "app" / ".env")

GEMINI_API_KEY = SecretStr(os.getenv("GEMINI_API_KEY", ""))
GROQ_API_KEY = SecretStr(os.getenv("GROQ_API_KEY", ""))
TYPESAFE_API_KEY = SecretStr(os.getenv("TYPESAFE_API_KEY", ""))
JEV_MODEL = os.getenv("JEV_MODEL", "jev-1.13.0")
JEV_APPROVAL_THRESHOLD = float(os.getenv("JEV_APPROVAL_THRESHOLD", "0.70"))
JEV_TIMEOUT = float(os.getenv("JEV_TIMEOUT", "10"))
# "escrita": só as tools propor_* passam pelo JUIZ (TypeSafe); consultas, FAQ e histórico
# seguem com as validações determinísticas do gate (schema, perfil, segredos, uso único).
# "todas": toda tool consulta o JEV antes de executar (mais lento e sujeito a falso bloqueio).
JEV_ESCOPO = os.getenv("JEV_ESCOPO", "escrita").strip().lower()
if JEV_ESCOPO not in {"escrita", "todas"}:
    raise ValueError("JEV_ESCOPO inválido; use 'escrita' ou 'todas'.")
if not 0.5 < JEV_APPROVAL_THRESHOLD <= 1 or not 0 < JEV_TIMEOUT <= 60:
    raise ValueError("Configuração JEV inválida: limiar > 0.5 e <= 1; timeout > 0 e <= 60.")
MONGO_CONNECTION = SecretStr(os.getenv("MONGO_CONNECTION", "mongodb://localhost:27017"))

PRECOS_MODELOS_USD_POR_MILHAO: dict[str, tuple[float, float]] = {
    "gemini-2.5-flash":             (0.30, 2.50),
    "openai/gpt-oss-20b":           (0.075, 0.30),
    "openai/gpt-oss-safeguard-20b": (0.075, 0.30),
}

REQUISICOES_POR_USUARIO_SEMANA = float(os.getenv("METRICAS_REQ_USUARIO_SEMANA", "10"))
VALOR_POR_RESOLUCAO_USD = float(os.getenv("METRICAS_VALOR_RESOLUCAO_USD", "1.50"))
CENARIOS_USUARIOS_SEMANAIS = (100, 1000)

PERIODOS: dict[str, tuple[int, float]] = {
    "semana": (7, 1.0),
    "mes":    (30, 52 / 12),
}

CORS_ORIGINS = [
    origem.strip()
    for origem in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://localhost:5173").split(",")
    if origem.strip()
]

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
