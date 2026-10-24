"""Mongo real ou substituto explícito para demonstração/testes. Sem I/O no import."""

from functools import lru_cache

from pymongo import MongoClient

from iai.app.core.config import MONGO_CONNECTION, STORAGE_MODE


@lru_cache(maxsize=1)
def get_client():
    if STORAGE_MODE == "mock":
        import mongomock

        return mongomock.MongoClient(tz_aware=True)
    return MongoClient(
        MONGO_CONNECTION.get_secret_value(),
        tz_aware=True,
        serverSelectionTimeoutMS=3000,
        connectTimeoutMS=3000,
    )


def get_db():
    return get_client()["dbIAI"]


class CollectionProxy:
    def __init__(self, name):
        self.name = name

    def __getattr__(self, name):
        return getattr(get_db()[self.name], name)


def initialize_storage():
    db = get_db()
    db.sessions.create_index(
        [("user_id", 1), ("kitchen_id", 1), ("session_id", 1), ("active", 1)],
        unique=True,
        partialFilterExpression={"active": True},
    )
    db.notifications.create_index([("kitchen_id", 1), ("created_at", -1)])
    db.metrics.create_index([("kitchen_id", 1), ("timestamp", -1)])
