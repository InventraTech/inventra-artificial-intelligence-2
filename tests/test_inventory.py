from contextlib import contextmanager
from copy import deepcopy
from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID

import pytest
from pydantic import ValidationError

from iai.app.domain import DomainError, Purchase, ReadQuery, Withdrawal
from iai.app.security import ExecutionContext, authorize
from iai.app.services import InventoryService

USER = str(UUID(int=1))
CTX = ExecutionContext(USER, "session")


class FakeRepository:
    def __init__(self):
        self.user = {"user_id": USER, "kitchen_id": 7, "kitchen_active": True, "active": True,
                         "access_type": "ESTOQUISTA_JBS_JR_U1", "kitchen_name": "Cozinha 7"}
        self.product_row = {"id_product": 10, "name": "Tomate", "brand": "Marca", "active": True,
                                "symbol": "UN", "unit_description": "Unidade"}
        self.batch_rows = [{"id_batch": 20, "id_product": 10, "id_kitchen": 7, "batch_number": "L20",
                               "current_quantity": Decimal(20), "expiration_date": date(2030, 1, 3), "status": "ACTIVE"}]
        self.supplier_row = {"id_supplier": 30, "legal_name": "Fornecedor", "active": True, "reference_price": Decimal(3)}
        self.read_rows = []
        self.writes = []
        self.fail_item = False
        self.locks = []

    def principal(self, user_id, lock=False):
        self.locks.append(("principal", lock))
        return deepcopy(self.user)

    def today(self):
        return date(2030, 1, 1)

    def product(self, product_id, lock=False):
        self.locks.append(("product", lock))
        return deepcopy(self.product_row)

    def batches(self, product_id, kitchen_id, batch_id, lock=False):
        self.locks.append(("batches", lock))
        return deepcopy([b for b in self.batch_rows if b["id_kitchen"] == kitchen_id
                         and b["id_product"] == product_id and (batch_id is None or b["id_batch"] == batch_id)])

    def supplier(self, product_id, supplier_id, lock=False):
        return deepcopy(self.supplier_row)

    def read(self, query, kitchen_id):
        assert kitchen_id == 7
        return deepcopy(self.read_rows)

    def withdraw(self, batch_id, kitchen_id, quantity):
        self.batch_rows[0]["current_quantity"] -= quantity
        self.writes.append("withdrawal")
        return {"id_batch": batch_id, "current_quantity": self.batch_rows[0]["current_quantity"]}

    def create_purchase(self, kitchen_id, user_id, purchase):
        self.writes.append("header")
        if self.fail_item:
            raise DomainError("CONFLICT", "item failed")
        self.writes.append("items")
        return {"id_requisition": 100, "status": "UNDER_REVIEW"}


@pytest.fixture
def business():
    repo = FakeRepository()
    events = []

    @contextmanager
    def transactions(write=False):
        before = deepcopy((repo.writes, repo.batch_rows))
        events.append(("begin", write))
        try:
            yield repo
        except Exception:
            repo.writes, repo.batch_rows = before
            events.append(("rollback", write))
            raise
        else:
            events.append(("commit", write))

    return SimpleNamespace(repo=repo, service=InventoryService(transactions, lambda r: r), events=events)


def withdrawal(**kwargs):
    return Withdrawal.model_validate(dict(product_id=10, quantity="5", unit="UN", **kwargs))


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity", "0.0001", "1000000000", True])
def test_invalid_quantities(quantity):
    with pytest.raises(ValidationError):
        Withdrawal(product_id=10, quantity=quantity, unit="UN")


@pytest.mark.parametrize("payload", [{"product_id": 0}, {"product_id": "10"}, {"user_id": USER}, {"kitchen_id": 1}, {"sql": "DELETE FROM tb_stock_batch"}])
def test_untrusted_extra_fields_and_ids(payload):
    values = {"product_id": 10, "quantity": "5", "unit": "UN"}
    values.update(payload)
    with pytest.raises(ValidationError):
        Withdrawal.model_validate(values)


def test_prepare_does_not_write_and_freezes_batch(business):
    proposal = business.service.prepare(CTX, "withdrawal", withdrawal())
    assert proposal["parameters"]["batch_id"] == 20
    assert proposal["snapshot"]["balance_after"] == "15"
    assert business.repo.writes == []
    assert business.events == [("begin", False), ("commit", False)]


def test_confirm_revalidates_locks_and_commits(business):
    proposal = business.service.prepare(CTX, "withdrawal", withdrawal())
    response = business.service.execute(CTX, proposal)
    assert response["success"] and response["data"]["current_quantity"] == "15"
    assert business.events[-1] == ("commit", True)
    assert ("principal", True) in business.repo.locks
    assert ("product", True) in business.repo.locks
    assert ("batches", True) in business.repo.locks


@pytest.mark.parametrize("change,code", [
    (lambda r: r.batch_rows[0].update(current_quantity=Decimal(4)), "INSUFFICIENT_STOCK"),
    (lambda r: r.batch_rows[0].update(current_quantity=Decimal(19)), "CONFLICT"),
    (lambda r: r.batch_rows[0].update(status="CANCELLED"), "CONFLICT"),
    (lambda r: r.batch_rows[0].update(expiration_date=date(2029, 1, 1)), "CONFLICT"),
    (lambda r: r.product_row.update(symbol="KG"), "INVALID_UNIT"),
    (lambda r: r.product_row.update(active=False), "CONFLICT"),
    (lambda r: r.user.update(active=False), "FORBIDDEN"),
    (lambda r: r.user.update(kitchen_id=8), "NOT_FOUND"),
    (lambda r: r.user.update(access_type="COMPRADOR_X"), "FORBIDDEN"),
])
def test_state_change_after_preview_never_writes(business, change, code):
    proposal = business.service.prepare(CTX, "withdrawal", withdrawal())
    change(business.repo)
    with pytest.raises(DomainError) as exc:
        business.service.execute(CTX, proposal)
    assert exc.value.code == code
    assert business.repo.writes == []
    assert business.events[-1] == ("rollback", True)


