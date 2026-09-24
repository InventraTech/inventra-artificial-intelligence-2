import json
from typing import Any

from pydantic import BaseModel

from iai.app.database import transaction
from iai.app.domain import DomainError, Purchase, ReadQuery, Withdrawal, result
from iai.app.repository import Repository
from iai.app.security import ExecutionContext, authorize


def serializable(value: Any) -> Any:
    """Preserve decimal precision; never convert inventory quantities to float."""
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


class InventoryService:
    def __init__(self, transactions=transaction, repository=Repository):
        self.transactions, self.repository = transactions, repository

    def read(self, context: ExecutionContext, query: ReadQuery) -> dict:
        with self.transactions() as conn:
            repo = self.repository(conn)
            user = authorize(repo, context.user_id, resource=query.resource)
            if query.product_id is not None and repo.product(query.product_id) is None:
                raise DomainError("NOT_FOUND", "Produto não encontrado.")
            rows = repo.read(query, user["kitchen_id"])
            truncated = len(rows) > query.limit
            rows = rows[:query.limit]
            return result(serializable(rows), code="OK" if rows else "NO_RESULTS",
                          truncated=truncated, resource=query.resource,
                          message=("Indicador proxy; não representa medição direta de desperdício."
                                   if query.resource == "desperdicio" else None))

    def _product(self, repo, item, lock: bool) -> dict:
        product = repo.product(item.product_id, lock=lock)
        if not product:
            raise DomainError("NOT_FOUND", "Produto não encontrado.")
        if not product["active"]:
            raise DomainError("CONFLICT", "Produto inativo.")
        if item.unit.casefold() not in {product["symbol"].casefold(), product["unit_description"].casefold()}:
            raise DomainError("INVALID_UNIT", "Unidade incompatível; não há conversão automática.",
                              {"expected_unit": product["symbol"]})
        if product["symbol"].upper() == "UN" and item.quantity != item.quantity.to_integral_value():
            raise DomainError("INVALID_PARAMETER", "A quantidade em UN deve ser inteira.")
        return product

    def _validate(self, repo, user, operation: str, data, *, lock: bool = False) -> dict:
        if operation == "withdrawal":
            product = self._product(repo, data, lock)
            batches = repo.batches(data.product_id, user["kitchen_id"], data.batch_id, lock=lock)
            if not batches:
                raise DomainError("NOT_FOUND", "Nenhum lote correspondente nesta cozinha.")
            if len(batches) > 1:
                raise DomainError("AMBIGUOUS", "Informe o lote; há mais de uma correspondência.",
                                  serializable(batches[:100]))
            batch = batches[0]
            if batch["status"] != "ACTIVE":
                raise DomainError("CONFLICT", "Somente lotes ACTIVE permitem consumo.")
            if batch["expiration_date"] and batch["expiration_date"] < repo.today():
                raise DomainError("CONFLICT", "O lote está vencido e não permite registro de consumo.")
            if data.quantity > batch["current_quantity"]:
                raise DomainError("INSUFFICIENT_STOCK", "Quantidade maior que o saldo disponível.",
                                  serializable({"available": batch["current_quantity"]}))
            return serializable({"operation": operation, "kitchen_id": user["kitchen_id"],
                "kitchen_name": user["kitchen_name"], "product": product, "batch": batch,
                "quantity": data.quantity, "unit": product["symbol"],
                "balance_after": batch["current_quantity"] - data.quantity})
        items = []
        for item in data.items:
            product = self._product(repo, item, lock)
            supplier = None
            if item.supplier_id is not None:
                supplier = repo.supplier(item.product_id, item.supplier_id, lock=lock)
                if not supplier or not supplier["active"]:
                    raise DomainError("NOT_FOUND", "Fornecedor ativo não encontrado no catálogo deste produto.")
            items.append({"product": product, "supplier": supplier, **item.model_dump()})
        return serializable({"operation": operation, "kitchen_id": user["kitchen_id"],
                             "kitchen_name": user["kitchen_name"], "items": items,
                             "reason": data.reason, "status": "UNDER_REVIEW"})

    def prepare(self, context: ExecutionContext, operation: str, data: Withdrawal | Purchase) -> dict:
        with self.transactions() as conn:
            repo = self.repository(conn)
            user = authorize(repo, context.user_id, operation=operation)
            snapshot = self._validate(repo, user, operation, data)
            # Freeze the unambiguous batch so confirmation cannot silently select another.
            if isinstance(data, Withdrawal):
                data = data.model_copy(update={"batch_id": snapshot["batch"]["id_batch"]})
            return {"operation": operation, "parameters": serializable(data), "snapshot": snapshot}

    def execute(self, context: ExecutionContext, proposal: dict) -> dict:
        operation = proposal["operation"]
        if operation not in {"withdrawal", "purchase"}:
            raise DomainError("INVALID_PARAMETER", "Operação não suportada.")
        data = (Withdrawal if operation == "withdrawal" else Purchase).model_validate(proposal["parameters"])
        with self.transactions(write=True) as conn:
            repo = self.repository(conn)
            user = authorize(repo, context.user_id, operation=operation, lock=True)
            snapshot = self._validate(repo, user, operation, data, lock=True)
            if snapshot != proposal["snapshot"]:
                raise DomainError("CONFLICT", "Os dados mudaram após o resumo. Solicite uma nova proposta.")
            if isinstance(data, Withdrawal):
                value = repo.withdraw(snapshot["batch"]["id_batch"],user["kitchen_id"],data.quantity)
            else:
                value = repo.create_purchase(user["kitchen_id"],context.user_id,data)
        # Exit/COMMIT must complete before success is constructed.
        return result(serializable(value), operation=operation, code="COMPLETED")


service = InventoryService()
