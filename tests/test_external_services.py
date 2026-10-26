"""Opt-in: serviços reais. Não são executados sem disponibilidade explícita."""

import json
import os
import subprocess
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest
from pymongo import MongoClient, ReturnDocument


@pytest.mark.skipif(
    os.getenv("RUN_MONGO_TESTS") != "1" or not os.getenv("MONGO_TEST_CONNECTION"),
    reason="MongoDB real de testes não disponibilizado.",
)
def test_real_mongo_atomic_claim_and_persistence():
    client = MongoClient(
        os.environ["MONGO_TEST_CONNECTION"], serverSelectionTimeoutMS=3000
    )
    database_name = "iai_test_" + uuid.uuid4().hex
    db = client[database_name]
    try:
        db.proposals.insert_one({"_id": "proposal", "status": "PENDING"})

        def claim(_):
            return db.proposals.find_one_and_update(
                {"_id": "proposal", "status": "PENDING"},
                {"$set": {"status": "EXECUTING"}},
                return_document=ReturnDocument.AFTER,
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(claim, range(16)))
        assert sum(c is not None for c in claims) == 1
        assert db.proposals.find_one({"_id": "proposal"})["status"] == "EXECUTING"
    finally:
        # Base temporária exclusiva deste teste; jamais dbIAI ou banco de negócio.
        assert database_name.startswith("iai_test_")
        client.drop_database(database_name)
        client.close()


@pytest.mark.skipif(
    os.getenv("RUN_SPRING_TESTS") != "1" or not os.getenv("SPRING_TEST_TOKEN"),
    reason="API Spring e token de homologação não disponibilizados.",
)
def test_real_spring_identity_and_catalog_readonly():
    from iai.app.core.config import API_URL

    headers = {"Authorization": "Bearer " + os.environ["SPRING_TEST_TOKEN"]}
    with httpx.Client(
        base_url=API_URL, headers=headers, timeout=10, follow_redirects=False
    ) as client:
        me = client.get("/api/users/me")
        assert me.status_code == 200
        identity = me.json()
        assert identity["active"] and identity["kitchen"]["id"]
        products = client.get("/api/products", params={"page": 0, "size": 5})
        assert products.status_code == 200
        assert isinstance(products.json()["content"], list)


@pytest.mark.skipif(
    os.getenv("RUN_JEV_TESTS") != "1" or not os.getenv("TYPESAFE_API_KEY"),
    reason="TypeSafe/JEV real não disponibilizado; contrato HTTP é simulado na suíte comum.",
)
def test_real_jev_typed_contract_readonly():
    from iai.app.core.assistant.judge import Evaluation, evaluate

    response = evaluate(
        {
            "user_request": "Consulte os lotes de arroz.",
            "tool": {
                "name": "consultar_inventra",
                "schema": {"properties": {"resource": {"type": "string"}}},
                "arguments": {"resource": "lotes"},
            },
            "rules": ["This is a read-only lookup; no stock is changed."],
        }
    )
    assert isinstance(response, Evaluation)


@pytest.mark.skipif(
    os.getenv("RUN_MONGO_TESTS") != "1" or not os.getenv("MONGO_TEST_CONNECTION"),
    reason="Retomada entre processos exige MongoDB real de testes.",
)
def test_pending_request_survives_process_restart_with_real_mongo():
    """Mongo real, negócio mock: prepara num processo e confirma em outro."""
    database_name = "iai_test_" + uuid.uuid4().hex
    client = MongoClient(
        os.environ["MONGO_TEST_CONNECTION"], serverSelectionTimeoutMS=3000
    )
    env = dict(
        os.environ,
        IAI_AUTH_MODE="mock",
        IAI_BUSINESS_MODE="mock",
        IAI_LLM_MODE="demo",
        IAI_STORAGE_MODE="mongo",
        MONGO_CONNECTION=os.environ["MONGO_TEST_CONNECTION"],
        IAI_TEST_DATABASE=database_name,
    )
    common = """
import json, os, sys
from iai.app.core.infra import storage
name = os.environ["IAI_TEST_DATABASE"]
assert name.startswith("iai_test_")
storage.get_db = lambda: storage.get_client()[name]
from fastapi.testclient import TestClient
from iai.app.main import app
headers = {"Authorization": "Bearer demo-estoquista"}
"""
    prepare = """
with TestClient(app) as api:
    response = api.post("/chat", headers=headers, json={"session_id":"restart", "user_id":"demo-estoquista", "pergunta":"Usei 2 kg de arroz"})
    assert response.status_code == 200
    action, = response.json()["pending_actions"]
    assert storage.get_db().execution_mock.count_documents({}) == 0
    print(json.dumps(action))
"""
    confirm = """
action = json.load(sys.stdin)
with TestClient(app) as api:
    response = api.post("/approvals/" + action["id"] + "/decision", headers=headers, json={"decision":"approve", "proposal_hash":action["proposal_hash"]})
    assert response.status_code == 200
    assert response.json()["status"] == "CONCLUIDA"
    assert storage.get_db().execution_mock.count_documents({}) == 1
    print("confirmed")
"""
    try:
        first = subprocess.run(
            [sys.executable, "-c", common + prepare],
            check=False,
            env=env,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert first.returncode == 0, "Processo de preparação não concluiu."
        action = json.loads(first.stdout)
        assert (
            client[database_name].approvals.find_one({"_id": action["id"]})["status"]
            == "AGUARDANDO_APROVACAO"
        )
        second = subprocess.run(
            [sys.executable, "-c", common + confirm],
            check=False,
            input=first.stdout,
            env=env,
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert second.returncode == 0 and second.stdout.strip() == "confirmed", (
            "Retomada não concluiu."
        )
    finally:
        assert database_name.startswith("iai_test_")
        client.drop_database(database_name)
        client.close()
