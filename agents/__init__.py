"""Compatibility entry point: use the same guarded production graph."""
from iai.app.graph import executar_fluxo_assessor
from iai.app.security import current_context


def responder(usuario_input: str) -> str:
    context = current_context()
    return executar_fluxo_assessor(usuario_input, context.session_id, context.user_id)
