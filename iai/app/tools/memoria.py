from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from iai.app.memory import recuperar_historico


@tool
def buscar_historico(busca: str, config: RunnableConfig) -> str:
    """Busca no histórico de conversas anteriores do usuário resumos relevantes para a busca informada."""
    configuravel = (config or {}).get("configurable", {})
    user_id = configuravel.get("user_id") or configuravel.get("thread_id")

    if not user_id:
        return "Não foi possível identificar o usuário."

    historico = recuperar_historico(user_id, busca=busca, limite=5)

    if not historico:
        return "Nenhuma conversa anterior relevante encontrada."

    linhas = []
    for h in historico:
        data = h.started_at
        if hasattr(data, "strftime"):
            data_fmt = data.strftime("%d/%m/%Y")
        else:
            data_fmt = str(data)[:10]

        linhas.append(f"- {data_fmt}: {h.summary}")
    return "\n\n".join(linhas)

TOOLS_MEMORIA = [buscar_historico]    