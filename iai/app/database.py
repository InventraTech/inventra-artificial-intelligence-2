"""One PostgreSQL boundary; no schema management or SQL supplied by a model."""
import os
from collections.abc import Iterator
from contextlib import contextmanager

import psycopg2
from psycopg2.extensions import connection
from psycopg2.extras import RealDictCursor

from iai.app import config  # noqa: F401 -- load environment once
from iai.app.domain import DomainError


@contextmanager
def transaction(*, write: bool = False) -> Iterator[connection]:
    conn = None
    try:
        settings = {"host": os.getenv("DB_HOST"), "port": os.getenv("DB_PORT", "5432"),
                    "dbname": os.getenv("DB_NAME"), "user": os.getenv("DB_USER"),
                    "password": os.getenv("DB_PASSWORD")}
        if not all(settings.values()):
            raise DomainError("CONFIGURATION", "Configuração PostgreSQL incompleta.")
        sslmode = os.getenv("DB_SSLMODE", "require")
        if sslmode not in {"require", "verify-ca", "verify-full"}:
            raise DomainError("CONFIGURATION", "PostgreSQL exige TLS.")
        conn = psycopg2.connect(**settings, sslmode=sslmode, connect_timeout=10,
                               cursor_factory=RealDictCursor,
                               application_name="inventra-ai",
                               options="-c statement_timeout=15000 -c lock_timeout=5000")
        conn.set_session(readonly=not write)
        with conn:
            yield conn
    except psycopg2.errors.QueryCanceled as exc:
        raise DomainError("TIMEOUT", "A consulta excedeu o tempo limite.") from exc
    except (psycopg2.errors.LockNotAvailable, psycopg2.errors.SerializationFailure,
            psycopg2.errors.DeadlockDetected, psycopg2.IntegrityError) as exc:
        raise DomainError("CONFLICT", "Os dados mudaram ou violam uma regra do banco.") from exc
    except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
        raise DomainError("DATABASE_UNAVAILABLE", "Não foi possível conectar ou concluir a operação no banco.") from exc
    except psycopg2.Error as exc:
        raise DomainError("DATABASE_ERROR", "O banco não concluiu a operação; nenhum sucesso foi confirmado.") from exc
    finally:
        if conn is not None:
            conn.close()
