import json

from langchain_core.tools import tool

from iai.app.core.config import FAQ_PATH
from iai.app.core.infra.retrieval import rank


@tool
def faq_retriever(question: str) -> str:
    """Recupera até quatro entradas relevantes por busca textual no FAQ versionado."""
    try:
        with open(FAQ_PATH, encoding="utf-8") as source:
            entries = [json.loads(line) for line in source if line.strip()]
        matches = rank(question, [entry["p"] + " " + entry["r"] for entry in entries])
        if not matches:
            return "Nenhuma informação documental relevante encontrada. Não invente uma resposta."
        return "\n\n".join(
            f"Fonte: faq_inventra.jsonl:{i + 1}\nP: {entries[i]['p']}\nR: {entries[i]['r']}"
            for i, _ in matches
        )
    except (OSError, ValueError, KeyError):
        return "Base documental indisponível ou inválida. Não invente a resposta."
