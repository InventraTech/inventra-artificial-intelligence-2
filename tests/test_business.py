import httpx
import pytest
from pydantic import SecretStr

from iai.app.core.inventra.business import SpringGateway
from iai.app.core.inventra.domain import Action, Query
from iai.app.core.infra.security import Identity, RequestContext, authenticate


def real_context(user="original"):
    return RequestContext(
        Identity(user_id=user, kitchen_id=7, role="SUPERVISOR", name="Pessoa"),
        SecretStr("secret-token-never-in-prompt"),
    )


def test_unexpected_success_status_does_not_confirm_creation(monkeypatch):
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: httpx.Response(204))
    action = Action(
        operation="criar_produto", parameters={"name": "Arroz", "unitId": 1}
    )
    outcome = SpringGateway().execute(
        action, {"user_id": "original", "kitchen_id": 7}, real_context(), "key"
    )
    assert not outcome.ok and outcome.code == "RESULTADO_INCERTO"
    assert (
        SpringGateway().query(Query(resource="produtos"), real_context()).code
        == "RESPOSTA_INVALIDA"
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (401, "NAO_AUTENTICADO"),
        (403, "SEM_PERMISSAO"),
        (404, "NAO_ENCONTRADO"),
        (409, "CONFLITO"),
        (429, "LIMITE_EXCEDIDO"),
        (503, "SERVICO_INDISPONIVEL"),
    ],
)
def test_spring_http_errors_are_distinct(monkeypatch, status, code):
    monkeypatch.setattr(httpx, "request", lambda *a, **kw: httpx.Response(status))
    assert (
        SpringGateway().query(Query(resource="produtos"), real_context()).code == code
    )


def test_spring_uses_documented_filters_and_does_not_leak_tokens(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "request", request)
    outcome = SpringGateway().query(Query(resource="validade", days=9), real_context())
    assert outcome.ok and not outcome.mocked
    assert calls[0][1].endswith("/api/stock-batches/expiring-soon")
    assert calls[0][2]["params"] == {"kitchenId": 7, "days": 9}
    assert "secret-token" not in outcome.model_dump_json()


def test_spring_rejects_cross_kitchen_and_invalid_response(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *a, **kw: httpx.Response(
            200,
            json=[
                {
                    "id": 1,
                    "product": {"id": 1},
                    "currentQuantity": 10,
                    "status": "ACTIVE",
                    "kitchen": {"id": 99},
                }
            ],
        ),
    )
    assert (
        SpringGateway().query(Query(resource="lotes"), real_context()).code
        == "SEM_PERMISSAO"
    )
    monkeypatch.setattr(
        httpx, "request", lambda *a, **kw: httpx.Response(200, json="not records")
    )
    assert (
        SpringGateway().query(Query(resource="produtos"), real_context()).code
        == "RESPOSTA_INVALIDA"
    )


def test_writes_use_requester_identity_and_uncertain_timeout_is_not_retried(
    monkeypatch,
):
    calls = []

    def request(*args, **kwargs):
        calls.append(args)
        raise httpx.ReadTimeout("Response lost")

    monkeypatch.setattr(httpx, "request", request)
    action = Action(
        operation="registrar_saida", parameters={"batchId": 3, "quantity": 2}
    )
    out = SpringGateway().execute(
        action, {"user_id": "original", "kitchen_id": 7}, real_context(), "durable-key"
    )
    assert out.code == "RESULTADO_INCERTO"
    assert len(calls) == 1
    out = SpringGateway().execute(
        action,
        {"user_id": "someone-else", "kitchen_id": 7},
        real_context(),
        "other-key",
    )
    assert out.code == "INTEGRACAO_PENDENTE" and len(calls) == 1


def test_real_identity_validated_by_spring_not_client_claims(monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials

    from iai.app.core.infra import security

    monkeypatch.setattr(security, "AUTH_MODE", "spring")
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: httpx.Response(
            200,
            json={
                "id": "trusted-id",
                "name": "Pessoa",
                "active": True,
                "kitchen": {"id": 7},
                "profile": {"accessType": "SUPERVISOR"},
            },
        ),
    )
    ctx = authenticate(
        HTTPAuthorizationCredentials(scheme="Bearer", credentials="opaque-jwt")
    )
    assert ctx.identity.user_id == "trusted-id"
    assert not ctx.identity.mocked


def test_real_edit_count_is_prepared_but_no_network_call(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *a, **kw: pytest.fail("Endpoint não pode ser inventado"),
    )
    action = Action(
        operation="alterar_contagem",
        parameters={
            "inventoryId": 1,
            "countId": 2,
            "batchId": 3,
            "physicalQuantity": 4,
        },
    )
    assert (
        SpringGateway()
        .execute(
            action, {"user_id": "original", "kitchen_id": 7}, real_context(), "key"
        )
        .code
        == "INTEGRACAO_PENDENTE"
    )
