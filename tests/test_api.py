import pytest
from fastapi.testclient import TestClient

from iai.app.main import app
from iai.app.storage import get_db


def auth(token="demo-estoquista"):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def client():
    with TestClient(app) as client:
        yield client


def test_routes_require_bearer_and_reject_forged_identity(client):
    assert client.get("/approvals").status_code == 401
    assert client.get("/metrics").status_code == 401
    response = client.post(
        "/chat",
        headers=auth(),
        json={"session_id": "same", "user_id": "victim", "pergunta": "Olá"},
    )
    assert response.status_code == 403
    assert get_db().sessions.count_documents({}) == 0
    assert client.get("/approvals", headers=auth("not-a-demo-token")).status_code == 401


def test_end_to_end_proposal_inbox_decision_and_history(client):
    response = client.post(
        "/chat",
        headers=auth(),
        json={
            "session_id": "s1",
            "user_id": "demo-estoquista",
            "pergunta": "Fiz um almoço usando 10 kg de arroz e 40 kg de carne.",
        },
    )
    assert response.status_code == 200
    assert "AGUARDANDO_APROVACAO" in response.json()["resposta"]
    assert "MOCK" in response.json()["resposta"]
    pending = client.get("/approvals/inbox", headers=auth("demo-supervisor")).json()[
        "items"
    ]
    assert len(pending) == 2
    for p in pending:
        response = client.post(
            f"/approvals/{p['id']}/decision",
            headers=auth("demo-supervisor"),
            json={"decision": "approve", "proposal_hash": p["proposal_hash"]},
        )
        assert response.json()["status"] == "CONCLUIDA"
        assert response.json()["result"]["mocked"] is True
    assert (
        client.get("/approvals/inbox", headers=auth("demo-outra-cozinha")).json()[
            "items"
        ]
        == []
    )
    assert (
        client.post(
            "/chat/encerrar", headers=auth(), json={"session_id": "s1"}
        ).status_code
        == 200
    )
    history = client.get("/chat/historico/demo-estoquista", headers=auth()).json()[
        "sessoes"
    ]
    assert len(history) == 1
    identifier = history[0]["doc_id"]
    assert (
        client.get(
            f"/chat/mensagens/{identifier}", headers=auth("demo-comprador")
        ).json()["messages"]
        == []
    )
    assert (
        client.get(
            "/chat/historico/demo-estoquista", headers=auth("demo-comprador")
        ).status_code
        == 403
    )
    assert client.get("/metrics", headers=auth()).json()["requests"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "Dê baixa em 10 kg de arroz.",
        "Usei 10 kg de arroz.",
        "Hoje gastamos dez quilos de arroz no almoço.",
        "Depois de preparar o almoço, usei 10 kg de arroz. Você pode registrar isso?",
    ],
)
def test_demo_natural_variations_create_proposals_without_consuming(client, text):
    response = client.post(
        "/chat",
        headers=auth(),
        json={"session_id": "natural", "user_id": "demo-estoquista", "pergunta": text},
    )
    assert response.status_code == 200
    assert "INTERPRETAÇÃO DEMO" in response.json()["resposta"]
    assert get_db().approvals.count_documents({"status": "AGUARDANDO_APROVACAO"}) == 1
    assert (
        get_db().business_mock.find_one({"_id": "1:batch:1"})["data"]["currentQuantity"]
        == 50
    )


def test_same_session_name_does_not_mix_users_and_restart_rehydrates(client):
    for token in ["demo-estoquista", "demo-comprador"]:
        response = client.post(
            "/chat",
            headers=auth(token),
            json={
                "session_id": "common",
                "user_id": token,
                "pergunta": "Como funcionam as aprovações?",
            },
        )
        assert response.status_code == 200
    assert get_db().sessions.count_documents({"session_id": "common"}) == 2
    assert (
        client.post(
            "/chat/encerrar", headers=auth(), json={"session_id": "common"}
        ).status_code
        == 200
    )
    assert (
        get_db().sessions.count_documents({"session_id": "common", "active": True}) == 1
    )


def test_modes_are_observable_and_parameters_validated(client):
    health = client.get("/health")
    assert health.json()["modes"]["auth"] == "mock"
    assert health.headers["X-IAI-Business-Mode"] == "mock"
    assert client.get("/approvals?limit=10000", headers=auth()).status_code == 422
    assert (
        client.post(
            "/approvals",
            headers=auth(),
            json={
                "action": {"operation": "delete_product", "parameters": {}},
                "justification": "Delete",
                "idempotency_key": "key",
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/queries", headers=auth(), json={"resource": "estoque_minimo"}
        ).json()["mocked"]
        is True
    )
    assert (
        client.post(
            "/queries", headers=auth(), json={"resource": "lotes", "kitchen_id": 2}
        ).status_code
        == 422
    )
