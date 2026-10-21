import httpx
from pydantic import SecretStr

from iai.app.core.infra.security import Identity, RequestContext
from iai.app.core.inventra.business import MockGateway, SpringGateway
from iai.app.core.inventra.domain import Query
from tests_helpers import context_for


def real_context():
    return RequestContext(
        Identity(user_id="original", kitchen_id=7, role="SUPERVISOR", name="Pessoa"),
        SecretStr("test-token"),
    )


def test_my_requisitions_use_authenticated_requester_not_client_id(monkeypatch):
    calls = []

    def request(method, url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(200, json=[])

    monkeypatch.setattr(httpx, "request", request)
    SpringGateway().query(
        Query(resource="requisicoes", mine=True, status="UNDER_REVIEW"), real_context()
    )
    assert calls[0]["params"] == {"requesterId": "original"}


def test_supplier_frequency_uses_all_authorized_lots_not_first_page(monkeypatch):
    lots = [
        {
            "id": i,
            "product": {"id": 1},
            "supplier": {"id": 4},
            "currentQuantity": 1,
            "status": "ACTIVE",
        }
        for i in range(1, 41)
    ]
    monkeypatch.setattr(
        httpx, "request", lambda *a, **kw: httpx.Response(200, json=lots)
    )
    out = SpringGateway().query(
        Query(resource="fornecedores_recorrentes"), real_context()
    )
    assert out.data == [{"supplierId": 4, "receivedBatches": 40}]


def test_related_product_queries_and_my_requisitions():
    linked = MockGateway().query(
        Query(resource="fornecedores_produto", product_id=1), context_for()
    )
    assert linked.mocked and linked.data[0]["supplier"]["id"] == 1
    assert (
        MockGateway()
        .query(Query(resource="parametros_produto", product_id=1), context_for())
        .data[0]["minStock"]
        == 60
    )
    assert (
        MockGateway()
        .query(Query(resource="requisicoes", mine=True), context_for())
        .data
        == []
    )
    assert (
        len(
            MockGateway()
            .query(
                Query(resource="requisicoes", mine=True), context_for("demo-comprador")
            )
            .data
        )
        == 1
    )


def test_stock_without_product_uses_kitchen_stock_endpoint(monkeypatch):
    calls = []
    rows = [
        {
            "product": {"id": 1, "name": "Arroz Camil"},
            "totalQuantity": 25,
            "minStock": 20,
            "lowStock": False,
        }
    ]

    def request(method, url, **kwargs):
        calls.append(url)
        return httpx.Response(200, json=rows)

    monkeypatch.setattr(httpx, "request", request)
    out = SpringGateway().query(Query(resource="estoque"), real_context())
    assert out.ok and calls[0].endswith("/api/kitchens/7/stock")
    assert out.data[0]["totalQuantity"] == 25
    filtrado = SpringGateway().query(
        Query(resource="estoque", name="arroz"), real_context()
    )
    assert len(filtrado.data) == 1
