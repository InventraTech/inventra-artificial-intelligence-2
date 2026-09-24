from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import Lock
from types import SimpleNamespace

import pytest

from iai.app.confirmation import ConfirmationStore
from iai.app.domain import DomainError
from iai.app.security import ExecutionContext

CTX = ExecutionContext("user-1", "session-1")


def nested(data, dotted):
    for part in dotted.split("."):
        if not isinstance(data, dict):
            return None
        data = data.get(part)
    return data


class Collection:
    """Small atomic document fixture; emulates only selectors used by the store."""
    def __init__(self):
        self.document = {"_id": "doc", "user_id": CTX.user_id, "session_id": CTX.session_id}
        self.lock = Lock()

    def matches(self, selector):
        for key, expected in selector.items():
            actual = nested(self.document, key)
            if isinstance(expected, dict):
                if "$ne" in expected and actual == expected["$ne"]:
                    return False
                if "$gt" in expected and not (actual and actual > expected["$gt"]):
                    return False
            elif actual != expected:
                return False
        return True

    def update(self, update):
        for key, value in update["$set"].items():
            parts = key.split(".")
            target = self.document
            for part in parts[:-1]:
                target = target.setdefault(part, {})
            target[parts[-1]] = deepcopy(value)

    def update_one(self, selector, update):
        with self.lock:
            matched = self.matches(selector)
            if matched:
                self.update(update)
            return SimpleNamespace(matched_count=int(matched))

    def find_one(self, selector):
        with self.lock:
            return deepcopy(self.document) if self.matches(selector) else None

    def find_one_and_update(self, selector, update, **kwargs):
        with self.lock:
            if not self.matches(selector):
                return None
            self.update(update)
            return deepcopy(self.document)


@pytest.fixture
def confirmation():
    collection = Collection()
    store = ConfirmationStore(collection)
    proposal = {"operation": "withdrawal", "parameters": {"quantity": "5"}, "snapshot": {"quantity": "5"}}
    response = store.save("doc", CTX, proposal)
    writes = []

    def execute(context, data):
        assert data["parameters"]["quantity"] == "5"
        writes.append(data["id"])
        return {"success": True, "code": "COMPLETED", "data": {"current_quantity": "15"}}

    return SimpleNamespace(store=store, collection=collection, proposal=proposal, response=response,
                           token=response["confirmation_id"], service=SimpleNamespace(execute=execute), writes=writes)


def test_save_prepares_only(confirmation):
    assert confirmation.response["code"] == "CONFIRMATION_REQUIRED"
    assert not confirmation.writes


def test_confirmation_is_single_use_and_replay_returns_same(confirmation):
    c = confirmation
    first = c.store.confirm(CTX, c.token, True, c.service)
    assert first == c.store.confirm(CTX, c.token, True, c.service)
    assert len(c.writes) == 1


def test_cancellation_never_executes(confirmation):
    c = confirmation
    assert c.store.confirm(CTX, c.token, False, c.service)["code"] == "CANCELLED"
    assert c.store.confirm(CTX, c.token, True, c.service)["code"] == "CANCELLED"
    assert not c.writes


@pytest.mark.parametrize("context", [ExecutionContext("other", "session-1"), ExecutionContext("user-1", "other")])
def test_other_identity_or_session_cannot_confirm(confirmation, context):
    c = confirmation
    with pytest.raises(DomainError) as exc:
        c.store.confirm(context, c.token, True, c.service)
    assert exc.value.code == "NOT_FOUND" and not c.writes


def test_expiration(confirmation):
    c = confirmation
    c.collection.document["pending_operation"]["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
    with pytest.raises(DomainError) as exc:
        c.store.confirm(CTX, c.token, True, c.service)
    assert exc.value.code == "EXPIRED_CONFIRMATION" and not c.writes


def test_parallel_confirmation_executes_once(confirmation):
    c = confirmation
    def run(_):
        try:
            return c.store.confirm(CTX, c.token, True, c.service)
        except DomainError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(run, range(20)))
    assert len(c.writes) == 1


@pytest.mark.parametrize("code", ["DATABASE_UNAVAILABLE", "DATABASE_ERROR"])
def test_uncertain_commit_never_retries(confirmation, code):
    c = confirmation
    def execute(*args):
        c.writes.append("possibly committed")
        raise DomainError(code, "lost connection")
    c.service.execute = execute
    for _ in range(2):
        with pytest.raises(DomainError) as exc:
            c.store.confirm(CTX, c.token, True, c.service)
        assert exc.value.code == "INDETERMINATE"
    assert len(c.writes) == 1


def test_validation_failure_is_persisted(confirmation):
    c = confirmation
    def execute(*args):
        raise DomainError("INSUFFICIENT_STOCK", "Saldo insuficiente")
    c.service.execute = execute
    first = c.store.confirm(CTX, c.token, True, c.service)
    assert first["code"] == "INSUFFICIENT_STOCK"
    assert first == c.store.confirm(CTX, c.token, True, c.service)


def test_new_proposal_invalidates_previous(confirmation):
    c = confirmation
    newer = c.store.save("doc", CTX, c.proposal)
    assert newer["confirmation_id"] != c.token
    with pytest.raises(DomainError):
        c.store.confirm(CTX, c.token, True, c.service)
    assert not c.writes


def test_cannot_replace_executing_proposal(confirmation):
    c = confirmation
    c.collection.document["pending_operation"]["status"] = "executing"
    with pytest.raises(DomainError):
        c.store.save("doc", CTX, c.proposal)
