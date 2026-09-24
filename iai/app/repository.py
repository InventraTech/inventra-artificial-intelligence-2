"""SQL identifiers are code-owned; all user values are bound parameters."""
from datetime import timedelta
from typing import Any

from psycopg2 import sql

from iai.app.domain import DomainError, ReadQuery

# (view, stable ordering, optional date column). Global catalog data has no kitchen.
VIEWS = {
    "lotes": ("vw_stock_batch_detail", "expiration_date NULLS LAST, id_batch", "expiration_date"),
    "pre_lista": ("vw_stock_batch_detail", "expiration_date NULLS LAST, id_batch", "expiration_date"),
    "estoque": ("vw_product_stock_position", "id_product", "nearest_expiration_date"),
    "catalogo": ("vw_product_supplier_catalog", "id_product, id_supplier", None),
    "fornecedores": ("vw_supplier_profile", "id_supplier", None),
    "validade_diaria": ("vw_daily_expiration_summary", "expiration_date", "expiration_date"),
    "alertas": ("vw_active_alerts", "severity_rank, id_alert", "created_at"),
    "valor_categoria": ("vw_stock_value_by_category", "id_category NULLS LAST", None),
    "requisicoes": ("vw_requisition_summary", "created_at DESC, id_requisition", "created_at"),
    "requisicoes_pendentes": ("vw_requisition_pending", "created_at, id_requisition", "created_at"),
    "movimentacoes": ("vw_stock_movement_log", "operation_date DESC, id_log", "operation_date"),
    "divergencias": ("vw_inventory_count_divergence", "id_count", "started_at"),
    "movimentacao_diaria": ("vw_kitchen_daily_stock_movement", "movement_date DESC", "movement_date"),
    "ranking_requisicoes": ("vw_product_requisition_ranking", "demand_rank, id_product", None),
    "painel": ("vw_kitchen_dashboard_kpi", "id_kitchen", None),
    "abaixo_minimo": ("vw_products_below_minimum", "id_product", "nearest_expiration_date"),
    "lotes_atencao": ("vw_batches_needing_attention", "expiration_date, id_batch", "expiration_date"),
    "desperdicio": ("vw_monthly_waste_proxy_kpi", "reference_month DESC", "reference_month"),
    "urgencia": ("vw_product_expiration_urgency", "urgency_rank, expiration_date, id_batch", "expiration_date"),
}

PRODUCTS = """SELECT p.id_product, p.name AS product_name, p.brand AS product_brand,
 p.active, c.name AS category_name, mu.symbol AS unit_symbol
 FROM public.tb_product p LEFT JOIN public.tb_category c USING (id_category)
 JOIN public.tb_measurement_unit mu USING (id_unit)"""
ITEMS = """SELECT ri.id_requisition_item, ri.id_requisition, r.id_kitchen,
 ri.id_product, p.name AS product_name, mu.symbol AS unit_symbol,
 ri.id_suggested_supplier AS id_supplier, s.legal_name AS supplier_name,
 ri.quantity, ri.estimated_price, ri.note
 FROM public.tb_requisition_item ri JOIN public.tb_requisition r USING (id_requisition)
 JOIN public.tb_product p USING (id_product)
 JOIN public.tb_measurement_unit mu USING (id_unit)
 LEFT JOIN public.tb_supplier s ON s.id_supplier=ri.id_suggested_supplier"""

# The global category views omit kitchen, so cannot safely serve a kitchen-scoped user.
CATEGORY_BALANCE = """SELECT c.id_category, c.name AS category_name, v.id_kitchen,
 sum(v.total_current_quantity) AS total_atual, sum(v.min_stock) AS total_minimo,
 sum(v.total_current_quantity-v.min_stock) AS folga
 FROM public.vw_product_stock_position v JOIN public.tb_product p USING (id_product)
 JOIN public.tb_category c USING (id_category)
 GROUP BY c.id_category,c.name,v.id_kitchen"""
