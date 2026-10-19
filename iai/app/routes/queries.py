from fastapi import APIRouter

from iai.app.business import gateway
from iai.app.domain import PARAMETERS, ROLE_OPERATIONS, Operation, Query, ToolResult
from iai.app.security import Authenticated
from iai.app.tools.memoria import TOOLS_MEMORIA
from iai.app.tools.operational import BUY_TOOLS, STOCK_TOOLS, SUPERVISOR_TOOLS

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
                "requires_approval": True,
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
        "approval_days": 7,
        "physical_delete": False,
    }


@router.post("", response_model=ToolResult)
def query(payload: Query, ctx: Authenticated):
    """Leitura estruturada, sem depender de LLM. Nunca aceita usuário/cozinha arbitrários."""
    return gateway().query(payload, ctx)
