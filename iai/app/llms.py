from langchain.agents.middleware import wrap_model_call
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq

from iai.app.config import GEMINI_API_KEY, GROQ_API_KEY

llm_gemini = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2,
    top_p=0.95,
    api_key=GEMINI_API_KEY
)

llm_rapido = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.0,
    api_key=GROQ_API_KEY,
    stop_sequences=None
)

llm_guardrail = ChatGroq(
    model="openai/gpt-oss-safeguard-20b",
    temperature=0.0,
    api_key=GROQ_API_KEY
)

llm_especialista = llm_gemini


@wrap_model_call
def specialist_fallback(request, handler):
    """Fallback surrounds only model inference, never replays a tool execution."""
    try:
        return handler(request)
    except Exception:  # noqa: BLE001 -- model-only fallback, no tool side effects
        return handler(request.override(model=llm_rapido))
