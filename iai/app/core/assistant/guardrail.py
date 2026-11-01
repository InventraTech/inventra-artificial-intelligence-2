import logging
import re
import unicodedata

from langchain_core.prompts import ChatPromptTemplate

from iai.app.core.assistant.llms import llm_guardrail, llm_rapido
from iai.app.core.assistant.prompts import (
    GUARDRAIL_ENTRADA_SYSTEM_PROMPT,
    GUARDRAIL_INSULTO_SYSTEM_PROMPT,
)
from iai.app.core.assistant.responses import resumo_para_log
from iai.app.core.infra.privacy import SECRET_PATTERN, remove_secrets
from iai.app.core.schemas import ResultadoGuardrail

logger = logging.getLogger(__name__)

# Mensagem quando nenhum modelo de verificação respondeu: o usuário sabe que é do sistema, não dele.
MENSAGEM_VERIFICACAO_INDISPONIVEL = "A verificação de segurança está sobrecarregada agora. Tente de novo em alguns segundos."

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

PADRAO_SEGREDO = SECRET_PATTERN


def normalizar_texto(texto: str) -> str:
    """Baixa a caixa e remove acentos, pra comparação robusta a variações de escrita."""

    sem_acento = (
        unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii")
    )
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
    # Segredos nunca entram no mapa de restauração, no histórico ou nos modelos.
    texto_anonimizado = remove_secrets(texto)

    for prefixo, padrao in PADROES_PII.items():
        valores = re.findall(padrao, texto_anonimizado)

        for i, valor in enumerate(valores):
            token = f"[{prefixo}_{i}]"
            mapa_pii[token] = valor
            texto_anonimizado = texto_anonimizado.replace(valor, token)

    return texto_anonimizado, mapa_pii


def _classificar(prompt_do_sistema: str, mensagem: str, modelos) -> ResultadoGuardrail:
    """Tenta cada modelo na ordem: o primeiro que responder decide. Se todos falharem, repassa o último erro."""
    prompt = ChatPromptTemplate.from_messages(
        [("system", prompt_do_sistema), ("human", "{mensagem}")]
    )
    ultimo_erro: Exception | None = None
    for modelo in modelos:
        try:
            res = (prompt | modelo.with_structured_output(ResultadoGuardrail)).invoke(
                {"mensagem": mensagem}
            )
            assert isinstance(res, ResultadoGuardrail)
            return res
        except Exception as exc:  # noqa: BLE001 - tenta o próximo modelo; o último erro sobe
            logger.warning("Modelo de verificação falhou: %s", resumo_para_log(exc))
            ultimo_erro = exc
    raise ultimo_erro or RuntimeError("Nenhum modelo de verificação configurado.")


def verificar_insulto(mensagem: str) -> dict | None:
    """
    Usa o modelo de moderação para identificar insultos/abuso.

    O modelo de segurança tem limite baixo na Groq; se ele falhar, o modelo rápido faz a mesma
    classificação. Só bloqueia por erro quando nenhum dos dois responde.
    """

    try:
        res = _classificar(
            GUARDRAIL_INSULTO_SYSTEM_PROMPT, mensagem, (llm_guardrail, llm_rapido)
        )

        if res.bloqueado:
            return {
                "bloqueado": True,
                "motivo": "insulto",
                "mensagem": res.mensagem,
            }

        return None

    except Exception as exc:  # noqa: BLE001 - fail-safe: sem nenhum modelo, a mensagem não passa
        logger.error("Guardrail de insulto indisponível: %s", resumo_para_log(exc))
        return {
            "bloqueado": True,
            "motivo": "erro_api_safeguard",
            "mensagem": MENSAGEM_VERIFICACAO_INDISPONIVEL,
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

    try:
        res = _classificar(GUARDRAIL_ENTRADA_SYSTEM_PROMPT, mensagem, (llm_rapido,))

        return {
            "bloqueado": res.bloqueado,
            "motivo": res.motivo,
            "mensagem": res.mensagem,
        }

    except Exception as exc:  # noqa: BLE001 - fail-safe: sem modelo, a mensagem não passa
        logger.error("Guardrail de escopo indisponível: %s", resumo_para_log(exc))
        return {
            "bloqueado": True,
            "motivo": "erro_api",
            "mensagem": MENSAGEM_VERIFICACAO_INDISPONIVEL,
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

    return {"conteudo": texto_final}