def test_ambiguous_batches_return_candidates(business):
    business.repo.batch_rows.append({**business.repo.batch_rows[0], "id_batch": 21})
    with pytest.raises(DomainError) as exc:
        business.service.prepare(CTX, "withdrawal", withdrawal())
    assert exc.value.code == "AMBIGUOUS"
    assert len(exc.value.data) == 2
    assert not business.repo.writes


def test_explicit_batch_resolves_ambiguity(business):
    business.repo.batch_rows.append({**business.repo.batch_rows[0], "id_batch": 21})
    assert business.service.prepare(CTX, "withdrawal", withdrawal(batch_id=20))


@pytest.mark.parametrize("unit,qty,code", [("KG", "5", "INVALID_UNIT"), ("UN", "0.5", "INVALID_PARAMETER"), ("UN", "999999", "INSUFFICIENT_STOCK")])
def test_invalid_stock_request(business, unit, qty, code):
    with pytest.raises(DomainError) as exc:
        business.service.prepare(CTX, "withdrawal", Withdrawal(product_id=10, quantity=qty, unit=unit))
    assert exc.value.code == code


def test_missing_product(business):
    business.repo.product_row = None
    with pytest.raises(DomainError, match="Produto não encontrado"):
        business.service.prepare(CTX, "withdrawal", withdrawal())


@pytest.mark.parametrize("role,operation,allowed", [
    ("ESTOQUISTA_X", "withdrawal", True), ("COMPRADOR_X", "purchase", True),
    ("SUPERVISOR_X", "withdrawal", False), ("SUPERVISOR_X", "purchase", False),
    ("COMPRADOR_X", "withdrawal", False), ("ESTOQUISTA_X", "purchase", False),
    ("ADMIN", "read", False), ("ESTOQUISTAevil", "read", False),
])
def test_role_matrix(business, role, operation, allowed):
    business.repo.user["access_type"] = role
    if allowed:
        assert authorize(business.repo, USER, operation=operation)
    else:
        with pytest.raises(DomainError):
            authorize(business.repo, USER, operation=operation)


@pytest.mark.parametrize("resource", ["painel", "desperdicio", "requisicoes", "requisicoes_pendentes"])
def test_read_permissions(business, resource):
    with pytest.raises(DomainError) as exc:
        business.service.read(CTX, ReadQuery(resource=resource))
    assert exc.value.code == "FORBIDDEN"


def test_no_results_and_truncation(business):
    response = business.service.read(CTX, ReadQuery(resource="lotes"))
    assert response["code"] == "NO_RESULTS" and response["success"]
    business.repo.read_rows = [{"id_batch": i} for i in range(3)]
    response = business.service.read(CTX, ReadQuery(resource="lotes", limit=2))
    assert response["count"] == 2 and response["truncated"]


def purchase():
    return Purchase(items=[{"product_id": 10, "quantity": "20", "unit": "UN"}])


def test_purchase_optional_fields_not_invented(business):
    business.repo.user["access_type"] = "COMPRADOR_X"
    proposal = business.service.prepare(CTX, "purchase", purchase())
    assert proposal["snapshot"]["items"][0]["supplier"] is None
    assert proposal["snapshot"]["items"][0]["estimated_price"] is None
    assert business.repo.writes == []
    assert business.service.execute(CTX, proposal)["code"] == "COMPLETED"
    assert business.repo.writes == ["header", "items"]


def test_purchase_rolls_back_header_if_item_fails(business):
    business.repo.user["access_type"] = "COMPRADOR_X"
    proposal = business.service.prepare(CTX, "purchase", purchase())
    business.repo.fail_item = True
    with pytest.raises(DomainError):
        business.service.execute(CTX, proposal)
    assert business.repo.writes == []
    assert business.events[-1] == ("rollback", True)


def test_missing_supplier_catalog_link(business):
    business.repo.user["access_type"] = "COMPRADOR_X"
    business.repo.supplier_row = None
    data = purchase()
    data.items[0].supplier_id = 30
    with pytest.raises(DomainError) as exc:
        business.service.prepare(CTX, "purchase", data)
    assert exc.value.code == "NOT_FOUND"


@pytest.mark.parametrize("items", [[], [{"quantity": 20, "unit": "KG"}],
    [{"product_id": 10, "quantity": 20, "unit": "UN"}]*2])
def test_invalid_requisitions(items):
    with pytest.raises(ValidationError):
        Purchase(items=items)


def test_database_failure_not_empty_result():
    @contextmanager
    def broken(**kwargs):
        raise DomainError("DATABASE_UNAVAILABLE", "offline")
        yield  # pragma: no cover
    with pytest.raises(DomainError) as exc:
        InventoryService(broken).read(CTX, ReadQuery(resource="lotes"))
    assert exc.value.code == "DATABASE_UNAVAILABLE"


@pytest.mark.parametrize("values", [{"start": "2030-02-02", "end": "2030-01-01"},
    {"period": "hoje", "start": "2030-01-01"}, {"period": "proximos_dias"}, {"days": 2},
    {"limit": 0}, {"limit": 101}, {"resource": "SELECT * FROM tb_user"}])
def test_invalid_query_inputs(values):
    with pytest.raises(ValidationError):
        ReadQuery.model_validate({"resource": "lotes", **values})
