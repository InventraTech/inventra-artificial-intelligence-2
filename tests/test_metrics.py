from iai.app.metrics import estimate_cost, record_metric, summarize
from iai.app.storage import get_db
from tests_helpers import context_for


def test_unknown_prices_are_not_reported_as_zero_cost(monkeypatch):
    monkeypatch.delenv("IAI_MODEL_PRICES_JSON", raising=False)
    assert estimate_cost({"model": {"input_tokens": 100, "output_tokens": 50}}) is None
    monkeypatch.setenv("IAI_MODEL_PRICES_JSON", '{"model":{"input":1,"output":2}}')
    assert (
        estimate_cost({"model": {"input_tokens": 100, "output_tokens": 50}}) == 0.0002
    )


def test_metrics_scope_and_no_judge_content():
    actor = context_for().identity
    record_metric(actor, 1.0, "ok", ["juiz:reprovado:private-reason"])
    record_metric(context_for("demo-comprador").identity, 2.0, "erro", ["comprador"])
    assert get_db().metrics.find_one({"user_id": actor.user_id})["agents"] == ["juiz"]
    assert summarize(actor)["requests"] == 1
    assert summarize(context_for("demo-supervisor").identity)["requests"] == 2
    assert summarize(context_for("demo-outra-cozinha").identity)["requests"] == 0
