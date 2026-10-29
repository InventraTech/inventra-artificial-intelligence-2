"""Spring de mentira, fiel ao contrato real (OpenAPI de ms-inventra-api, 102 rotas).

Não é um mock de "tudo certo": valida os corpos contra os schemas reais (obrigatórios, tipos,
enums, propriedades desconhecidas), devolve as formas reais de resposta (listas simples, 204 sem
corpo, DTOs com `product`/`kitchen` resumidos, campos nulos) e mantém estado, como o banco:
baixa só em lote ativo e com saldo, lote zerado vira WRITTEN_OFF, movimentações gravadas etc.
"""

import re
from datetime import date, timedelta

import httpx

KITCHEN = 7
USER_ID = "0b6f7a52-52c4-4a46-9d5e-6f3a7b9c1d10"

# nome -> (obrigatórios, {propriedade: tipo}). Copiado do OpenAPI real.
SCHEMAS = {
    "RestockRequest": (["quantity"], {"quantity": "number"}),
    "ConsumeStockRequest": (
        ["quantity"],
        {
            "quantity": "number",
            "reason": "enum:USAGE|LOSS|EXPIRED|REQUISITION",
        },
    ),
    "AdjustStockRequest": (["newQuantity"], {"newQuantity": "number"}),
    "RegisterStockEntryRequest": (
        ["batchNumber", "entryDate", "initialQuantity", "kitchenId", "productId"],
        {
            "batchNumber": "string:50",
            "entryDate": "date",
            "expirationDate": "date",
            "initialQuantity": "number",
            "invoiceNumber": "string:50",
            "kitchenId": "integer",
            "productId": "integer",
            "supplierId": "integer",
            "unitPrice": "number",
        },
    ),
    "ConsumeProductRequest": (
        ["kitchenId", "productId", "quantity"],
        {
            "kitchenId": "integer",
            "productId": "integer",
            "quantity": "number",
            "reason": "enum:USAGE|LOSS|EXPIRED|REQUISITION",
        },
    ),
    "CreateProductRequest": (
        ["name", "unitId"],
        {
            "name": "string:150",
            "unitId": "integer",
            "brand": "string:80",
            "categoryId": "integer",
            "barcode": "string:50",
        },
    ),
    "UpdateProductRequest": (
        [],
        {
            "name": "string:150",
            "unitId": "integer",
            "brand": "string:80",
            "categoryId": "integer",
            "barcode": "string:50",
        },
    ),
    "CreateSupplierRequest": (
        ["cnpj", "legalName"],
        {
            "cnpj": "string:18",
            "legalName": "string:150",
            "email": "string:150",
            "whatsapp": "string:20",
            "rating": "integer",
        },
    ),
    "CreateRequisitionRequest": (
        ["kitchenId", "origin", "type"],
        {
            "kitchenId": "integer",
            "origin": "string:20",
            "type": "enum:PURCHASE|TRANSFER|CONSUMPTION",
        },
    ),
    "AddRequisitionItemRequest": (
        ["productId", "quantity"],
        {
            "productId": "integer",
            "quantity": "number",
            "suggestedSupplierId": "integer",
            "estimatedPrice": "number",
            "note": "string:255",
        },
    ),
    "CancelRequisitionRequest": (["reason"], {"reason": "string:255"}),
    "OpenInventoryRequest": (
        ["kitchenId"],
        {"kitchenId": "integer", "responsibleId": "uuid", "note": "string:255"},
    ),
    "RegisterInventoryCountRequest": (
        ["batchId", "physicalQuantity"],
        {"batchId": "integer", "physicalQuantity": "number", "note": "string:255"},
    ),
}


