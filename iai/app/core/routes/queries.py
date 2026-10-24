from fastapi import APIRouter

from iai.app.core.infra.security import Authenticated
from iai.app.core.inventra.business import gateway
from iai.app.core.inventra.domain import (
    PARAMETERS,
    ROLE_OPERATIONS,
    Operation,
    Query,
    ToolResult,
)
from iai.app.core.tools.memoria import TOOLS_MEMORIA
from iai.app.core.tools.operational import BUY_TOOLS, STOCK_TOOLS, SUPERVISOR_TOOLS

router = APIRouter(prefix="/queries", tags=["consultas"])


def tools(*groups):
    return [
        {"name": t.name, "description": t.description}
        for group in groups
        for t in group
    ]


@router.get("/capabilities")
def capabilities(ctx: Authenticated):
    return {
        "agents": {
            "estoquista": tools(STOCK_TOOLS, TOOLS_MEMORIA),
            "comprador": tools(BUY_TOOLS, TOOLS_MEMORIA),
            "supervisor": tools(SUPERVISOR_TOOLS, TOOLS_MEMORIA),
        },
        "queries": Query.model_json_schema(),
        "operations": [
            {
                "operation": operation.value,
                "executes": False,  # HITL: troque quando a execução existir
                "allowed_roles": [
                    role for role, ops in ROLE_OPERATIONS.items() if operation in ops
                ],
                "parameters_schema": PARAMETERS[operation].model_json_schema(),
                "spring_limitation": "Depende de endpoint de edição de contagem."
                if operation == Operation.EDIT_COUNT
                else "Depende de remoção lógica confirmada."
                if operation == Operation.REMOVE_ITEM
                else None,
            }
            for operation in Operation
        ],
        "physical_delete": False,
    }


@router.post("", response_model=ToolResult)
def query(payload: Query, ctx: Authenticated):
    """Leitura estruturada, sem depender de LLM. Nunca aceita usuário/cozinha arbitrários."""
    return gateway().query(payload, ctx)
