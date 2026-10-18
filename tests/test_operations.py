"""Regras e persistência do adaptador de negócio MOCK; não comprova Spring real."""

import pytest

from iai.app.business import MockGateway
from iai.app.domain import Query
from iai.app.storage import get_db
from tests_helpers import approved, context_for


def test_product_lifecycle_is_logical_and_preserves_records():
    created = approved(
        "criar_produto", {"name": "Feijão", "unitId": 1, "barcode": "789-demo"}
    )
    identifier = created["result"]["data"]["id"]
    changed = approved(
        "editar_produto", {"productId": identifier, "name": "Feijão preto"}
    )
    assert changed["result"]["data"]["name"] == "Feijão preto"
    inactive = approved("desativar_produto", {"productId": identifier})
    assert not inactive["result"]["data"]["active"]
    assert get_db().business_mock.find_one({"_id": f"1:product:{identifier}"})


def test_supplier_lifecycle_and_new_batch():
    supplier = approved(
        "cadastrar_fornecedor", {"legalName": "Fornecedor B", "cnpj": "11111111111111"}
    )
    sid = supplier["result"]["data"]["id"]
    lot = approved(
        "criar_lote",
        {
            "productId": 1,
            "supplierId": sid,
            "batchNumber": "TEST-BATCH",
            "initialQuantity": "2.500",
            "entryDate": "2026-10-14",
            "expirationDate": "2099-12-31",
        },
    )
    assert lot["status"] == "CONCLUIDA"
    assert lot["result"]["data"]["supplier"]["id"] == sid
    assert (
        approved("desativar_fornecedor", {"supplierId": sid})["result"]["data"][
            "active"
        ]
        is False
    )


def test_restock_adjustment_and_insufficient_balance():
    assert (
        approved("registrar_entrada", {"batchId": 1, "quantity": 3})["result"]["data"][
            "currentQuantity"
        ]
        == 53
    )
    assert (
        approved("ajustar_estoque", {"batchId": 1, "newQuantity": 8})["result"]["data"][
            "currentQuantity"
        ]
        == 8
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as denied:
        approved("registrar_saida", {"batchId": 1, "quantity": 10})
    assert denied.value.status_code == 409
    assert (
        get_db().business_mock.find_one({"_id": "1:batch:1"})["data"]["currentQuantity"]
        == 8
    )


def test_requisition_items_are_added_removed_and_cancelled_with_approval():
    req = approved("criar_requisicao", {"type": "PURCHASE", "origin": "cozinha"})
    rid = req["result"]["data"]["id"]
    assert req["result"]["data"]["requester"]["id"] == "demo-comprador"
    item = approved(
        "adicionar_item_requisicao",
        {"requisitionId": rid, "productId": 1, "quantity": 10},
    )
    item_id = item["result"]["data"]["id"]
    assert (
        approved("remover_item_requisicao", {"requisitionId": rid, "itemId": item_id})[
            "status"
        ]
        == "CONCLUIDA"
    )
    assert (
        MockGateway()
        .query(Query(resource="itens_requisicao", record_id=rid), context_for())
        .data
        == []
    )
    assert get_db().business_mock.find_one(
        {"_id": f"1:item:{item_id}"}
    )  # sem exclusão física no mock
    assert (
        approved("cancelar_requisicao", {"requisitionId": rid, "reason": "Duplicada"})[
            "result"
        ]["data"]["status"]
        == "CANCELLED"
    )


def test_inventory_counts_edit_without_changing_stock():
    MockGateway().query(Query(resource="inventarios"), context_for())
    get_db().business_mock.update_one(
        {"_id": "1:inventory:1"}, {"$set": {"data.status": "CLOSED"}}
    )
    inv = approved("registrar_inventario", {"note": "Contagem periódica"})
    iid = inv["result"]["data"]["id"]
    count = approved(
        "registrar_contagem", {"inventoryId": iid, "batchId": 1, "physicalQuantity": 40}
    )
    cid = count["result"]["data"]["id"]
    edited = approved(
        "alterar_contagem",
        {"inventoryId": iid, "countId": cid, "batchId": 1, "physicalQuantity": 42},
    )
    assert edited["result"]["data"]["divergence"] == -8
    assert (
        get_db().business_mock.find_one({"_id": "1:batch:1"})["data"]["currentQuantity"]
        == 50
    )


@pytest.mark.parametrize(
    "resource",
    [
        "produtos",
        "lotes",
        "validade",
        "estoque_minimo",
        "fornecedores",
        "fornecedores_recorrentes",
        "requisicoes",
        "inventarios",
        "alertas",
        "categorias",
        "unidades",
        "cozinha",
    ],
)
def test_all_domain_queries_are_labeled_mock(resource):
    response = MockGateway().query(Query(resource=resource), context_for())
    assert response.ok
    assert response.mocked
    assert "SIMULAÇÃO" in response.message


def test_expired_batches_are_not_available_stock_and_small_pages_are_incomplete():
    MockGateway().query(Query(resource="lotes"), context_for())
    get_db().business_mock.update_one(
        {"_id": "1:batch:1"}, {"$set": {"data.expirationDate": "2000-01-01"}}
    )
    out = MockGateway().query(Query(resource="estoque", product_id=1), context_for())
    assert out.data[0]["currentQuantity"] == 0
    assert (
        MockGateway()
        .query(Query(resource="produtos", size=1), context_for())
        .incomplete
    )
