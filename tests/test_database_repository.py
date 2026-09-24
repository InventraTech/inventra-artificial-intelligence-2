from datetime import date
from types import SimpleNamespace

import psycopg2
import pytest
from psycopg2 import sql

from iai.app import database
from iai.app.domain import DomainError, ReadQuery
from iai.app.repository import Repository


def sql_text(value):
    if isinstance(value, sql.SQL):
        return value.string
    if isinstance(value, sql.Identifier):
        return '"' + '"."'.join(value.strings) + '"'
    if isinstance(value, sql.Composed):
        return "".join(sql_text(v) for v in value.seq)
    return str(value)


class Cursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = [SimpleNamespace(name=c) for c in conn.columns]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, params=()):
        self.conn.calls.append((sql_text(statement), params))

    def fetchall(self):
        return self.conn.rows


class Connection:
    def __init__(self, columns=(), rows=()):
        self.columns, self.rows, self.calls = columns, rows, []
        self.closed = False
        self.session = None
        self.outcome = None

    def cursor(self):
        return Cursor(self)

    def set_session(self, **kwargs):
        self.session = kwargs

    def __enter__(self):
        return self

    def __exit__(self, typ, *args):
        self.outcome = "rollback" if typ else "commit"
        return False

    def close(self):
        self.closed = True


@pytest.fixture
def configured(monkeypatch):
    for key in ("DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD"):
        monkeypatch.setenv(key, "test")
    monkeypatch.setenv("DB_PORT", "5432")
    return monkeypatch


@pytest.mark.parametrize("write", [False, True])
def test_transaction_mode_commit_and_close(configured, write):
    conn = Connection()
    configured.setattr(database.psycopg2, "connect", lambda **kwargs: conn)
    with database.transaction(write=write) as got:
        assert got is conn
    assert conn.session == {"readonly": not write}
    assert conn.outcome == "commit" and conn.closed


def test_transaction_rollback_on_business_error(configured):
    conn = Connection()
    configured.setattr(database.psycopg2, "connect", lambda **kwargs: conn)
    with pytest.raises(DomainError), database.transaction(write=True):
        raise DomainError("CONFLICT", "conflict")
    assert conn.outcome == "rollback" and conn.closed


@pytest.mark.parametrize("error,code", [
    (psycopg2.OperationalError, "DATABASE_UNAVAILABLE"),
    (psycopg2.InterfaceError, "DATABASE_UNAVAILABLE"),
    (psycopg2.errors.QueryCanceled, "TIMEOUT"),
    (psycopg2.errors.LockNotAvailable, "CONFLICT"),
    (psycopg2.IntegrityError, "CONFLICT"),
    (psycopg2.ProgrammingError, "DATABASE_ERROR"),
])
def test_database_error_mapping(configured, error, code):
    def fail(**kwargs):
        raise error("secret connection details")
    configured.setattr(database.psycopg2, "connect", fail)
    with pytest.raises(DomainError) as exc, database.transaction():
        pass
    assert exc.value.code == code
    assert "secret" not in exc.value.message


def test_missing_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("DB_HOST", raising=False)
    with pytest.raises(DomainError) as exc, database.transaction():
        pass
    assert exc.value.code == "CONFIGURATION"


def test_query_injection_is_bound_as_value():
    conn = Connection(["id_batch", "id_kitchen", "product_name", "expiration_date", "status"])
    text = "tomate'; DELETE FROM tb_stock_batch; --%_"
    Repository(conn).read(ReadQuery(resource="lotes", product=text), 7)
    statement, params = conn.calls[-1]
    assert "DELETE" not in statement
    assert params[0] == 7
    assert "DELETE" in params[1] and "\\%\\_" in params[1]
    assert params[-1] == 26


def test_pre_list_filters_active():
    conn = Connection(["id_batch", "id_kitchen", "expiration_date", "status"])
    Repository(conn).read(ReadQuery(resource="pre_lista"), 7)
    assert "ACTIVE" in conn.calls[-1][1]
    with pytest.raises(DomainError):
        Repository(conn).read(ReadQuery(resource="pre_lista", status="CANCELLED"), 7)


def test_unsupported_filter_not_silently_dropped():
    conn = Connection(["id_batch", "id_kitchen", "expiration_date"])
    with pytest.raises(DomainError) as exc:
        Repository(conn).read(ReadQuery(resource="lotes", supplier="supplier"), 7)
    assert exc.value.code == "INVALID_PARAMETER"


def test_view_without_kitchen_cannot_leak_data():
    conn = Connection(["id_batch"])
    with pytest.raises(DomainError) as exc:
        Repository(conn).read(ReadQuery(resource="lotes"), 7)
    assert exc.value.code == "FORBIDDEN"


@pytest.mark.parametrize("period,days,start,end", [
    ("hoje", None, date(2030,1,2), date(2030,1,3)),
    ("amanha", None, date(2030,1,3), date(2030,1,4)),
    ("ontem", None, date(2030,1,1), date(2030,1,2)),
    ("esta_semana", None, date(2029,12,31), date(2030,1,7)),
    ("proximos_dias", 3, date(2030,1,2), date(2030,1,6)),
])
def test_relative_dates_use_database_clock(monkeypatch, period, days, start, end):
    conn = Connection(["id_batch", "id_kitchen", "expiration_date"])
    repo = Repository(conn)
    monkeypatch.setattr(repo, "today", lambda: date(2030,1,2))
    repo.read(ReadQuery(resource="lotes", period=period, days=days), 7)
    assert conn.calls[-1][1] == (7, start, end, 26)


def test_stock_update_is_conditional_and_returns_actual_row():
    conn = Connection(rows=[{"id_batch": 20, "current_quantity": 15}])
    response = Repository(conn).withdraw(20,7,5)
    statement, params = conn.calls[-1]
    assert "current_quantity>=%s" in statement and "RETURNING" in statement
    assert params == (5,20,7,5) and response["current_quantity"] == 15


def test_update_no_row_is_conflict():
    with pytest.raises(DomainError) as exc:
        Repository(Connection()).withdraw(20,7,5)
    assert exc.value.code == "CONFLICT"


def test_product_and_identity_locks():
    conn = Connection()
    repo = Repository(conn)
    repo.principal("uuid", lock=True)
    assert "FOR SHARE OF u,p,k" in conn.calls[-1][0]
    repo.product(10, lock=True)
    assert "FOR SHARE OF p,mu" in conn.calls[-1][0]
    repo.batches(10,7,20,lock=True)
    assert "FOR UPDATE" in conn.calls[-1][0]


def test_supplier_lock_and_read_column_privacy():
    conn = Connection(["id_supplier", "supplier_name", "email", "cnpj", "whatsapp"])
    repo = Repository(conn)
    repo.supplier(10,30,lock=True)
    assert "FOR SHARE OF ps,s" in conn.calls[-1][0]
    repo.read(ReadQuery(resource="fornecedores"), 7)
    projection = conn.calls[-1][0].split("FROM")[0]
    assert '"email"' not in projection and '"cnpj"' not in projection
