"""Adaptadores de negócio. Só o futuro HITL (core/inventra/acoes.py) deve chamar execute()."""

from collections import Counter
from datetime import timedelta
from decimal import Decimal
from threading import RLock
from typing import Protocol

import httpx
from pymongo import ReturnDocument

from iai.app.core.config import API_URL, BUSINESS_MODE, HTTP_TIMEOUT, business_date
from iai.app.core.infra.security import RequestContext
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.domain import Action, Operation, Query, ToolResult


class BusinessGateway(Protocol):
    def query(self, query: Query, ctx: RequestContext) -> ToolResult: ...
    def execute(
        self, action: Action, requester: dict, ctx: RequestContext, key: str
    ) -> ToolResult: ...


def result(ok, code, message, data=None, *, mocked=False, incomplete=False):
    return ToolResult(
        ok=ok,
        code=code,
        message=message,
        data=data,
        source="MOCK local INVENTRA" if mocked else "API Spring INVENTRA",
        mocked=mocked,
        incomplete=incomplete,
    )


ERRORS = {
    400: "PARAMETROS_INVALIDOS",
    401: "NAO_AUTENTICADO",
    403: "SEM_PERMISSAO",
    404: "NAO_ENCONTRADO",
    409: "CONFLITO",
    429: "LIMITE_EXCEDIDO",
}


def redact_secrets(value):
    """Segredos de uma resposta externa nunca entram na evidência enviada ao modelo."""
    if isinstance(value, dict):
        return {
            key: redact_secrets(item)
            for key, item in value.items()
            if not any(
                word in key.lower()
                for word in (
                    "token",
                    "password",
                    "senha",
                    "secret",
                    "authorization",
                    "api_key",
                )
            )
        }
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, str):
        from iai.app.core.infra.privacy import remove_secrets

        return remove_secrets(value)
    return value