def violations(name, body):
    required, props = SCHEMAS[name]
    if not isinstance(body, dict):
        return ["corpo ausente"]
    erros = [f"falta {k}" for k in required if body.get(k) is None]
    for key, value in body.items():
        if key not in props:
            erros.append(f"propriedade desconhecida {key}")
            continue
        kind = props[key]
        ok = True
        if kind == "integer":
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif kind == "number":
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif kind.startswith("string"):
            limit = int(kind.split(":")[1])
            ok = isinstance(value, str) and 0 < len(value) <= limit
        elif kind == "date":
            ok = isinstance(value, str) and bool(
                re.fullmatch(r"\d{4}-\d\d-\d\d", value)
            )
        elif kind == "uuid":
            ok = isinstance(value, str) and len(value) == 36
        elif kind.startswith("enum:"):
            ok = value in kind[5:].split("|")
        if not ok:
            erros.append(f"{key} inválido: {value!r}")
    return erros


class FakeSpring:
    """Chame como `httpx.request`. `omit_nulls` simula DTOs sem os campos nulos."""

    def __init__(self, today=None, omit_nulls=False):
        self.today = today or date.today()
        self.omit_nulls = omit_nulls
        self.calls = []
        self.seq = {"batch": 100, "req": 100, "item": 100, "inv": 100, "count": 100}
        self.seq.update(prod=100, sup=100, mov=100)
        self.units = {1: "kg", 2: "L", 3: "un", 4: "g"}
        self.categories = {1: "Grãos", 2: "Hortifruti"}
        self.products = {
            1: {"name": "Arroz", "unit": 1, "cat": 1, "active": True},
            2: {"name": "Abóbora cabotiá", "unit": 1, "cat": 2, "active": True},
            3: {"name": "Leite", "unit": 2, "cat": 1, "active": True},
            4: {"name": "Ovos", "unit": 3, "cat": 1, "active": True},
        }
        self.suppliers = {
            1: {"legalName": "Fornecedor X", "cnpj": "11222333000181", "active": True}
        }
        d = self.today
        self.batches = {
            1: self._b(1, 1, "L-A1", 50, 30, d - timedelta(30), d + timedelta(60)),
            2: self._b(2, 2, "L-B1", 20, 8, d - timedelta(10), d + timedelta(5)),
            3: self._b(3, 2, "L-B2", 30, 30, d - timedelta(3), d + timedelta(20)),
            4: self._b(4, 3, "L-C1", 12, 12, d - timedelta(2), d + timedelta(10)),
            5: self._b(
                5, 1, "L-A0", 10, 5, d - timedelta(60), d - timedelta(2), "EXPIRED"
            ),
        }
        self.params = {1: {"min": 40.0}, 2: {"min": 50.0}, 3: {"min": 5.0}}
        self.requisitions = {
            1: {"type": "PURCHASE", "origin": "APP", "status": "DRAFT", "reason": None}
        }
        self.items = {1: {"req": 1, "product": 3, "quantity": 6.0}}
        self.inventories = {}
        self.counts = {}
        self.movements = []
        self.alerts = []

    # ----------------------------------------------------------------- dados
    def _b(self, bid, product, number, initial, current, entry, exp, status="ACTIVE"):
        return {
            "id": bid,
            "product": product,
            "number": number,
            "initial": float(initial),
            "current": float(current),
            "entry": entry,
            "exp": exp,
            "status": status,
            "supplier": None,
            "unit_price": None,
        }

    def _clean(self, value):
        if isinstance(value, dict):
            return {
                k: self._clean(v)
                for k, v in value.items()
                if not (self.omit_nulls and v is None)
            }
        if isinstance(value, list):
            return [self._clean(v) for v in value]
        return value

    def _resp(self, status, body=None):
        if body is None:
            return httpx.Response(status)
        return httpx.Response(status, json=self._clean(body))

    def _err(self, status, message="erro"):
        return httpx.Response(status, json={"message": message})

    def psum(self, pid):
        p = self.products[pid]
        return {"id": pid, "name": p["name"], "photoUrl": None}

    def ksum(self):
        return {"id": KITCHEN, "name": "Cozinha Teste"}

    def usum(self, uid):
        return {"id": uid, "symbol": self.units[uid]}

    def batch_dto(self, b):
        sup = self.suppliers.get(b["supplier"]) if b["supplier"] else None
        return {
            "id": b["id"],
            "product": self.psum(b["product"]),
            "kitchen": self.ksum(),
            "supplier": {"id": b["supplier"], "legalName": sup["legalName"]}
            if sup
            else None,
            "batchNumber": b["number"],
            "invoiceNumber": None,
            "initialQuantity": b["initial"],
            "currentQuantity": b["current"],
            "entryDate": b["entry"].isoformat(),
            "expirationDate": b["exp"].isoformat() if b["exp"] else None,
            "unitPrice": b["unit_price"],
            "status": b["status"],
        }

    def product_dto(self, pid):
        p = self.products[pid]
        return {
            "id": pid,
            "name": p["name"],
            "brand": None,
            "category": {"id": p["cat"], "name": self.categories[p["cat"]]},
            "unit": self.usum(p["unit"]),
            "barcode": None,
            "photoUrl": None,
            "active": p["active"],
            "createdAt": "2026-01-01T10:00:00",
        }

    def supplier_dto(self, sid):
        s = self.suppliers[sid]
        return {
            "id": sid,
            "legalName": s["legalName"],
            "cnpj": s["cnpj"],
            "email": None,
            "whatsapp": None,
            "rating": None,
            "active": s["active"],
            "createdAt": "2026-01-01T10:00:00",
        }

    def req_dto(self, rid):
        r = self.requisitions[rid]
        items = [i for i in self.items.values() if i["req"] == rid]
        return {
            "id": rid,
            "type": r["type"],
            "origin": r["origin"],
            "status": r["status"],
            "reason": r["reason"],
            "kitchen": self.ksum(),
            "requester": {"id": USER_ID, "name": "Davi", "photoUrl": None},
            "approver": None,
            "createdAt": "2026-10-01T10:00:00",
            "approvedAt": None,
            "itemCount": len(items),
            "estimatedTotal": 0.0,
            "itemNames": [self.products[i["product"]]["name"] for i in items],
        }

    def item_dto(self, iid):
        i = self.items[iid]
        return {
            "id": iid,
            "product": self.psum(i["product"]),
            "quantity": i["quantity"],
            "estimatedPrice": None,
            "suggestedSupplier": None,
            "note": None,
        }

    def inv_dto(self, iid):
        v = self.inventories[iid]
        return {
            "id": iid,
            "kitchen": self.ksum(),
            "responsible": {"id": USER_ID, "name": "Davi", "photoUrl": None},
            "startedAt": "2026-10-28T10:00:00",
            "closedAt": None,
            "status": v["status"],
            "note": v["note"],
        }

    def count_dto(self, cid):
        c = self.counts[cid]
        b = self.batches[c["batch"]]
        return {
            "id": cid,
            "inventoryId": c["inv"],
            "batch": {
                "id": b["id"],
                "batchNumber": b["number"],
                "expirationDate": b["exp"].isoformat() if b["exp"] else None,
            },
            "registeredQuantity": c["registered"],
            "physicalQuantity": c["physical"],
            "divergence": c["physical"] - c["registered"],
            "note": c["note"],
        }

    def move(self, batch, kind, qty, reason=None):
        self.seq["mov"] += 1
        self.movements.append(
            {
                "id": self.seq["mov"],
                "type": kind,
                "reason": reason,
                "quantity": qty,
                "balance": batch["current"],
                "batch": batch["id"],
                "product": batch["product"],
            }
        )

    # --------------------------------------------------------------- roteador
    def __call__(self, method, url, params=None, json=None, headers=None, **_):
        path = re.sub(r"^https?://[^/]+", "", url)
        self.calls.append((method, path, params, json))
        if not (headers or {}).get("Authorization", "").startswith("Bearer "):
            return self._err(401)
        params = params or {}
        for (verb, pattern), handler in ROUTES:
            found = re.fullmatch(pattern, path)
            if verb == method and found:
                return handler(self, found, params, json)
        return self._err(404, f"sem rota {method} {path}")

    # ------------------------------------------------------------- handlers
    def get_products(self, m, q, body):
        name = (q.get("name") or "").lower()
        rows = [
            self.product_dto(pid)
            for pid, p in self.products.items()
            if name in p["name"].lower()
        ]
        return self._resp(
            200,
            {
                "content": rows,
                "totalElements": len(rows),
                "totalPages": 1,
                "number": 0,
                "size": len(rows),
            },
        )

    def get_product(self, m, q, body):
        pid = int(m.group(1))
        if pid not in self.products:
            return self._err(404)
        return self._resp(200, self.product_dto(pid))

    def post_product(self, m, q, body):
        bad = violations("CreateProductRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if body["unitId"] not in self.units:
            return self._err(404, "unidade")
        self.seq["prod"] += 1
        pid = self.seq["prod"]
        self.products[pid] = {
            "name": body["name"],
            "unit": body["unitId"],
            "cat": body.get("categoryId", 1),
            "active": True,
        }
        return self._resp(201, self.product_dto(pid))

    def put_product(self, m, q, body):
        pid = int(m.group(1))
        bad = violations("UpdateProductRequest", body)
        if bad or pid not in self.products:
            return self._err(400 if bad else 404, "; ".join(bad))
        p = self.products[pid]
        if "name" in body:
            p["name"] = body["name"]
        if "unitId" in body:
            p["unit"] = body["unitId"]
        return self._resp(200, self.product_dto(pid))

    @staticmethod
    def flag(kind, value):
        def handler(self, m, q, body):
            table = self.products if kind == "product" else self.suppliers
            ident = int(m.group(1))
            if ident not in table:
                return self._err(404)
            table[ident]["active"] = value
            return self._resp(204)

        return handler

    def get_suppliers(self, m, q, body):
        return self._resp(200, [self.supplier_dto(s) for s in self.suppliers])

    def get_supplier(self, m, q, body):
        sid = int(m.group(1))
        if sid not in self.suppliers:
            return self._err(404)
        return self._resp(200, self.supplier_dto(sid))

    def post_supplier(self, m, q, body):
        bad = violations("CreateSupplierRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if any(
            s["cnpj"] == re.sub(r"\D", "", body["cnpj"])
            for s in self.suppliers.values()
        ):
            return self._err(409, "CNPJ já cadastrado")
        self.seq["sup"] += 1
        sid = self.seq["sup"]
        self.suppliers[sid] = {
            "legalName": body["legalName"],
            "cnpj": re.sub(r"\D", "", body["cnpj"]),
            "active": True,
        }
        return self._resp(201, self.supplier_dto(sid))

    def get_batches(self, m, q, body):
        rows = [
            self.batch_dto(b)
            for b in self.batches.values()
            if (not q.get("productId") or b["product"] == int(q["productId"]))
        ]
        return self._resp(200, rows)

    def expiring(self, m, q, body):
        limit = self.today + timedelta(days=int(q["days"]))
        rows = [
            self.batch_dto(b)
            for b in self.batches.values()
            if b["status"] == "ACTIVE" and b["exp"] and b["exp"] <= limit
        ]
        return self._resp(200, rows)

    def total(self, pid):
        return sum(
            b["current"]
            for b in self.batches.values()
            if b["product"] == pid
            and b["status"] == "ACTIVE"
            and (not b["exp"] or b["exp"] >= self.today)
        )

    def low_stock(self, m, q, body):
        rows = []
        for pid, par in self.params.items():
            if self.total(pid) < par["min"]:
                rows.append(
                    {
                        "kitchenId": KITCHEN,
                        "productId": pid,
                        "product": self.psum(pid),
                        "currentQuantity": self.total(pid),
                        "minStock": par["min"],
                    }
                )
        return self._resp(200, rows)

    def kitchen_stock(self, m, q, body):
        rows = []
        for pid, p in self.products.items():
            lots = [
                b
                for b in self.batches.values()
                if b["product"] == pid and b["status"] == "ACTIVE"
            ]
            if not lots:
                continue
            minimo = self.params.get(pid, {}).get("min")
            exps = sorted(b["exp"] for b in lots if b["exp"])
            rows.append(
                {
                    "product": self.psum(pid),
                    "category": {"id": p["cat"], "name": self.categories[p["cat"]]},
                    "unit": self.usum(p["unit"]),
                    "totalQuantity": self.total(pid),
                    "activeBatches": len(lots),
                    "nextExpirationDate": exps[0].isoformat() if exps else None,
                    "minStock": minimo,
                    "lowStock": minimo is not None and self.total(pid) < minimo,
                }
            )
        return self._resp(200, rows)

    def kitchen_parameters(self, m, q, body):
        pid = int(m.group(1))
        if pid not in self.products:
            return self._err(404)
        par = self.params.get(pid, {})
        return self._resp(
            200,
            {
                "kitchen": self.ksum(),
                "minStock": par.get("min"),
                "maxStock": None,
                "averageDailyConsumption": None,
                "currentQuantity": self.total(pid),
                "estimatedDaysUntilStockout": None,
                "lowStock": bool(par) and self.total(pid) < par["min"],
                "overStock": False,
            },
        )

    def product_suppliers(self, m, q, body):
        return self._resp(
            200,
            [
                {
                    "supplier": {"id": 1, "legalName": "Fornecedor X"},
                    "supplierCode": None,
                    "referencePrice": 9.5,
                    "leadTimeDays": 2,
                }
            ],
        )

    def get_movements(self, m, q, body):
        rows = [
            {
                "id": mv["id"],
                "type": mv["type"],
                "reason": mv["reason"],
                "quantity": mv["quantity"],
                "balanceAfter": mv["balance"],
                "batch": {
                    "id": mv["batch"],
                    "batchNumber": self.batches[mv["batch"]]["number"],
                },
                "product": self.psum(mv["product"]),
                "user": {"id": USER_ID, "name": "Davi", "photoUrl": None},
                "requisitionId": None,
                "createdAt": "2026-10-28T10:00:00",
            }
            for mv in self.movements
            if (not q.get("type") or mv["type"] == q["type"])
            and (not q.get("productId") or mv["product"] == int(q["productId"]))
        ]
        return self._resp(200, rows)

    def post_batch(self, m, q, body):
        bad = violations("RegisterStockEntryRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if body["productId"] not in self.products:
            return self._err(404, "produto")
        if body["initialQuantity"] <= 0:
            return self._err(400, "quantidade")
        self.seq["batch"] += 1
        bid = self.seq["batch"]
        exp = (
            date.fromisoformat(body["expirationDate"])
            if body.get("expirationDate")
            else None
        )
        self.batches[bid] = self._b(
            bid,
            body["productId"],
            body["batchNumber"],
            body["initialQuantity"],
            body["initialQuantity"],
            date.fromisoformat(body["entryDate"]),
            exp,
        )
        self.move(self.batches[bid], "ENTRY", float(body["initialQuantity"]))
        return self._resp(201, self.batch_dto(self.batches[bid]))

    @staticmethod
    def batch_action(kind):
        schema = {
            "restock": "RestockRequest",
            "consume": "ConsumeStockRequest",
            "adjust": "AdjustStockRequest",
        }[kind]

        def handler(self, m, q, body):
            bid = int(m.group(1))
            bad = violations(schema, body)
            if bad:
                return self._err(400, "; ".join(bad))
            b = self.batches.get(bid)
            if b is None:
                return self._err(404)
            if kind == "restock":
                if b["status"] in ("EXPIRED", "CANCELLED") or body["quantity"] <= 0:
                    return self._err(409, "lote não aceita entrada")
                b["current"] += body["quantity"]
                b["status"] = "ACTIVE"
                self.move(b, "ENTRY", float(body["quantity"]))
            elif kind == "consume":
                if b["status"] != "ACTIVE":
                    return self._err(409, "só lote ativo")
                if body["quantity"] <= 0 or b["current"] < body["quantity"]:
                    return self._err(409, "saldo insuficiente")
                b["current"] -= body["quantity"]
                if b["current"] == 0:
                    b["status"] = "WRITTEN_OFF"
                self.move(
                    b, "WRITE_OFF", float(body["quantity"]), body.get("reason", "USAGE")
                )
            else:
                if body["newQuantity"] < 0:
                    return self._err(400, "negativa")
                diff = body["newQuantity"] - b["current"]
                b["current"] = float(body["newQuantity"])
                self.move(b, "ADJUSTMENT", diff)
            return self._resp(200, self.batch_dto(b))

        return handler

    def consume_product(self, m, q, body):
        bad = violations("ConsumeProductRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        lots = sorted(
            (
                b
                for b in self.batches.values()
                if b["product"] == body["productId"] and b["status"] == "ACTIVE"
            ),
            key=lambda b: (b["exp"] or date.max, b["entry"], b["id"]),
        )
        if sum(b["current"] for b in lots) < body["quantity"]:
            return self._err(409, "saldo insuficiente")
        restante, out = float(body["quantity"]), []
        for b in lots:
            if restante <= 0:
                break
            tirar = min(b["current"], restante)
            b["current"] -= tirar
            restante -= tirar
            if b["current"] == 0:
                b["status"] = "WRITTEN_OFF"
            self.move(b, "WRITE_OFF", tirar, body.get("reason", "USAGE"))
            out.append(
                {
                    "batch": {
                        "id": b["id"],
                        "batchNumber": b["number"],
                        "expirationDate": b["exp"].isoformat() if b["exp"] else None,
                    },
                    "consumedQuantity": tirar,
                    "remainingQuantity": b["current"],
                    "status": b["status"],
                }
            )
        return self._resp(200, out)

    def get_requisitions(self, m, q, body):
        rows = [
            self.req_dto(r)
            for r, v in self.requisitions.items()
            if (not q.get("status") or v["status"] == q["status"])
        ]
        return self._resp(200, rows)

    def get_requisition(self, m, q, body):
        rid = int(m.group(1))
        if rid not in self.requisitions:
            return self._err(404)
        return self._resp(200, self.req_dto(rid))

    def req_items(self, m, q, body):
        rid = int(m.group(1))
        if rid not in self.requisitions:
            return self._err(404)
        return self._resp(
            200, [self.item_dto(i) for i, v in self.items.items() if v["req"] == rid]
        )

    def post_requisition(self, m, q, body):
        bad = violations("CreateRequisitionRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        self.seq["req"] += 1
        rid = self.seq["req"]
        self.requisitions[rid] = {
            "type": body["type"],
            "origin": body["origin"],
            "status": "DRAFT",
            "reason": None,
        }
        return self._resp(201, self.req_dto(rid))

    def add_item(self, m, q, body):
        rid = int(m.group(1))
        bad = violations("AddRequisitionItemRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if rid not in self.requisitions:
            return self._err(404)
        if self.requisitions[rid]["status"] != "DRAFT":
            return self._err(409, "só rascunho")
        self.seq["item"] += 1
        self.items[self.seq["item"]] = {
            "req": rid,
            "product": body["productId"],
            "quantity": float(body["quantity"]),
        }
        return self._resp(200, self.req_dto(rid))

    def remove_item(self, m, q, body):
        rid, iid = int(m.group(1)), int(m.group(2))
        if rid not in self.requisitions or iid not in self.items:
            return self._err(404)
        if self.requisitions[rid]["status"] != "DRAFT":
            return self._err(409, "só rascunho")
        del self.items[iid]
        return self._resp(200, self.req_dto(rid))

    def cancel_requisition(self, m, q, body):
        rid = int(m.group(1))
        bad = violations("CancelRequisitionRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if rid not in self.requisitions:
            return self._err(404)
        if self.requisitions[rid]["status"] not in (
            "DRAFT",
            "UNDER_REVIEW",
            "APPROVED",
        ):
            return self._err(409, "não cancelável")
        self.requisitions[rid].update(status="CANCELLED", reason=body["reason"])
        return self._resp(200, self.req_dto(rid))

    def get_inventories(self, m, q, body):
        return self._resp(200, [self.inv_dto(i) for i in self.inventories])

    def get_inventory(self, m, q, body):
        iid = int(m.group(1))
        if iid not in self.inventories:
            return self._err(404)
        return self._resp(200, self.inv_dto(iid))

    def post_inventory(self, m, q, body):
        bad = violations("OpenInventoryRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if any(v["status"] == "OPEN" for v in self.inventories.values()):
            return self._err(409, "já existe aberto")
        self.seq["inv"] += 1
        iid = self.seq["inv"]
        self.inventories[iid] = {"status": "OPEN", "note": body.get("note")}
        return self._resp(201, self.inv_dto(iid))

    def get_counts(self, m, q, body):
        iid = int(m.group(1))
        if iid not in self.inventories:
            return self._err(404)
        return self._resp(
            200, [self.count_dto(c) for c, v in self.counts.items() if v["inv"] == iid]
        )

    def post_count(self, m, q, body):
        iid = int(m.group(1))
        bad = violations("RegisterInventoryCountRequest", body)
        if bad:
            return self._err(400, "; ".join(bad))
        if iid not in self.inventories or body["batchId"] not in self.batches:
            return self._err(404)
        if self.inventories[iid]["status"] != "OPEN":
            return self._err(409, "inventário fechado")
        if any(
            c["inv"] == iid and c["batch"] == body["batchId"]
            for c in self.counts.values()
        ):
            return self._err(409, "lote já contado neste inventário")
        self.seq["count"] += 1
        cid = self.seq["count"]
        self.counts[cid] = {
            "inv": iid,
            "batch": body["batchId"],
            "registered": self.batches[body["batchId"]]["current"],
            "physical": float(body["physicalQuantity"]),
            "note": body.get("note"),
        }
        return self._resp(201, self.count_dto(cid))

    def delete_count(self, m, q, body):
        iid, cid = int(m.group(1)), int(m.group(2))
        if iid not in self.inventories or cid not in self.counts:
            return self._err(404)
        if self.inventories[iid]["status"] != "OPEN":
            return self._err(409, "inventário fechado")
        del self.counts[cid]
        return self._resp(200, self.inv_dto(iid))

    def get_alerts(self, m, q, body):
        return self._resp(200, self.alerts)

    def get_units(self, m, q, body):
        return self._resp(
            200,
            [{"id": i, "symbol": s, "description": s} for i, s in self.units.items()],
        )

    def get_unit(self, m, q, body):
        uid = int(m.group(1))
        if uid not in self.units:
            return self._err(404)
        return self._resp(
            200, {"id": uid, "symbol": self.units[uid], "description": self.units[uid]}
        )

    def get_categories(self, m, q, body):
        return self._resp(
            200,
            [
                {"id": i, "name": n, "description": None}
                for i, n in self.categories.items()
            ],
        )

    def get_category(self, m, q, body):
        cid = int(m.group(1))
        if cid not in self.categories:
            return self._err(404)
        return self._resp(
            200, {"id": cid, "name": self.categories[cid], "description": None}
        )

    def get_kitchens(self, m, q, body):
        return self._resp(
            200,
            [
                {
                    "id": KITCHEN,
                    "name": "Cozinha Teste",
                    "code": "CT",
                    "address": None,
                    "city": None,
                    "state": None,
                    "latitude": None,
                    "longitude": None,
                    "acceptingRequests": True,
                    "active": True,
                    "createdAt": "2026-01-01T10:00:00",
                }
            ],
        )


ROUTES = [
    (("GET", r"/api/products"), FakeSpring.get_products),
    (("GET", r"/api/products/(\d+)"), FakeSpring.get_product),
    (("POST", r"/api/products"), FakeSpring.post_product),
    (("PUT", r"/api/products/(\d+)"), FakeSpring.put_product),
    (
        ("PATCH", r"/api/products/(\d+)/deactivate"),
        FakeSpring.flag("product", False),
    ),
    (("GET", r"/api/products/(\d+)/kitchen-parameters"), FakeSpring.kitchen_parameters),
    (("GET", r"/api/products/(\d+)/suppliers"), FakeSpring.product_suppliers),
    (("GET", r"/api/suppliers"), FakeSpring.get_suppliers),
    (("GET", r"/api/suppliers/(\d+)"), FakeSpring.get_supplier),
    (("POST", r"/api/suppliers"), FakeSpring.post_supplier),
    (
        ("PATCH", r"/api/suppliers/(\d+)/deactivate"),
        FakeSpring.flag("supplier", False),
    ),
    (("GET", r"/api/stock-batches"), FakeSpring.get_batches),
    (("GET", r"/api/stock-batches/expiring-soon"), FakeSpring.expiring),
    (("GET", r"/api/stock-batches/low-stock"), FakeSpring.low_stock),
    (("POST", r"/api/stock-batches"), FakeSpring.post_batch),
    (("POST", r"/api/stock-batches/consume"), FakeSpring.consume_product),
    (
        ("PATCH", r"/api/stock-batches/(\d+)/restock"),
        FakeSpring.batch_action("restock"),
    ),
    (
        ("PATCH", r"/api/stock-batches/(\d+)/consume"),
        FakeSpring.batch_action("consume"),
    ),
    (
        ("PATCH", r"/api/stock-batches/(\d+)/adjust"),
        FakeSpring.batch_action("adjust"),
    ),
    (("GET", r"/api/stock-movements"), FakeSpring.get_movements),
    (("GET", rf"/api/kitchens/{KITCHEN}/stock"), FakeSpring.kitchen_stock),
    (("GET", r"/api/kitchens"), FakeSpring.get_kitchens),
    (("GET", r"/api/requisitions"), FakeSpring.get_requisitions),
    (("GET", r"/api/requisitions/(\d+)"), FakeSpring.get_requisition),
    (("GET", r"/api/requisitions/(\d+)/items"), FakeSpring.req_items),
    (("POST", r"/api/requisitions"), FakeSpring.post_requisition),
    (("POST", r"/api/requisitions/(\d+)/items"), FakeSpring.add_item),
    (("DELETE", r"/api/requisitions/(\d+)/items/(\d+)"), FakeSpring.remove_item),
    (("PATCH", r"/api/requisitions/(\d+)/cancel"), FakeSpring.cancel_requisition),
    (("GET", r"/api/inventories"), FakeSpring.get_inventories),
    (("GET", r"/api/inventories/(\d+)"), FakeSpring.get_inventory),
    (("POST", r"/api/inventories"), FakeSpring.post_inventory),
    (("GET", r"/api/inventories/(\d+)/counts"), FakeSpring.get_counts),
    (("POST", r"/api/inventories/(\d+)/counts"), FakeSpring.post_count),
    (("DELETE", r"/api/inventories/(\d+)/counts/(\d+)"), FakeSpring.delete_count),
    (("GET", r"/api/alerts"), FakeSpring.get_alerts),
    (("GET", r"/api/units"), FakeSpring.get_units),
    (("GET", r"/api/units/(\d+)"), FakeSpring.get_unit),
    (("GET", r"/api/categories"), FakeSpring.get_categories),
    (("GET", r"/api/categories/(\d+)"), FakeSpring.get_category),
]
