"""Tools leem dados ou CRIAM PROPOSTAS; não expõem execução/decisão ao modelo."""

import json
from hashlib import sha256

from fastapi import HTTPException
from langchain_core.tools import StructuredTool, tool
from pydantic import Field, ValidationError, create_model
from pymongo.errors import PyMongoError

from iai.app.core.assistant.tool_gate import protect_tool
from iai.app.core.config import STORAGE_MODE
from iai.app.core.infra.security import context
from iai.app.core.inventra.approvals import (
    create_proposal,
    get_proposal,
    list_proposals,
    public,
)
from iai.app.core.inventra.business import gateway
from iai.app.core.inventra.domain import (
    BUY_OPERATIONS,
    PARAMETERS,
    STOCK_OPERATIONS,
    Action,
    Operation,
    ProposalInput,
    Query,
    ToolResult,
)


@tool(args_schema=Query)
def consultar_inventra(**parameters) -> str:
    """Consulta produtos, estoque, lotes, validade, fornecedores, requisições, inventários, alertas e catálogos autorizados. Não altera dados. Para o saldo geral do estoque da cozinha use resource=estoque sem product_id; com product_id retorna os parâmetros e o saldo daquele produto. O resource=produtos é o catálogo global e exige name."""
    try:
        query = Query.model_validate(parameters)
        if query.resource == "produtos" and not (query.name or query.product_id):
            # O catálogo é global (todas as cozinhas): nunca listar sem filtro.
            return ToolResult(
                ok=False,
                code="USE_ESTOQUE",
                message=(
                    "O catálogo de produtos não é da cozinha. Para saber o que a cozinha tem, "
                    "use resource=estoque (sem product_id). Use produtos apenas com name para "
                    "localizar um produto específico."
                ),
                source="IAI",
                mocked=False,
            ).model_dump_json()
        outcome = gateway().query(query, context())
        return outcome.model_dump_json()
    except PyMongoError:
        return ToolResult(
            ok=False,
            code="PERSISTENCIA_INDISPONIVEL",
            message="Persistência indisponível.",
            source="IAI",
            mocked=False,
        ).model_dump_json()
    except HTTPException as exc:
        return ToolResult(
            ok=False,
            code="SEM_PERMISSAO",
            message=str(exc.detail),
            source="IAI",
            mocked=False,
        ).model_dump_json()


@tool
def consultar_solicitacoes_iai(solicitacao_id: str = "") -> str:
    """Consulta estado e resultado de propostas no escopo autenticado; não aprova nem executa."""
    try:
        data = (
            public(get_proposal(solicitacao_id, context()))
            if solicitacao_id
            else list_proposals(context())
        )
        return ToolResult(
            ok=True,
            code="OK",
            message="Estado persistido; verifique source/mocked do resultado de negócio.",
            data=data,
            source="Mongo IAI",
            mocked=STORAGE_MODE == "mock",
        ).model_dump_json()
    except HTTPException:
        return ToolResult(
            ok=False,
            code="NAO_ENCONTRADO",
            message="Solicitação não encontrada no seu escopo.",
            source="Mongo IAI",
            mocked=STORAGE_MODE == "mock",
        ).model_dump_json()
    except PyMongoError:
        return ToolResult(
            ok=False,
            code="PERSISTENCIA_INDISPONIVEL",
            message="Estado indisponível; não inferir execução.",
            source="Mongo IAI",
            mocked=STORAGE_MODE == "mock",
        ).model_dump_json()


def proposal_tool(operation):
    schema = create_model(
        f"Proposta_{operation.value}",
        __base__=PARAMETERS[operation],
        justification=(str, Field(min_length=1, max_length=1000)),
    )

    def prepare(**parameters):
        try:
            ctx = context()
            justification = parameters.pop("justification")
            action = Action(operation=operation, parameters=parameters)
            fingerprint = sha256(
                json.dumps(action.model_dump(mode="json"), sort_keys=True).encode()
            ).hexdigest()
            key = sha256(f"{ctx.turn_id}:{fingerprint}".encode()).hexdigest()
            proposal = create_proposal(
                ProposalInput(
                    action=action, justification=justification, idempotency_key=key
                ),
                ctx,
            )
            return json.dumps(
                {
                    "ok": True,
                    "code": "AGUARDANDO_APROVACAO",
                    "executed": False,
                    "message": "Proposta registrada. Nenhuma alteração de negócio foi executada.",
                    "proposal": proposal,
                },
                default=str,
                ensure_ascii=False,
            )
        except (HTTPException, ValidationError) as exc:
            message = (
                str(exc.detail)
                if isinstance(exc, HTTPException)
                else "Parâmetros inválidos; peça os dados necessários."
            )
            return json.dumps(
                {
                    "ok": False,
                    "code": "PROPOSTA_NAO_CRIADA",
                    "executed": False,
                    "message": message,
                },
                ensure_ascii=False,
            )
        except PyMongoError:
            return '{"ok":false,"code":"PERSISTENCIA_INDISPONIVEL","executed":false,"message":"Não foi possível confirmar o registro da proposta. Consulte suas solicitações antes de repetir."}'

    return StructuredTool.from_function(
        prepare,
        name=f"propor_{operation.value}",
        description=f"Prepara {operation.value} para aprovação. NÃO EXECUTA. Confirme IDs/dados; nunca invente.",
        args_schema=schema,
    )


PROPOSAL_TOOLS = {operation: proposal_tool(operation) for operation in Operation}
for operational_tool in [
    consultar_inventra,
    consultar_solicitacoes_iai,
    *PROPOSAL_TOOLS.values(),
]:
    protect_tool(operational_tool)
    operational_tool.handle_validation_error = "Parâmetros incompletos ou inválidos. Consulte os IDs e peça os campos necessários; nenhuma operação foi executada."
STOCK_TOOLS = [consultar_inventra, consultar_solicitacoes_iai] + [
    PROPOSAL_TOOLS[o] for o in Operation if o in STOCK_OPERATIONS
]
BUY_TOOLS = [consultar_inventra, consultar_solicitacoes_iai] + [
    PROPOSAL_TOOLS[o] for o in Operation if o in BUY_OPERATIONS
]
SUPERVISOR_TOOLS = [consultar_inventra, consultar_solicitacoes_iai] + list(
    PROPOSAL_TOOLS.values()
)
