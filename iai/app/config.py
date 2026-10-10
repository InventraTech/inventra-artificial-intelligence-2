import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import SecretStr

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FRONTEND_DIR = BASE_DIR / "frontend"
FAQ_PATH = DATA_DIR / "faq_inventra.jsonl"

load_dotenv(BASE_DIR / ".env")

GEMINI_API_KEY = SecretStr(os.getenv("GEMINI_API_KEY", ""))
GROQ_API_KEY = SecretStr(os.getenv("GROQ_API_KEY", ""))
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
    "MONGO_CONNECTION": MONGO_CONNECTION
}

def validar_config() -> list[str]:
    """Devolve a lista de problemas de configuração (vazia = tudo certo)."""
    problemas = []
    for nome, valor in OBRIGATORIAS.items():
        if not valor:
            problemas.append(f"Variável ausente no .env: {nome}")
    return problemas