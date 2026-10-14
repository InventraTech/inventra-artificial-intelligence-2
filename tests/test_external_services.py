"""Opt-in: serviços reais. Não são executados sem disponibilidade explícita."""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

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
    from iai.app.config import API_URL

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