class SpringGateway:
    def query(self, query, ctx):
        k = ctx.identity.kitchen_id
        resource = query.resource
        paths = {
            "produtos": "/api/products",
            "fornecedores": "/api/suppliers",
            "lotes": "/api/stock-batches",
            "validade": "/api/stock-batches/expiring-soon",
            "estoque_minimo": "/api/stock-batches/low-stock",
            "estoque": f"/api/products/{query.product_id}/kitchen-parameters",
            "parametros_produto": f"/api/products/{query.product_id}/kitchen-parameters",
            "fornecedores_produto": f"/api/products/{query.product_id}/suppliers",
            "requisicoes": "/api/requisitions",
            "inventarios": "/api/inventories",
            "alertas": "/api/alerts",
            "categorias": "/api/categories",
            "unidades": "/api/units",
            "cozinha": "/api/kitchens",
            "itens_requisicao": f"/api/requisitions/{query.record_id}/items",
            "contagens": f"/api/inventories/{query.record_id}/counts",
        }
        if resource == "fornecedores_recorrentes":
            lots = self._request(
                "GET",
                "/api/stock-batches",
                ctx,
                params={"kitchenId": k},
                expected="lotes",
            )
            if not lots.ok:
                return lots
            frequency = Counter(
                (row.get("supplier") or {}).get("id") for row in lots.data
            )
            frequency.pop(None, None)
            return result(
                True,
                "OK",
                "Recorrência por número de lotes recebidos da cozinha; não é frequência de pedidos.",
                [
                    {"supplierId": sid, "receivedBatches": n}
                    for sid, n in frequency.most_common()
                ],
            )
        params = {}
        if query.record_id and resource in {
            "fornecedores",
            "inventarios",
            "alertas",
            "categorias",
            "unidades",
        }:
            paths[resource] += f"/{query.record_id}"
        if resource == "produtos":
            params = {"page": query.page, "size": query.size}
            if query.name:
                params["name"] = query.name
            if query.product_id:
                paths[resource] += f"/{query.product_id}"
                params = {}
        elif resource == "lotes":
            params = (
                {"productId": query.product_id}
                if query.product_id
                else {"kitchenId": k}
            )
        elif resource == "validade":
            params = {"kitchenId": k, "days": query.days}
        elif resource == "requisicoes":
            params = (
                {"requesterId": ctx.identity.user_id}
                if query.mine
                else {"status": query.status}
                if query.status
                else {"kitchenId": k}
            )
        elif resource in {"inventarios", "alertas"}:
            params = {} if query.record_id else {"kitchenId": k}
            if resource == "alertas":
                params["unread"] = query.unread
        expected = resource
        if resource == "estoque" and not query.product_id:
            # Saldo geral da cozinha (mesmo endpoint do app): total ativo por produto.
            paths[resource] = f"/api/kitchens/{k}/stock"
            expected = "estoque_cozinha"
        out = self._request(
            "GET", paths[resource], ctx, params=params, expected=expected
        )
        if not out.ok:
            return out
        # Filtros locais somente sobre dados já autorizados; não inventar filtros HTTP.
        from iai.app.core.infra.retrieval import normalize

        data = out.data
        rows = data.get("content", [data]) if isinstance(data, dict) else data
        if query.record_id and resource not in {"itens_requisicao", "contagens"}:
            rows = [r for r in rows if r.get("id") == query.record_id]
        if query.product_id and resource in {"lotes", "validade"}:
            rows = [
                r
                for r in rows
                if (r.get("product") or {}).get("id") == query.product_id
            ]
        if query.name:
            rows = [
                r
                for r in rows
                if normalize(query.name)
                in normalize(
                    r.get(
                        "name",
                        r.get("legalName", (r.get("product") or {}).get("name", "")),
                    )
                )
            ]
        if query.status:
            rows = [r for r in rows if r.get("status") == query.status]
        if query.mine and resource == "inventarios":
            rows = [
                r
                for r in rows
                if (r.get("responsible") or {}).get("id") == ctx.identity.user_id
            ]
        if isinstance(data, dict) and "content" in data:
            out.data = dict(data, content=rows)
        elif isinstance(data, dict):
            out.data = rows[0] if rows else []
        else:
            total = len(rows)
            out.data = rows[query.page * query.size : (query.page + 1) * query.size]
            out.incomplete = total > (query.page + 1) * query.size
        if not rows:
            out.code = "VAZIO"
        return out

    def execute(self, action, requester, ctx, key):
        if ctx.identity.mocked or ctx.identity.user_id != requester["user_id"]:
            return result(
                False,
                "INTEGRACAO_PENDENTE",
                "A API precisa de delegação segura do solicitante; nenhum token do gestor foi usado.",
            )
        p = dict(action.parameters)
        # Contrato Spring usa números JSON para BigDecimal (até 12 dígitos validados).
        for numeric_field in (
            "quantity",
            "newQuantity",
            "initialQuantity",
            "physicalQuantity",
            "unitPrice",
            "estimatedPrice",
        ):
            if numeric_field in p:
                p[numeric_field] = float(Decimal(str(p[numeric_field])))
        op = action.operation
        if op == Operation.REMOVE_ITEM:
            return result(
                False,
                "INTEGRACAO_PENDENTE",
                "A API documenta DELETE de item, mas não foi comprovada remoção lógica. Não executamos exclusão física.",
            )
        method, path = "POST", ""
        if op in {Operation.CREATE_PRODUCT, Operation.CREATE_SUPPLIER}:
            path = (
                "/api/products" if op == Operation.CREATE_PRODUCT else "/api/suppliers"
            )
        elif op in {Operation.EDIT_PRODUCT, Operation.DEACTIVATE_PRODUCT}:
            path = f"/api/products/{p.pop('productId')}"
            method = "PUT" if op == Operation.EDIT_PRODUCT else "PATCH"
            if op == Operation.DEACTIVATE_PRODUCT:
                path += "/deactivate"
        elif op == Operation.DEACTIVATE_SUPPLIER:
            method, path = "PATCH", f"/api/suppliers/{p.pop('supplierId')}/deactivate"
        elif op == Operation.CREATE_BATCH:
            path = "/api/stock-batches"
            p["kitchenId"] = requester["kitchen_id"]
        elif op in {Operation.RESTOCK, Operation.CONSUME, Operation.ADJUST}:
            suffix = {
                Operation.RESTOCK: "restock",
                Operation.CONSUME: "consume",
                Operation.ADJUST: "adjust",
            }[op]
            method, path = "PATCH", f"/api/stock-batches/{p.pop('batchId')}/{suffix}"
        elif op == Operation.CREATE_REQUISITION:
            path = "/api/requisitions"
            p["kitchenId"] = requester["kitchen_id"]
        elif op in {Operation.ADD_ITEM, Operation.CANCEL_REQUISITION}:
            path = f"/api/requisitions/{p.pop('requisitionId')}"
            if op == Operation.CANCEL_REQUISITION:
                method, path = "PATCH", path + "/cancel"
            else:
                path += "/items"
        elif op == Operation.CREATE_INVENTORY:
            path = "/api/inventories"
            p.update(
                kitchenId=requester["kitchen_id"], responsibleId=requester["user_id"]
            )
        elif op == Operation.ADD_COUNT:
            path = f"/api/inventories/{p.pop('inventoryId')}/counts"
        else:
            return result(
                False,
                "INTEGRACAO_PENDENTE",
                "Não há endpoint documentado para alterar contagem. Não removemos/recriamos registros.",
            )
        # Envio único: não inventar header de idempotência ou endpoint de delegação.
        created = {
            Operation.CREATE_PRODUCT,
            Operation.CREATE_SUPPLIER,
            Operation.CREATE_BATCH,
            Operation.CREATE_REQUISITION,
            Operation.CREATE_INVENTORY,
            Operation.ADD_COUNT,
        }
        expected_status = (
            201
            if op in created
            else 204
            if op in {Operation.DEACTIVATE_PRODUCT, Operation.DEACTIVATE_SUPPLIER}
            else 200
        )
        return self._request(
            method, path, ctx, body=p or None, allowed_statuses={expected_status}
        )

    def _request(
        self,
        method,
        path,
        ctx,
        params=None,
        body=None,
        expected=None,
        allowed_statuses=None,
    ):
        if ctx.identity.mocked:
            return result(
                False, "SEM_PERMISSAO", "Identidade mock não acessa a API real."
            )
        try:
            response = httpx.request(
                method,
                f"{API_URL}{path}",
                params=params,
                json=body,
                headers={"Authorization": "Bearer " + ctx.token.get_secret_value()},
                timeout=HTTP_TIMEOUT,
                follow_redirects=False,
            )
            if response.status_code not in (allowed_statuses or {200}):
                code = ERRORS.get(response.status_code, "SERVICO_INDISPONIVEL")
                if method == "GET" and 200 <= response.status_code < 300:
                    code = "RESPOSTA_INVALIDA"
                if method != "GET" and (
                    response.status_code >= 500 or 200 <= response.status_code < 300
                ):
                    code = "RESULTADO_INCERTO"
                return result(
                    False,
                    code,
                    "A API recusou a operação ou não confirmou o resultado.",
                )
            data = (
                None if response.status_code == 204 else redact_secrets(response.json())
            )
            if response.status_code != 204 and not isinstance(data, (list, dict)):
                raise ValueError("Resposta inválida")
            if (
                isinstance(data, dict)
                and "content" in data
                and not isinstance(data["content"], list)
            ):
                raise ValueError("Página inválida")
            rows = (
                data
                if isinstance(data, list)
                else data.get("content", [data])
                if data
                else []
            )
            required = {
                "parametros_produto": {"kitchen", "minStock", "currentQuantity"},
                "fornecedores_produto": {"supplier"},
                "estoque": {"currentQuantity", "minStock"},
                "estoque_cozinha": {"product", "totalQuantity"},
                "estoque_minimo": {"productId", "currentQuantity"},
                "lotes": {"id", "product", "currentQuantity", "status"},
                "validade": {
                    "id",
                    "product",
                    "currentQuantity",
                    "expirationDate",
                    "status",
                },
                "produtos": {"id", "name", "unit"},
                "fornecedores": {"id", "legalName"},
                "requisicoes": {"id", "status", "requester"},
                "inventarios": {"id", "status"},
                "contagens": {"id", "physicalQuantity"},
                "itens_requisicao": {"id", "quantity"},
                "alertas": {"id", "message"},
                "categorias": {"id", "name"},
                "unidades": {"id", "symbol"},
                "cozinha": {"id", "name"},
            }
            for row in rows:
                if not isinstance(row, dict):
                    raise TypeError("Registro inválido")
                if expected and not required[expected].issubset(row):
                    raise ValueError("Campos obrigatórios da resposta ausentes")
                if "id" in row and (
                    isinstance(row["id"], bool) or not isinstance(row["id"], int)
                ):
                    raise ValueError("Identificador inválido")
                for text_field in ("name", "legalName", "status", "symbol", "message"):
                    if text_field in row and not isinstance(row[text_field], str):
                        raise ValueError("Texto de resposta inválido")
                for numeric in (
                    "quantity",
                    "currentQuantity",
                    "physicalQuantity",
                    "minStock",
                ):
                    if numeric in row:
                        try:
                            value = Decimal(str(row[numeric]))
                            if not value.is_finite() or value < 0:
                                raise ValueError("Quantidade inválida")
                        except ArithmeticError:
                            raise ValueError("Quantidade inválida") from None
                for nested in ("kitchen", "product", "unit", "requester", "supplier"):
                    if (
                        nested in row
                        and row[nested] is not None
                        and not isinstance(row[nested], dict)
                    ):
                        raise ValueError("Referência inválida")
                kitchen = row.get("kitchen") or {}
                if (
                    kitchen.get("id", row.get("kitchenId", ctx.identity.kitchen_id))
                    != ctx.identity.kitchen_id
                ):
                    return result(
                        False,
                        "SEM_PERMISSAO",
                        "Resposta fora da cozinha autorizada foi bloqueada.",
                    )
            if (
                method != "GET"
                and response.status_code in {200, 201}
                and not isinstance(data.get("id"), int)
            ):
                raise ValueError("Identificador de execução ausente")
            return result(
                True,
                "OK",
                "Resultado confirmado pela API.",
                data,
                incomplete=isinstance(data, dict) and data.get("totalPages", 1) > 1,
            )
        except httpx.HTTPError:
            return result(
                False,
                "RESULTADO_INCERTO" if method != "GET" else "SERVICO_INDISPONIVEL",
                "Comunicação interrompida. Escritas incertas não são repetidas automaticamente.",
            )
        except (ValueError, TypeError, AttributeError):
            return result(
                False,
                "RESULTADO_INCERTO" if method != "GET" else "RESPOSTA_INVALIDA",
                "O serviço devolveu uma resposta que não pôde ser validada.",
            )


