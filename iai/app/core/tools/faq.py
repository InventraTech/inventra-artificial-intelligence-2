import json

from langchain_core.tools import tool

from iai.app.core.assistant.tool_gate import protect_tool
from iai.app.core.config import FAQ_PATH
from iai.app.core.infra.retrieval import rank

SEM_RESULTADO = (
    "Nenhuma informação documental relevante encontrada. Não invente uma resposta."
)
BASE_INDISPONIVEL = "Base documental indisponível ou inválida. Não invente a resposta."


def buscar_faq(pergunta: str, limite: int = 4) -> list[dict]:
    """Busca textual no FAQ versionado. Levanta OSError/ValueError/KeyError se a base estiver inválida."""
    with open(FAQ_PATH, encoding="utf-8") as source:
        entries = [json.loads(line) for line in source if line.strip()]
    matches = rank(pergunta, [e["p"] + " " + e["r"] for e in entries], limit=limite)
    return [
        {
            "fonte": f"faq_inventra.jsonl:{i + 1}",
            "pergunta": entries[i]["p"],
            "resposta": entries[i]["r"],
        }
        for i, _ in matches
    ]


def formatar_trechos(trechos: list[dict]) -> str:
    return "\n\n".join(
        f"Fonte: {t['fonte']}\nP: {t['pergunta']}\nR: {t['resposta']}" for t in trechos
    )


@tool
def faq_retriever(question: str) -> str:
    """Recupera até quatro entradas relevantes por busca textual no FAQ versionado."""
    try:
        trechos = buscar_faq(question)
    except (OSError, ValueError, KeyError):
        return BASE_INDISPONIVEL
    return formatar_trechos(trechos) if trechos else SEM_RESULTADO


protect_tool(faq_retriever)
