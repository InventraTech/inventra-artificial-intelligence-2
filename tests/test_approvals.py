import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi import HTTPException
from pydantic import SecretStr, ValidationError

from iai.app.core.infra.security import DEMO_IDENTITIES, RequestContext
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra import approvals
from iai.app.core.inventra.business import MockGateway
from iai.app.core.inventra.domain import (
    Action,
    DecisionInput,
    Operation,
    ProposalInput,
    Query,
)


def ctx(token="demo-estoquista"):
    return RequestContext(DEMO_IDENTITIES[token], SecretStr(token))


def proposal(operation="registrar_saida", parameters=None, actor=None, key="test"):
    return approvals.create_proposal(
        ProposalInput(
            action=Action(
                operation=operation,
                parameters=parameters or {"batchId": 1, "quantity": "10"},
            ),
            justification="Consumo informado e revisado",
            idempotency_key=key,
        ),
        actor or ctx(),
    )


def approve(p, actor=None, decision="approve", proposal_hash=None):
    return approvals.decide(
        p["id"],
        DecisionInput(
            decision=decision,
            proposal_hash=proposal_hash or p["proposal_hash"],
            reason="Decisão de teste",
        ),
        actor or ctx(p["requester_id"]),
    )


def balance():
    return (
        MockGateway()
        .query(Query(resource="lotes", product_id=1), ctx())
        .data[0]["currentQuantity"]
    )


def test_only_approval_executes_and_preserves_original_requester():
    p = proposal()
    assert p["status"] == approvals.PENDING
    assert balance() == 50
    done = approve(p)
    assert done["status"] == "CONCLUIDA"
    assert done["result"]["mocked"] is True
    assert done["requester_id"] == "demo-estoquista"
    assert done["decided_by"] == "demo-estoquista"
    assert balance() == 40
    assert approve(p)["status"] == "CONCLUIDA"
    assert balance() == 40


def test_concurrent_decisions_execute_once():
    p = proposal()

    def run(_):
        try:
            return approve(p)
        except HTTPException as e:
            assert e.status_code == 409

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(16)))
    assert balance() == 40
    assert get_db().execution_mock.count_documents({}) == 1


@pytest.mark.parametrize(
    "token", ["demo-supervisor", "demo-comprador", "demo-outra-cozinha"]
)
def test_decision_denied_for_wrong_role_or_kitchen(token):
    p = proposal()
    with pytest.raises(HTTPException) as exc:
        approve(p, ctx(token))
    assert exc.value.status_code in {403, 404}
    assert balance() == 50


def test_supervisor_self_approval_is_allowed():
    actor = ctx("demo-supervisor")
    assert approve(proposal(actor=actor), actor)["status"] == "CONCLUIDA"


def test_rejection_and_expiration_do_not_write():
    p = proposal()
    assert approve(p, decision="reject")["status"] == "RECUSADA"
    other = proposal(key="other")
    get_db().approvals.update_one(
        {"_id": other["id"]},
        {"$set": {"expires_at": approvals.now() - timedelta(seconds=1)}},
    )
    approvals.expire_pending()
    assert get_db().approvals.find_one({"_id": other["id"]})["status"] == "EXPIRADA"
    with pytest.raises(HTTPException):
        approve(other)
    assert balance() == 50


def test_expiration_is_exactly_seven_days():
    p = proposal()
    assert p["expires_at"] - p["created_at"] == timedelta(days=7)


def test_cannot_execute_pending_or_reuse_key_with_new_payload():
    p = proposal()
    with pytest.raises(HTTPException):
        approvals.execute_approved(p["id"], ctx())
    assert proposal()["id"] == p["id"]
    with pytest.raises(HTTPException):
        proposal(parameters={"batchId": 1, "quantity": 9})


def test_hash_and_stored_data_tampering_are_rejected():
    p = proposal()
    with pytest.raises(HTTPException):
        approve(p, proposal_hash="0" * 64)
    get_db().approvals.update_one(
        {"_id": p["id"]}, {"$set": {"action.parameters.quantity": "999"}}
    )
    with pytest.raises(HTTPException):
        approve(p)
    assert balance() == 50


def test_changed_resource_requires_new_proposal():
    p = proposal()
    get_db().business_mock.update_one(
        {"_id": "1:batch:1"}, {"$set": {"data.currentQuantity": 45}}
    )
    assert approve(p)["status"] == "FALHOU"
    assert balance() == 45


def test_uncertain_execution_is_never_repeated(monkeypatch):
    p = proposal()
    original = MockGateway()
    calls = []

    class Broken:
        query = original.query

        def execute(self, *args):
            calls.append(1)
            raise TimeoutError("Não sabemos se ocorreu escrita")

    monkeypatch.setattr(approvals, "gateway", lambda: Broken())
    assert approve(p)["status"] == "RESULTADO_INCERTO"
    approve(p)
    approvals.execute_approved(p["id"], ctx())
    assert len(calls) == 1


def test_changed_business_mode_retains_approval_without_execution(monkeypatch):
    p = proposal()
    monkeypatch.setattr(approvals, "BUSINESS_MODE", "spring")
    done = approve(p)
    assert done["status"] == "APROVADA_PENDENTE_INTEGRACAO"
    assert balance() == 50
    assert get_db().execution_mock.count_documents({}) == 0


