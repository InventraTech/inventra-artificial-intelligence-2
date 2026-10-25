import json
from dataclasses import replace
from datetime import UTC

import pytest

from iai.app.core.assistant.guardrail import anonimizar_entrada
from iai.app.core.assistant.memory import (
    iniciar_sessao,
    recuperar_historico,
    recuperar_mensagem,
    salvar_mensagem,
)
from iai.app.core.infra.security import current_context
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.domain import Operation
from iai.app.core.tools.operational import PROPOSAL_TOOLS, consultar_inventra
from tests_helpers import context_for


def test_propor_tools_validate_but_never_execute_or_persist():
    token = current_context.set(replace(context_for(), turn_id="stable-turn"))
    try:
        tool = PROPOSAL_TOOLS[Operation.CONSUME]
        args = {"batchId": 1, "quantity": 10, "justification": "Consumo confirmado"}
        first = json.loads(tool.invoke(args))
        second = json.loads(tool.invoke(dict(args, quantity="10.000")))
        assert first["proposal"]["id"] == second["proposal"]["id"]
        assert first["executed"] is False
        assert first["code"] == "NAO_EXECUTADA_SEM_CONFIRMACAO_HUMANA"
        assert get_db().execution_mock.count_documents({}) == 0
        assert "approvals" not in get_db().list_collection_names()
        assert "Parâmetros incompletos" in tool.invoke({"quantity": 10})
        assert json.loads(
            consultar_inventra.invoke({"resource": "produtos", "name": "Arroz"})
        )["mocked"]
    finally:
        current_context.reset(token)


def test_tokens_never_enter_pii_map_or_model_text():
    secrets = [
        "Bearer abc123-super-secret",
        "token=private-secret",
        "senha=not-for-models",
        "gsk_mysecret123",
    ]
    for secret in secrets:
        text, pii_map = anonimizar_entrada(f"Guarde {secret} para consultar estoque")
        assert secret not in text
        assert secret not in json.dumps(pii_map)
        assert "SEGREDO_REMOVIDO" in text


def test_credentials_are_rejected_in_action_requests():
    token = current_context.set(replace(context_for(), turn_id="segredo"))
    try:
        out = json.loads(
            PROPOSAL_TOOLS[Operation.CONSUME].invoke(
                {"batchId": 1, "quantity": 2, "justification": "token=secret"}
            )
        )
        assert out["ok"] is False and out["executed"] is False
        assert "tokens ou senhas" in out["message"]
    finally:
        current_context.reset(token)


def test_memory_search_uses_query_and_isolates_user_and_kitchen():
    from datetime import datetime

    for identifier, user, kitchen, summary in [
        ("a", "u", 1, "Consulta de arroz"),
        ("b", "u", 1, "Aprovação de fornecedor"),
        ("c", "v", 1, "Arroz de outra pessoa"),
        ("d", "u", 2, "Arroz de outra cozinha"),
    ]:
        get_db().sessions.insert_one(
            {
                "_id": identifier,
                "user_id": user,
                "kitchen_id": kitchen,
                "summary": summary,
                "started_at": datetime.now(UTC),
            }
        )
    matches = recuperar_historico("u", busca="arroz", kitchen_id=1)
    assert [m.doc_id for m in matches] == ["a"]
    assert recuperar_historico("u", busca="zqqwy", kitchen_id=1) == []
    from iai.app.core.tools.memoria import buscar_historico

    actor = context_for()
    marker = current_context.set(
        replace(actor, identity=actor.identity.model_copy(update={"user_id": "u"}))
    )
    try:
        history_text = buscar_historico.invoke({"busca": "arroz"})
        assert "Consulta de arroz" in history_text and "outra" not in history_text
    finally:
        current_context.reset(marker)


def test_message_lookup_requires_identity_and_same_session_does_not_share_data():
    from fastapi import HTTPException

    iniciar_sessao("same", "u", 1)
    iniciar_sessao("same", "v", 1)
    salvar_mensagem("same", "human", "Privado de U", "u", 1)
    assert get_db().sessions.count_documents({"session_id": "same"}) == 2
    identifier = get_db().sessions.find_one({"user_id": "u"})["_id"]
    assert recuperar_mensagem(identifier, "v", 1) == []
    with pytest.raises(HTTPException):
        recuperar_mensagem(identifier)


def test_catalogo_global_nao_e_listado_sem_filtro():
    resposta = json.loads(consultar_inventra.invoke({"resource": "produtos"}))
    assert resposta["ok"] is False and resposta["code"] == "USE_ESTOQUE"
    assert "estoque" in resposta["message"]
