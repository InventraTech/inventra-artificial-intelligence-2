import inspect
import os
import sys
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Annotated

BASE_DIR = Path(__file__).resolve().parents[3]
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from iai.app.core.infra.security import RequestContext, authenticate, current_context
from iai.app.core.tools.faq import faq_retriever
from iai.app.core.tools.memoria import TOOLS_MEMORIA
from iai.app.core.tools.operational import BUY_TOOLS, STOCK_TOOLS, SUPERVISOR_TOOLS

TOOLS_POR_CARGO = {
    "ESTOQUISTA": STOCK_TOOLS,
    "COMPRADOR": BUY_TOOLS,
    "SUPERVISOR": SUPERVISOR_TOOLS,
}

LEITURA = ToolAnnotations(read_only_hint=True)
ESCRITA = ToolAnnotations(read_only_hint=False, destructive_hint=False)


def registrar_tool(servidor: MCPServer, ferramenta, ctx: RequestContext) -> None:
    """Publica uma tool LangChain com o mesmo schema de argumentos e a mesma descrição."""
    campos = ferramenta.get_input_schema().model_fields
    parametros = [
        inspect.Parameter(
            nome,
            inspect.Parameter.KEYWORD_ONLY,
            annotation=Annotated[campo.annotation, campo],
        )
        for nome, campo in campos.items()
    ]

    def executar(**argumentos) -> str:
        # Cada chamada é uma rodada nova: a chave de idempotência das propostas usa o turn_id.
        marcador = current_context.set(replace(ctx, turn_id=str(uuid.uuid4())))
        try:
            return ferramenta.invoke(argumentos)
        finally:
            current_context.reset(marcador)

    executar.__signature__ = inspect.Signature(parametros, return_annotation=str)  # type: ignore[attr-defined]
    executar.__annotations__ = {
        nome: parametro.annotation for nome, parametro in zip(campos, parametros)
    } | {"return": str}
    somente_leitura = not ferramenta.name.startswith("propor_")
    servidor.add_tool(
        executar,
        name=ferramenta.name,
        description=ferramenta.description,
        annotations=LEITURA if somente_leitura else ESCRITA,
    )


def criar_servidor(ctx: RequestContext) -> MCPServer:
    identidade = ctx.identity
    servidor = MCPServer(
        name="assessor-inventra",
        version="1.0.0",
        instructions=(
            f"Tools da IAI do Inventra para {identidade.name} ({identidade.role}). "
            "As tools propor_* só criam propostas pendentes; somente o próprio solicitante "
            "confirma em /approvals. Consulte os dados antes de propor e nunca invente IDs."
        ),
    )
    for ferramenta in [*TOOLS_POR_CARGO[identidade.role], *TOOLS_MEMORIA, faq_retriever]:
        registrar_tool(servidor, ferramenta, ctx)
    return servidor


def contexto_do_ambiente() -> RequestContext:
    token = os.getenv("IAI_MCP_TOKEN", "").strip()
    if not token:
        raise SystemExit(
            "[assessor-inventra] Defina IAI_MCP_TOKEN (JWT do Spring) no bloco env do mcp.json."
        )
    try:
        return authenticate(HTTPAuthorizationCredentials(scheme="Bearer", credentials=token))
    except HTTPException as erro:
        raise SystemExit(f"[assessor-inventra] Token recusado: {erro.detail}") from None


if __name__ == "__main__":
    from pymongo.errors import PyMongoError

    from iai.app.core.infra.storage import initialize_storage

    contexto = contexto_do_ambiente()
    try:
        initialize_storage()
    except PyMongoError:
        print("[assessor-inventra] Mongo indisponível ao iniciar.", file=sys.stderr)
    criar_servidor(contexto).run(transport="stdio")
