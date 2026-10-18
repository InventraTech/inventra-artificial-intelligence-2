from fastapi import APIRouter

from iai.app.business import gateway
from iai.app.domain import PARAMETERS, ROLE_OPERATIONS, Operation, Query, ToolResult
from iai.app.security import Authenticated

router = APIRouter(prefix="/queries", tags=["consultas"])


@router.get("/capabilities")
def capabilities(ctx: Authenticated):
    return {
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