CATEGORY_TREND = """SELECT r.id_kitchen,c.id_category,c.name AS category_name,
 date_trunc('month',r.created_at)::date AS reference_month,
 sum(ri.quantity) AS quantity_requested
 FROM public.tb_requisition r JOIN public.tb_requisition_item ri USING (id_requisition)
 JOIN public.tb_product p USING (id_product) JOIN public.tb_category c USING (id_category)
 WHERE r.status IN ('UNDER_REVIEW','APPROVED')
 GROUP BY r.id_kitchen,c.id_category,c.name,date_trunc('month',r.created_at)::date"""


class Repository:
    def __init__(self, conn: Any):
        self.conn = conn

    def rows(self, statement: Any, params: tuple = ()) -> list[dict]:
        with self.conn.cursor() as cur:
            cur.execute(statement, params)
            return [dict(row) for row in cur.fetchall()]

    def one(self, statement: Any, params: tuple = ()) -> dict | None:
        rows = self.rows(statement, params)
        return rows[0] if rows else None

    def principal(self, user_id: str, lock: bool = False) -> dict | None:
        return self.one("""SELECT u.id_user::text AS user_id,u.id_kitchen AS kitchen_id,
          p.access_type,u.active,k.active AS kitchen_active,k.name AS kitchen_name
          FROM public.tb_user u JOIN public.tb_profile p USING (id_profile)
          JOIN public.tb_kitchen k USING (id_kitchen) WHERE u.id_user=%s"""
          + (" FOR SHARE OF u,p,k" if lock else ""), (user_id,))

    def today(self):
        return self.one("SELECT CURRENT_DATE AS today")["today"]

    def read(self, query: ReadQuery, kitchen_id: int) -> list[dict]:
        resource = query.resource
        if resource in VIEWS:
            view, order, date_column = VIEWS[resource]
            source = sql.SQL("SELECT * FROM public.{}").format(sql.Identifier(view))
        else:
            source_text, order, date_column = {
                "produtos": (PRODUCTS, "id_product", None),
                "itens_requisicao": (ITEMS, "id_requisition_item", None),
                "saldo_categoria": (CATEGORY_BALANCE, "folga, id_category", None),
                "tendencia_categoria": (CATEGORY_TREND, "reference_month DESC, id_category", "reference_month"),
            }[resource]
            source = sql.SQL(source_text)
        # Introspect code-owned SELECT metadata, never identifiers supplied by the model.
        with self.conn.cursor() as cur:
            cur.execute(sql.SQL("SELECT * FROM ({}) q LIMIT 0").format(source))
            columns = {d.name for d in cur.description}
        clauses: list[Any] = []
        params: list[Any] = []

        def add(column: str, operator: str, value: Any):
            if column not in columns:
                raise DomainError("INVALID_PARAMETER", f"Filtro {column} não se aplica a {resource}.")
            clauses.append(sql.SQL("q.{} " + operator + " %s").format(sql.Identifier(column)))
            params.append(value)

        if "id_kitchen" in columns:
            add("id_kitchen", "=", kitchen_id)
        elif resource not in {"produtos", "catalogo", "fornecedores"}:
            raise DomainError("FORBIDDEN", "A consulta não permite isolamento por cozinha.")
        for field, column in {"product_id": "id_product", "supplier_id": "id_supplier",
                              "batch_id": "id_batch", "requisition_id": "id_requisition"}.items():
            value = getattr(query, field)
            if value is not None:
                add(column, "=", value)
        for field, column in {"product": "product_name", "supplier": "supplier_name",
                              "category": "category_name"}.items():
            value = getattr(query, field)
            if value is not None:
                # Escape LIKE wildcards as well as parameterizing the complete value.
                value = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                add(column, "ILIKE", f"%{value}%")
        if query.status:
            add("stock_status" if "stock_status" in columns else "status", "=", query.status)
        if resource == "pre_lista":
            if query.status and query.status != "ACTIVE":
                raise DomainError("INVALID_PARAMETER", "A pré-lista contém apenas lotes ACTIVE.")
            add("status", "=", "ACTIVE")
        if query.expiration:
            add("expiration_status", "=", query.expiration)
        start, end = query.start, query.end
        if query.period:
            today = self.today()
            start, end = today, today
            if query.period == "amanha":
                start = end = today + timedelta(days=1)
            elif query.period == "ontem":
                start = end = today - timedelta(days=1)
            elif query.period == "esta_semana":
                start = today - timedelta(days=today.weekday())
                end = start + timedelta(days=6)
            elif query.period == "proximos_dias":
                end = today + timedelta(days=query.days or 0)
        if start or end:
            if not date_column:
                raise DomainError("INVALID_PARAMETER", "Esta consulta não aceita período.")
            if start:
                add(date_column, ">=", start)
            if end:
                add(date_column, "<", end + timedelta(days=1))
        # Do not send contact PII, DB account or arbitrary user notes to the LLM.
        excluded = {"requester_email", "requester_name", "approver_name", "id_requester_user",
                    "id_approver_user", "email", "whatsapp", "cnpj", "db_user"}
        projection = sql.SQL(", ").join(sql.SQL("q.{}").format(sql.Identifier(c))
                                       for c in sorted(columns - excluded))
        where = sql.SQL(" AND ").join(clauses) if clauses else sql.SQL("TRUE")
        statement = sql.SQL("SELECT {} FROM ({}) q WHERE {} ORDER BY {} LIMIT %s").format(
            projection, source, where, sql.SQL(order))
        return self.rows(statement, (*params, query.limit + 1))

    def product(self, product_id: int, lock: bool = False) -> dict | None:
        return self.one("""SELECT p.id_product,p.name,p.brand,p.active,mu.symbol,mu.description AS unit_description
          FROM public.tb_product p JOIN public.tb_measurement_unit mu USING (id_unit)
          WHERE p.id_product=%s""" + (" FOR SHARE OF p,mu" if lock else ""), (product_id,))

    def batches(self, product_id: int, kitchen_id: int, batch_id: int | None,
                lock: bool = False) -> list[dict]:
        return self.rows("""SELECT id_batch,id_product,id_kitchen,batch_number,current_quantity,
          expiration_date,status FROM public.tb_stock_batch
          WHERE id_product=%s AND id_kitchen=%s AND (%s IS NULL OR id_batch=%s)
          AND (%s IS NOT NULL OR (status='ACTIVE' AND current_quantity>0))
          ORDER BY id_batch LIMIT 101""" + (" FOR UPDATE" if lock else ""),
          (product_id, kitchen_id, batch_id, batch_id, batch_id))

    def supplier(self, product_id: int, supplier_id: int, lock: bool = False) -> dict | None:
        return self.one("""SELECT s.id_supplier,s.legal_name,s.active,ps.reference_price
          FROM public.tb_product_supplier ps JOIN public.tb_supplier s USING (id_supplier)
          WHERE ps.id_product=%s AND ps.id_supplier=%s"""
          + (" FOR SHARE OF ps,s" if lock else ""), (product_id, supplier_id))

    def withdraw(self, batch_id: int, kitchen_id: int, quantity: Any) -> dict:
        row = self.one("""UPDATE public.tb_stock_batch SET current_quantity=current_quantity-%s
          WHERE id_batch=%s AND id_kitchen=%s AND status='ACTIVE' AND current_quantity>=%s
          RETURNING id_batch,current_quantity,status""", (quantity,batch_id,kitchen_id,quantity))
        if row is None:
            raise DomainError("CONFLICT", "O saldo ou status do lote mudou.")
        return row

    def create_purchase(self, kitchen_id: int, user_id: str, purchase: Any) -> dict:
        row = self.one("""INSERT INTO public.tb_requisition
          (requisition_type,origin,id_kitchen,id_requester_user,reason)
          VALUES ('PURCHASE','SYSTEM',%s,%s,%s) RETURNING id_requisition,status,created_at""",
          (kitchen_id,user_id,purchase.reason))
        if row is None:
            raise DomainError("DATABASE_ERROR", "O banco não retornou a requisição criada.")
        items = []
        for item in purchase.items:
            items.append(self.one("""INSERT INTO public.tb_requisition_item
              (id_requisition,id_product,id_suggested_supplier,quantity,estimated_price,note)
              VALUES (%s,%s,%s,%s,%s,%s) RETURNING id_requisition_item,id_product,quantity""",
              (row["id_requisition"],item.product_id,item.supplier_id,item.quantity,item.estimated_price,item.note)))
        return {**row, "items": items}
