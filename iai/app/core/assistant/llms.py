from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from pydantic import SecretStr

from iai.app.core.config import GEMINI_API_KEY, GROQ_API_KEY, PENSAMENTO


def chave_ou_aviso(chave: SecretStr) -> SecretStr:
    return chave if chave.get_secret_value() else SecretStr("unconfigured")


llm_gemini = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,
    top_p=0.95,
    api_key=chave_ou_aviso(GEMINI_API_KEY),
)

# Só os especialistas expõem o raciocínio (resumos que o /chat/stream mostra ao vivo). Gastam
# tokens de saída; IAI_PENSAMENTO=off desliga. Saídas estruturadas seguem no llm_gemini puro.
llm_gemini_pensante = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,
    top_p=0.95,
    api_key=chave_ou_aviso(GEMINI_API_KEY),
    include_thoughts=PENSAMENTO,
)

llm_rapido = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.0,
    api_key=chave_ou_aviso(GROQ_API_KEY),
    stop_sequences=None,
)

llm_guardrail = ChatGroq(
    model="openai/gpt-oss-safeguard-20b",
    temperature=0.0,
    api_key=chave_ou_aviso(GROQ_API_KEY),
)

llm_especialista = llm_gemini_pensante.with_fallbacks([llm_rapido])

modelo_transcricao = "whisper-large-v3-turbo"
