from langchain_community.document_loaders import JSONLoader
from langchain_core.tools import tool

from iai.app.config import FAQ_PATH


@tool
def faq_retriever(question: str) -> str:
    """Busca no FAQ oficial do Inventra a resposta para a pergunta fornecida."""
    loader = JSONLoader(
        file_path=FAQ_PATH,
        jq_schema=".",
        content_key="p",
        metadata_func=lambda record, metadata: {
            **metadata, "resposta": record.get("r")
            },
        json_lines=True,
    )
    documentos = loader.load()

    return "\n".join(
        f"P: {documento.page_content}\nR: {documento.metadata['resposta']}"
        for documento in documentos
    )
