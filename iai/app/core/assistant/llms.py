import re

from langchain_core.language_models import BaseChatModel
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_openai import ChatOpenAI
from pydantic import SecretStr

from iai.app.core.assistant.rodizio import Vigia, montar
from iai.app.core.config import (
    GEMINI_API_KEYS,
    GEMINI_MODEL,
    GROQ_API_KEYS,
    LLM_TIMEOUT,
    LLM_TIMEOUT_LEVE,
    LUNA_MODEL_OPENAI,
    LUNA_MODEL_OPENROUTER,
    LUNA_REASONING_EFFORT,
    OPENAI_API_KEY,
    OPENROUTER_API_KEY,
    PENSAMENTO,
)

Candidato = tuple[str, BaseChatModel]

_LOOKAROUND = re.compile(r"\(\?(?:=|!|<=|<!)")


def _sem_lookaround(valor):
    """Tira os `pattern` com lookaround do schema: a OpenAI recusa o pedido inteiro (HTTP 400) por causa deles."""
    if isinstance(valor, dict):
        return {
            chave: _sem_lookaround(item)
            for chave, item in valor.items()
            if not (
                chave == "pattern"
                and isinstance(item, str)
                and _LOOKAROUND.search(item)
            )
        }
    if isinstance(valor, list):
        return [_sem_lookaround(item) for item in valor]
    return valor


class ChatOpenAISemLookaround(ChatOpenAI):
    """O Pydantic gera `pattern` com lookaround para `Decimal`; a OpenAI não aceita isso nas tools."""

    def _get_request_payload(self, input_, *, stop=None, **kwargs):
        payload = super()._get_request_payload(input_, stop=stop, **kwargs)
        for campo in ("tools", "response_format"):
            if campo in payload:
                payload[campo] = _sem_lookaround(payload[campo])
        return payload

SEM_CHAVE = SecretStr("unconfigured")


def chave_ou_aviso(chave: SecretStr) -> SecretStr:
    return chave if chave.get_secret_value() else SEM_CHAVE


# Cada papel é uma lista de candidatos (uma entrada por chave de cada provedor) em ordem de preferência.
# Quando um falha (429, crédito, 5xx, timeout), a mesma requisição segue para o próximo, sem avisar o
# usuário (rodizio.py). Os SDKs não repetem sozinhos (max_retries=0): repetir a mesma chave só atrasa.


def _gemini(*, pensante: bool = False) -> list[Candidato]:
    # Gemini 3.x pede a temperatura padrão; só a série 2.x usa a baixa de antes.
    temperatura = {"temperature": 0.2} if GEMINI_MODEL.startswith("gemini-2") else {}
    candidatos: list[Candidato] = []
    for n, chave in enumerate(GEMINI_API_KEYS or (SEM_CHAVE,), 1):
        rotulo = f"gemini#{n}"
        candidatos.append(
            (
                rotulo,
                ChatGoogleGenerativeAI(
                    model=GEMINI_MODEL,
                    top_p=0.95,
                    api_key=chave,
                    timeout=LLM_TIMEOUT,
                    max_retries=1,
                    # Só os especialistas expõem o raciocínio (resumos que o /chat/stream mostra ao vivo).
                    # Gastam tokens de saída; IAI_PENSAMENTO=off desliga.
                    include_thoughts=PENSAMENTO if pensante else None,
                    callbacks=[Vigia(rotulo)],
                    **temperatura,
                ),
            )
        )
    return candidatos


def _groq(modelo: str, **opcoes) -> list[Candidato]:
    candidatos: list[Candidato] = []
    for n, chave in enumerate(GROQ_API_KEYS or (SEM_CHAVE,), 1):
        rotulo = f"groq/{modelo.rsplit('/', 1)[-1]}#{n}"
        candidatos.append(
            (
                rotulo,
                ChatGroq(
                    model=modelo,
                    api_key=chave,
                    timeout=LLM_TIMEOUT_LEVE,
                    max_retries=0,
                    callbacks=[Vigia(rotulo)],
                    **opcoes,
                ),
            )
        )
    return candidatos


def _luna(timeout: float) -> list[Candidato]:
    """GPT Luna: OpenRouter primeiro (plano A), OpenAI direto depois (plano B). Só entram se houver chave."""
    candidatos: list[Candidato] = []
    comuns = {
        "timeout": timeout,
        "max_retries": 0,
        "stream_usage": True,
        "reasoning_effort": LUNA_REASONING_EFFORT,
    }
    if OPENROUTER_API_KEY.get_secret_value():
        candidatos.append(
            (
                "openrouter/luna",
                ChatOpenAISemLookaround(
                    model=LUNA_MODEL_OPENROUTER,
                    api_key=OPENROUTER_API_KEY,
                    base_url="https://openrouter.ai/api/v1",
                    default_headers={"X-Title": "Inventra IAI"},
                    callbacks=[Vigia("openrouter/luna")],
                    **comuns,
                ),
            )
        )
    if OPENAI_API_KEY.get_secret_value():
        candidatos.append(
            (
                "openai/luna",
                ChatOpenAISemLookaround(
                    model=LUNA_MODEL_OPENAI,
                    api_key=OPENAI_API_KEY,
                    callbacks=[Vigia("openai/luna")],
                    **comuns,
                ),
            )
        )
    return candidatos


_groq_rapido = _groq("openai/gpt-oss-20b", temperature=0.0, stop_sequences=None)

# Saídas estruturadas e visão (guardrail de arquivos, descrição de imagem): Gemini, com a Luna de reserva.
llm_gemini = montar([*_gemini(), *_luna(LLM_TIMEOUT)])

# Roteador, orquestrador, guardrail de escopo, resumos: Groq (rápido e barato), depois todo o resto.
llm_rapido = montar(
    [*_groq_rapido, *_luna(LLM_TIMEOUT_LEVE), *_gemini()],
)

# A Groq (plano on_demand) limita este modelo a 2.000 tokens por minuto e confere o tamanho do pedido
# contra o máximo de resposta. Sem max_tokens o pedido passava de 2.400 e falhava com 413 de vez em
# quando; a classificação cabe em poucas centenas. Só existe na Groq: o guardrail de insulto cai para
# o llm_rapido quando todas as chaves falham.
llm_guardrail = montar(
    _groq(
        "openai/gpt-oss-safeguard-20b",
        temperature=0.0,
        max_tokens=300,
        reasoning_effort="low",
    )
)

# Especialistas: Luna (OpenRouter, OpenAI), depois Gemini com resumo do raciocínio, e por último o modelo
# rápido da Groq, como já era antes do rodízio.
llm_especialista = montar([*_luna(LLM_TIMEOUT), *_gemini(pensante=True), *_groq_rapido])

modelo_transcricao = "whisper-large-v3-turbo"
