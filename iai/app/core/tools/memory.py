from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

from iai.app.core.assistant.memory import recuperar_historico
from iai.app.core.assistant.tool_gate import protect_tool
from iai.app.core.infra.security import context


@tool
def buscar_historico(busca: str, config: RunnableConfig) -> str:
    """Busca no histórico de conversas anteriores do usuário resumos relevantes para a busca informada."""
    identity = context().identity
    historico = recuperar_historico(
        identity.user_id, busca=busca, limite=5, kitchen_id=identity.kitchen_id
    )

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


TOOLS_MEMORIA = [protect_tool(buscar_historico)]