_mock_lock = RLock()


class MockGateway:
    """Dados de exemplo persistidos no adaptador Mongo escolhido; não são estoque real."""

    def _collection(self, kitchen):
        col = get_db().business_mock
        if not col.find_one({"kitchen": kitchen, "kind": "product"}):
            today = business_date()
            fixtures = [
                ("unit", {"id": 1, "symbol": "kg", "description": "Quilograma"}),
                ("category", {"id": 1, "name": "Alimentos"}),
                (
                    "supplier",
                    {
                        "id": 1,
                        "legalName": "Fornecedor demonstração",
                        "cnpj": "00000000000000",
                        "active": True,
                    },
                ),
            ]
            for pid, name, qty, minimum in [
                (1, "Arroz", 50, 60),
                (2, "Carne", 100, 20),
                (3, "Tomate", 5, 10),
            ]:
                fixtures.extend(
                    [
                        (
                            "product",
                            {
                                "id": pid,
                                "name": name,
                                "unitId": 1,
                                "unit": {"id": 1, "symbol": "kg"},
                                "active": True,
                                "minStock": minimum,
                            },
                        ),
                        (
                            "batch",
                            {
                                "id": pid,
                                "product": {"id": pid, "name": name},
                                "supplier": {
                                    "id": 1,
                                    "legalName": "Fornecedor demonstração",
                                },
                                "batchNumber": f"DEMO-{pid}",
                                "currentQuantity": qty,
                                "initialQuantity": qty,
                                "entryDate": today.isoformat(),
                                "expirationDate": (
                                    today + timedelta(days=pid)
                                ).isoformat(),
                                "status": "ACTIVE",
                            },
                        ),
                    ]
                )
            fixtures.extend(
                [
                    (
                        "requisition",
                        {
                            "id": 1,
                            "type": "PURCHASE",
                            "origin": "demo",
                            "status": "DRAFT",
                            "requester": {"id": "demo-comprador"},
                        },
                    ),
                    (
                        "product_supplier",
                        {
                            "id": 1,
                            "productId": 1,
                            "supplier": {
                                "id": 1,
                                "legalName": "Fornecedor demonstração",
                            },
                            "referencePrice": "8.90",
                            "leadTimeDays": 2,
                        },
                    ),
                    (
                        "inventory",
                        {
                            "id": 1,
                            "status": "OPEN",
                            "responsible": {"id": "demo-estoquista"},
                        },
                    ),
                    (
                        "alert",
                        {
                            "id": 1,
                            "type": "STOCK",
                            "severity": "HIGH",
                            "message": "EXEMPLO: arroz abaixo do mínimo",
                            "read": False,
                        },
                    ),
                ]
            )
            for kind, data in fixtures:
                col.update_one(
                    {"_id": f"{kitchen}:{kind}:{data['id']}"},
                    {"$setOnInsert": {"kitchen": kitchen, "kind": kind, "data": data}},
                    upsert=True,
                )
        return col

    def _rows(self, kitchen, kind):
        return [
            d["data"]
            for d in self._collection(kitchen).find({"kitchen": kitchen, "kind": kind})
        ]

    def query(self, query, ctx):
        with _mock_lock:
            return self._query(query, ctx)

    def _query(self, q, ctx):
        k = ctx.identity.kitchen_id
        kinds = {
            "fornecedores_produto": "product_supplier",
            "produtos": "product",
            "fornecedores": "supplier",
            "requisicoes": "requisition",
            "inventarios": "inventory",
            "alertas": "alert",
            "categorias": "category",
            "unidades": "unit",
            "itens_requisicao": "item",
            "contagens": "count",
        }
        rows = self._rows(k, kinds.get(q.resource, "batch"))
        if q.resource == "cozinha":
            rows = [{"id": k, "name": "Cozinha DEMONSTRAÇÃO"}]
        elif q.resource == "fornecedores_recorrentes":
            frequencies = Counter((r.get("supplier") or {}).get("id") for r in rows)
            rows = [
                {"supplierId": sid, "receivedBatches": n}
                for sid, n in frequencies.most_common()
                if sid
            ]
        elif q.resource in {"estoque", "estoque_minimo", "parametros_produto"}:
            stock = Counter()
            for r in rows:
                if r["status"] == "ACTIVE" and (
                    not r.get("expirationDate")
                    or r["expirationDate"] >= business_date().isoformat()
                ):
                    stock[r["product"]["id"]] += Decimal(str(r["currentQuantity"]))
            rows = [
                {
                    "product": {
                        "id": p["id"],
                        "name": p["name"],
                        "unit": p.get("unit"),
                    },
                    "currentQuantity": float(stock[p["id"]]),
                    "minStock": p.get("minStock", 0),
                    "lowStock": stock[p["id"]] < p.get("minStock", 0),
                }
                for p in self._rows(k, "product")
            ]
            if q.resource == "estoque_minimo":
                rows = [r for r in rows if r["lowStock"]]
        elif q.resource == "validade":
            start, end = (
                business_date().isoformat(),
                (business_date() + timedelta(days=q.days)).isoformat(),
            )
            rows = [
                r
                for r in rows
                if r["status"] == "ACTIVE"
                and r.get("expirationDate")
                and start <= r["expirationDate"] <= end
            ]
        if q.product_id:
            rows = [
                r
                for r in rows
                if (r.get("product") or {}).get("id", r.get("id")) == q.product_id
            ]
        if q.record_id and q.resource not in {"itens_requisicao", "contagens"}:
            rows = [r for r in rows if r.get("id") == q.record_id]
        if q.name:
            from iai.app.core.infra.retrieval import normalize

            rows = [
                r
                for r in rows
                if normalize(q.name) in normalize(r.get("name", r.get("legalName", "")))
            ]
        if q.status:
            rows = [r for r in rows if r.get("status") == q.status]
        if q.mine and q.resource in {"requisicoes", "inventarios"}:
            owner_key = "requester" if q.resource == "requisicoes" else "responsible"
            rows = [
                r
                for r in rows
                if (r.get(owner_key) or {}).get("id") == ctx.identity.user_id
            ]
        if q.resource in {"itens_requisicao", "contagens"}:
            field = (
                "requisitionId" if q.resource == "itens_requisicao" else "inventoryId"
            )
            rows = [r for r in rows if r.get(field) == q.record_id]
        if q.resource == "itens_requisicao":
            rows = [r for r in rows if r.get("active", True)]
        if q.unread:
            rows = [r for r in rows if not r.get("read")]
        total = len(rows)
        rows = rows[q.page * q.size : (q.page + 1) * q.size]
        return result(
            True,
            "OK" if rows else "VAZIO",
            "SIMULAÇÃO: dados de exemplo, não são informações atuais do INVENTRA.",
            rows,
            mocked=True,
            incomplete=total > (q.page + 1) * q.size,
        )

    def execute(self, action, requester, ctx, key):
        with _mock_lock:
            ledger = get_db().execution_mock
            previous = ledger.find_one({"_id": key})
            if previous:
                if previous.get("result"):
                    return ToolResult.model_validate(previous["result"])
                return result(
                    False,
                    "RESULTADO_INCERTO",
                    "Execução demo interrompida; não foi repetida.",
                    mocked=True,
                )
            ledger.insert_one(
                {"_id": key, "status": "EXECUTING", "requester": requester}
            )
            outcome = self._apply(action, requester)
            ledger.update_one(
                {"_id": key},
                {"$set": {"status": "DONE", "result": outcome.model_dump(mode="json")}},
            )
            return outcome

    def _apply(self, action, requester):
        k, p, op = requester["kitchen_id"], dict(action.parameters), action.operation
        col = self._collection(k)

        def find(kind, identifier):
            d = col.find_one({"_id": f"{k}:{kind}:{identifier}"})
            return d["data"] if d else None

        def save(kind, data):
            col.replace_one(
                {"_id": f"{k}:{kind}:{data['id']}"},
                {
                    "_id": f"{k}:{kind}:{data['id']}",
                    "kitchen": k,
                    "kind": kind,
                    "data": data,
                },
                upsert=True,
            )
            return result(
                True,
                "OK",
                "SIMULAÇÃO executada; nenhum registro da API real foi alterado.",
                data,
                mocked=True,
            )

        def new_id(kind):
            existing = [r["id"] for r in self._rows(k, kind)]
            counter = get_db().mock_counters
            identifier = f"{k}:{kind}"
            counter.update_one(
                {"_id": identifier},
                {"$max": {"value": max(existing, default=0)}},
                upsert=True,
            )
            return counter.find_one_and_update(
                {"_id": identifier},
                {"$inc": {"value": 1}},
                return_document=ReturnDocument.AFTER,
            )["value"]

        def failure(code, text):
            return result(False, code, "SIMULAÇÃO: " + text, mocked=True)

        if op == Operation.CREATE_PRODUCT:
            if not find("unit", p["unitId"]) or (
                p.get("categoryId") and not find("category", p["categoryId"])
            ):
                return failure("NAO_ENCONTRADO", "Unidade/categoria inexistente.")
            if p.get("barcode") and any(
                r.get("barcode") == p["barcode"] for r in self._rows(k, "product")
            ):
                return failure("CONFLITO", "Código de barras duplicado.")
            return save(
                "product",
                dict(
                    p, id=new_id("product"), active=True, unit=find("unit", p["unitId"])
                ),
            )
        if op in {Operation.EDIT_PRODUCT, Operation.DEACTIVATE_PRODUCT}:
            data = find("product", p.pop("productId"))
            if not data:
                return failure("NAO_ENCONTRADO", "Produto inexistente.")
            if op == Operation.DEACTIVATE_PRODUCT:
                data["active"] = False
            else:
                if p.get("unitId") and not find("unit", p["unitId"]):
                    return failure("NAO_ENCONTRADO", "Unidade inexistente.")
                if p.get("barcode") and any(
                    r["id"] != data["id"] and r.get("barcode") == p["barcode"]
                    for r in self._rows(k, "product")
                ):
                    return failure("CONFLITO", "Código de barras duplicado.")
                data.update(p)
            return save("product", data)
        if op == Operation.CREATE_SUPPLIER:
            if any(r["cnpj"] == p["cnpj"] for r in self._rows(k, "supplier")):
                return failure("CONFLITO", "CNPJ duplicado.")
            return save("supplier", dict(p, id=new_id("supplier"), active=True))
        if op == Operation.DEACTIVATE_SUPPLIER:
            data = find("supplier", p["supplierId"])
            if not data:
                return failure("NAO_ENCONTRADO", "Fornecedor inexistente.")
            return save("supplier", dict(data, active=False))
        if op == Operation.CREATE_BATCH:
            product = find("product", p["productId"])
            supplier = (
                find("supplier", p["supplierId"]) if p.get("supplierId") else None
            )
            if (
                not product
                or not product["active"]
                or (p.get("supplierId") and not supplier)
            ):
                return failure("CONFLITO", "Produto/fornecedor não disponível.")
            return save(
                "batch",
                dict(
                    p,
                    id=new_id("batch"),
                    product={"id": product["id"], "name": product["name"]},
                    supplier=supplier,
                    currentQuantity=p["initialQuantity"],
                    status="ACTIVE",
                ),
            )
        if op in {Operation.CONSUME, Operation.RESTOCK, Operation.ADJUST}:
            data = find("batch", p["batchId"])
            if not data:
                return failure("NAO_ENCONTRADO", "Lote inexistente na cozinha.")
            if (
                data.get("expirationDate")
                and data["expirationDate"] < business_date().isoformat()
            ):
                return failure("CONFLITO", "Lote vencido.")
            old = Decimal(str(data["currentQuantity"]))
            quantity = Decimal(p.get("quantity", p.get("newQuantity")))
            if op == Operation.CONSUME and (
                data["status"] != "ACTIVE" or quantity > old
            ):
                return failure("CONFLITO", "Lote não ativo ou saldo insuficiente.")
            if data["status"] in {"EXPIRED", "CANCELLED"}:
                return failure("CONFLITO", "Lote indisponível.")
            new = (
                quantity
                if op == Operation.ADJUST
                else old + quantity
                if op == Operation.RESTOCK
                else old - quantity
            )
            return save(
                "batch",
                dict(
                    data,
                    currentQuantity=float(new),
                    status="ACTIVE" if new > 0 else "WRITTEN_OFF",
                ),
            )
        if op == Operation.CREATE_REQUISITION:
            return save(
                "requisition",
                dict(
                    p,
                    id=new_id("requisition"),
                    status="DRAFT",
                    requester={"id": requester["user_id"]},
                ),
            )
        if op in {
            Operation.ADD_ITEM,
            Operation.REMOVE_ITEM,
            Operation.CANCEL_REQUISITION,
        }:
            req = find("requisition", p["requisitionId"])
            aceitos = (
                {"DRAFT", "UNDER_REVIEW", "APPROVED"}
                if op == Operation.CANCEL_REQUISITION
                else {"DRAFT"}
            )
            if not req or req["status"] not in aceitos:
                return failure(
                    "CONFLITO", "A requisição não está num estado que permita isso."
                )
            if op == Operation.CANCEL_REQUISITION:
                return save(
                    "requisition", dict(req, status="CANCELLED", reason=p["reason"])
                )
            if op == Operation.ADD_ITEM:
                product = find("product", p["productId"])
                if not product or not product["active"]:
                    return failure("NAO_ENCONTRADO", "Produto não disponível.")
                return save(
                    "item",
                    dict(
                        p,
                        id=new_id("item"),
                        product={"id": product["id"], "name": product["name"]},
                        active=True,
                    ),
                )
            item = find("item", p["itemId"])
            if not item or item["requisitionId"] != req["id"]:
                return failure("NAO_ENCONTRADO", "Item não pertence à requisição.")
            return save("item", dict(item, active=False))
        if op == Operation.CREATE_INVENTORY:
            if any(i["status"] == "OPEN" for i in self._rows(k, "inventory")):
                return failure("CONFLITO", "Já existe inventário aberto.")
            return save(
                "inventory",
                dict(
                    p,
                    id=new_id("inventory"),
                    status="OPEN",
                    responsible={"id": requester["user_id"]},
                ),
            )
        if op in {Operation.ADD_COUNT, Operation.EDIT_COUNT}:
            inv, batch = (
                find("inventory", p["inventoryId"]),
                find("batch", p["batchId"]),
            )
            if not inv or inv["status"] != "OPEN" or not batch:
                return failure(
                    "CONFLITO", "Inventário fechado/inexistente ou lote inexistente."
                )
            count = find("count", p["countId"]) if op == Operation.EDIT_COUNT else None
            if op == Operation.EDIT_COUNT and (
                not count
                or count["inventoryId"] != inv["id"]
                or count["batchId"] != batch["id"]
            ):
                return failure(
                    "NAO_ENCONTRADO", "Contagem não pertence ao inventário/lote."
                )
            if op == Operation.ADD_COUNT and any(
                r["inventoryId"] == inv["id"] and r["batchId"] == batch["id"]
                for r in self._rows(k, "count")
            ):
                return failure("CONFLITO", "Lote já contado.")
            registered = (
                count["registeredQuantity"] if count else batch["currentQuantity"]
            )
            return save(
                "count",
                dict(
                    p,
                    id=count["id"] if count else new_id("count"),
                    registeredQuantity=registered,
                    divergence=float(
                        Decimal(p["physicalQuantity"]) - Decimal(str(registered))
                    ),
                ),
            )
        return failure("NAO_IMPLEMENTADO", "Operação não disponível.")


def gateway() -> BusinessGateway:
    return MockGateway() if BUSINESS_MODE == "mock" else SpringGateway()
