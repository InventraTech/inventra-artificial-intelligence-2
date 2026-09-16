import re
import unicodedata

from langchain_core.prompts import ChatPromptTemplate

from iai.app.llms import llm_rapido, llm_guardrail

from iai.app.prompts import (
    GUARDRAIL_ENTRADA_SYSTEM_PROMPT,
    GUARDRAIL_INSULTO_SYSTEM_PROMPT,
)

from iai.app.schemas import ResultadoGuardrail


TERMOS_PROIBIDOS = [
    r"ignor[ae]\s+(todas\s+)?(as\s+)?instrucoes",
    r"desconsider[ae]\s+(as\s+)?instrucoes",
    r"esqueca\s+o\s+que\s+(eu\s+)?disse",
    r"revele?\s+(o\s+)?(seu\s+)?(system\s+)?prompt",
    r"aja\s+como\s+(uma\s+)?ia\s+sem\s+restricoes",
    r"ignore\s+(all\s+)?(previous|prior)\s+instructions",
    r"disregard\s+(the\s+)?(above|previous)",
    r"act\s+as\s+(an?\s+)?(unrestricted|uncensored)",
]

PADRAO_EMAIL = r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"
PADRAO_CPF = r"\b(?:\d{3}\.\d{3}\.\d{3}-\d{2}|\d{11})\b"
PADRAO_RG = r"\b\d{1,2}\.\d{3}\.\d{3}-[\dXx]\b"
PADRAO_TELEFONE = r"\(?\d{2}\)?\s?9?\d{4}-\d{4}"

PADROES_PII = {
    "EMAIL": PADRAO_EMAIL,
    "CPF": PADRAO_CPF,
    "RG": PADRAO_RG,
    "TELEFONE": PADRAO_TELEFONE,
}


def normalizar_texto(texto: str) -> str:
    """Baixa a caixa e remove acentos, pra comparação robusta a variações de escrita."""

    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    return sem_acento.lower()


def verificar(texto: str) -> dict | None:
    """Verificações determinísticas antes dos modelos."""

    texto_normalizado = normalizar_texto(texto)

    for padrao in TERMOS_PROIBIDOS:
        if re.search(padrao, texto_normalizado):
            return {
                "bloqueado": True,
                "motivo": "filtro_hardcoded",
                "mensagem": (
                    "Sua mensagem contém conteúdo não permitido. "
                    "Por favor, mantenha o foco na gestão do restaurante."
                ),
            }

    return None


def anonimizar_entrada(texto: str) -> tuple[str, dict]:
    """
    Substitui dados sensíveis por tokens e guarda os valores originais.
    """

    mapa_pii = {}
    texto_anonimizado = texto

    for prefixo, padrao in PADROES_PII.items():
        valores = re.findall(padrao, texto_anonimizado)

        for i, valor in enumerate(valores):
            token = f"[{prefixo}_{i}]"
            mapa_pii[token] = valor
            texto_anonimizado = texto_anonimizado.replace(valor, token)

    return texto_anonimizado, mapa_pii


def verificar_insulto(mensagem: str) -> dict | None:
    """
    Usa o modelo de moderação para identificar insultos/abuso.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", GUARDRAIL_INSULTO_SYSTEM_PROMPT),
        ("human", "{mensagem}"),
    ])

    chain = prompt | llm_guardrail.with_structured_output(
        ResultadoGuardrail
    )

    try:
        res = chain.invoke({"mensagem": mensagem})

        assert isinstance(res, ResultadoGuardrail)

        if res.bloqueado:
            return {
                "bloqueado": True,
                "motivo": "insulto",
                "mensagem": res.mensagem,
            }

        return None

    except Exception:
        return {
            "bloqueado": True,
            "motivo": "erro_api_safeguard",
            "mensagem": "Erro interno de segurança. Tente novamente.",
        }


def guardrail_insulto(mensagem: str) -> dict:
    """
    Primeiro guardrail: filtro determinístico + modelo de moderação
    (identifica insultos/abuso).
    """

    resultado = verificar(mensagem)

    if resultado:
        return resultado

    resultado = verificar_insulto(mensagem)

    if resultado:
        return resultado

    return {"bloqueado": False, "motivo": "aprovado", "mensagem": ""}


def guardrail_escopo(mensagem: str) -> dict:
    """
    Segundo guardrail: verifica se a mensagem pertence ao escopo do Inventra.
    """

    prompt = ChatPromptTemplate.from_messages([
        ("system", GUARDRAIL_ENTRADA_SYSTEM_PROMPT),
        ("human", "{mensagem}"),
    ])

    chain = prompt | llm_rapido.with_structured_output(
        ResultadoGuardrail
    )

    try:
        res = chain.invoke({"mensagem": mensagem})

        assert isinstance(res, ResultadoGuardrail)

        return {
            "bloqueado": res.bloqueado,
            "motivo": res.motivo,
            "mensagem": res.mensagem,
        }

    except Exception:
        return {
            "bloqueado": True,
            "motivo": "erro_api",
            "mensagem": "Erro interno de segurança. Tente novamente.",
        }


def guardrail_saida(
    texto_gerado: str,
    mapa_pii: dict,
    extra: dict,
) -> dict:
    """
    Restaura os dados originais na resposta.
    """

    texto_final = texto_gerado

    if mapa_pii:
        for token, valor_original in mapa_pii.items():
            texto_final = texto_final.replace(
                token,
                valor_original,
            )

    return {
        "conteudo": texto_final
    }