@pytest.mark.parametrize("bad", [0, -1, "NaN", "Infinity", "1.0001"])
def test_invalid_quantities_cannot_be_proposed(bad):
    with pytest.raises(ValidationError):
        proposal(parameters={"batchId": 1, "quantity": bad})


def test_cross_kitchen_id_is_validated_by_snapshot():
    with pytest.raises(HTTPException):
        proposal(parameters={"batchId": 9876, "quantity": 1})


def test_real_edit_count_has_no_invented_endpoint(monkeypatch):
    count = approve(
        proposal(
            "registrar_contagem",
            {"inventoryId": 1, "batchId": 1, "physicalQuantity": 40},
        )
    )
    p = proposal(
        "alterar_contagem",
        {
            "inventoryId": 1,
            "batchId": 1,
            "countId": count["result"]["data"]["id"],
            "physicalQuantity": 42,
        },
        key="edit",
    )
    monkeypatch.setattr(approvals, "BUSINESS_MODE", "spring")
    assert approve(p)["status"] == "APROVADA_PENDENTE_INTEGRACAO"


@pytest.mark.parametrize(
    ("token", "operation", "parameters", "permitido"),
    [
        ("demo-estoquista", Operation.CONSUME, {"batchId": 1, "quantity": "2"}, True),
        (
            "demo-estoquista",
            Operation.CREATE_REQUISITION,
            {"type": "PURCHASE", "origin": "app"},
            False,
        ),
        (
            "demo-estoquista",
            Operation.CREATE_PRODUCT,
            {"name": "Sal", "unitId": 1},
            False,
        ),
        (
            "demo-comprador",
            Operation.CREATE_REQUISITION,
            {"type": "PURCHASE", "origin": "app"},
            True,
        ),
        ("demo-comprador", Operation.CONSUME, {"batchId": 1, "quantity": "2"}, False),
        (
            "demo-comprador",
            Operation.CREATE_PRODUCT,
            {"name": "Sal", "unitId": 1},
            False,
        ),
        (
            "demo-supervisor",
            Operation.CREATE_PRODUCT,
            {"name": "Sal", "unitId": 1},
            True,
        ),
        ("demo-supervisor", Operation.CONSUME, {"batchId": 1, "quantity": "2"}, True),
    ],
)
def test_cargo_so_propoe_operacoes_da_sua_area(token, operation, parameters, permitido):
    payload = ProposalInput(
        action=Action(operation=operation, parameters=parameters),
        justification="teste de cargo",
        idempotency_key=str(uuid.uuid4()),
    )
    if permitido:
        assert (
            approvals.create_proposal(payload, ctx(token))["status"]
            == "AGUARDANDO_APROVACAO"
        )
    else:
        with pytest.raises(HTTPException) as erro:
            approvals.create_proposal(payload, ctx(token))
        assert erro.value.status_code == 403


def test_current_permissions_are_revalidated_at_confirmation_and_execute():
    p = proposal()
    changed = replace(
        ctx(), identity=ctx().identity.model_copy(update={"role": "COMPRADOR"})
    )
    with pytest.raises(HTTPException) as error:
        approve(p, changed)
    assert error.value.status_code == 403
    assert balance() == 50
    approve(p)
    with pytest.raises(HTTPException) as error:
        approvals.execute_approved(p["id"], changed)
    assert error.value.status_code == 403


def test_fresh_requester_context_executes_without_delegation_or_stored_jwt(monkeypatch):
    p = proposal()
    token = "fresh-opaque-credential"
    fresh = replace(ctx(), token=SecretStr(token))
    original = MockGateway()
    observed = []

    class Gateway:
        query = original.query

        def execute(self, action, requester, current, key):
            observed.append(current.token.get_secret_value())
            return original.execute(action, requester, current, key)

    monkeypatch.setattr(approvals, "gateway", lambda: Gateway())
    done = approve(p, fresh)
    assert done["status"] == "CONCLUIDA" and observed == [token]
    assert token not in str(get_db().approvals.find_one({"_id": p["id"]}))
    with pytest.raises(HTTPException) as error:
        approvals.execute_approved(p["id"], ctx("demo-supervisor"))
    assert error.value.status_code == 403


def test_legacy_third_party_decision_cannot_execute_under_new_policy():
    p = proposal()
    get_db().approvals.update_one(
        {"_id": p["id"]},
        {
            "$set": {
                "status": "APROVADA_PENDENTE_INTEGRACAO",
                "decision": "approve",
                "decided_by": "demo-supervisor",
            }
        },
    )
    with pytest.raises(HTTPException) as error:
        approvals.execute_approved(p["id"], ctx())
    assert error.value.status_code == 409
    assert balance() == 50


def test_same_role_other_requester_is_denied():
    p = proposal()
    other = replace(
        ctx(),
        identity=ctx().identity.model_copy(update={"user_id": "other-stock-user"}),
    )
    with pytest.raises(HTTPException) as error:
        approve(p, other)
    assert error.value.status_code == 404
    assert balance() == 50
