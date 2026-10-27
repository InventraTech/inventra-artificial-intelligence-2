import json
import runpy
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from iai.app.core.assistant import judge
from tests_helpers import jev_evaluation


@pytest.mark.parametrize("key", [*judge.POSITIVE, *judge.RISKS])
def test_atomic_criteria_individually_veto_execution(key):
    bad = 0.1 if key in judge.POSITIVE else 0.9
    decision = judge.policy(jev_evaluation(failed={key: bad}, route="agent_repair"), 1)
    assert decision.decision == "RETRY_SPECIALIST"
    assert key in decision.failed_checks


def test_probability_boundary_and_unknown_information_are_fail_closed():
    evaluation = jev_evaluation()
    for key in judge.POSITIVE:
        evaluation.answers[key].noul = 0.7
    for key in judge.RISKS:
        evaluation.answers[key].noul = 0.3
    assert judge.policy(evaluation, 3).decision == "APPROVE"
    evaluation.answers["arguments_grounded"].noul = 0.5
    assert judge.policy(evaluation, 1).decision == "REJECT"


def test_choice_confidence_is_not_used_as_a_probability():
    evaluation = jev_evaluation(
        failed={"arguments_grounded": 0.1}, route="agent_repair"
    )
    evaluation.answers["repair_route"].confidence = 0.0
    assert judge.policy(evaluation, 1).decision == "RETRY_SPECIALIST"


def test_missing_user_information_and_retry_limit():
    missing = jev_evaluation(
        failed={"missing_user_information": 0.99},
        route="user_input",
        missing="quantity",
        fields=["quantity"],
    )
    assert judge.policy(missing, 1).missing_field == "quantity"
    assert judge.policy(missing, 1).decision == "ASK_USER"
    repair = jev_evaluation(failed={"arguments_grounded": 0.1}, route="agent_repair")
    assert judge.policy(repair, 2).decision == "RETRY_SPECIALIST"
    assert judge.policy(repair, 3).decision == "REJECT"


@pytest.mark.parametrize(
    "probability", [float("nan"), float("inf"), -0.1, 1.1, True, "0.9"]
)
def test_invalid_probabilities_are_not_decisions(probability):
    with pytest.raises(ValidationError):
        judge.Noul(type="noul", noul=probability)


def state():
    return {
        "user_request": "Quais lotes vencem?",
        "tool": {"schema": {"properties": {"days": {"type": "integer"}}}},
    }


def setup_http(monkeypatch, handler):
    real_client = httpx.Client
    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr("test-typesafe-key"))
    monkeypatch.setattr(
        judge.httpx,
        "Client",
        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw),
    )


def test_real_http_adapter_contract_with_simulated_service(monkeypatch):
    def handler(request):
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        body = json.loads(request.content)
        assert "test-typesafe-key" not in request.content.decode()
        assert request.headers["authorization"] == "Bearer test-typesafe-key"
        assert body["model"] == "jev-1.13.0"
        assert {q["type"] for q in body["questions"].values()} == {"noul", "choice"}
        return httpx.Response(200, json=jev_evaluation(fields=["days"]).model_dump())

    setup_http(monkeypatch, handler)
    assert judge.policy(judge.evaluate(state()), 1).decision == "APPROVE"


@pytest.mark.parametrize("status", [401, 403, 429, 500, 302])
def test_http_errors_never_authorize(monkeypatch, status):
    setup_http(
        monkeypatch,
        lambda request: httpx.Response(status, text="Bearer private-server-secret"),
    )
    with pytest.raises(judge.JevUnavailable) as error:
        judge.evaluate(state())
    assert "private-server-secret" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"model": "jev-1.13.0", "answers": {}},
        {
            "model": "jev-1.13.0",
            "answers": {"intent_alignment": {"type": "noul", "noul": 8}},
        },
    ],
)
def test_malformed_responses_never_authorize(monkeypatch, payload):
    setup_http(monkeypatch, lambda request: httpx.Response(200, json=payload))
    with pytest.raises(judge.JevUnavailable):
        judge.evaluate(state())


def test_timeout_and_missing_key(monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("Bearer secret")

    setup_http(monkeypatch, timeout)
    with pytest.raises(judge.JevUnavailable):
        judge.evaluate(state())
    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr(""))
    with pytest.raises(judge.JevUnavailable, match="JEV_NOT_CONFIGURED"):
        judge.evaluate(state())


def test_credentials_only_in_header_and_input_state_is_preserved(monkeypatch):
    original = state()
    original["user_request"] = "test-typesafe-key"
    original["evidence"] = [{"result": "Bearer private-token"}]

    def handler(request):
        body = json.loads(request.content)
        assert request.headers["authorization"] == "Bearer test-typesafe-key"
        assert "test-typesafe-key" not in request.content.decode()
        assert "private-token" not in request.content.decode()
        assert body["state"]["user_request"] == "[SEGREDO_REMOVIDO]"
        return httpx.Response(200, json=jev_evaluation(fields=["days"]).model_dump())

    setup_http(monkeypatch, handler)
    judge.evaluate(original)
    assert original["user_request"] == "test-typesafe-key"
    assert original["evidence"][0]["result"] == "Bearer private-token"


@pytest.mark.parametrize(
    "process_key,base_key,expected",
    [
        (None, None, "app-key"),
        (None, "base-key", "base-key"),
        ("env-key", "base-key", "env-key"),
    ],
)
def test_config_reads_app_env_preserving_precedence(
    tmp_path, monkeypatch, process_key, base_key, expected
):
    base = tmp_path / "iai"
    app_dir = base / "app"
    config_path = app_dir / "core" / "config.py"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        (Path(judge.__file__).parents[1] / "config.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (app_dir / ".env").write_text('TYPESAFE_API_KEY="app-key"\n', encoding="utf-8")
    if base_key:
        (base / ".env").write_text(f"TYPESAFE_API_KEY={base_key}\n", encoding="utf-8")
    monkeypatch.setenv("TYPESAFE_API_KEY", process_key or "reset")
    if process_key is None:
        monkeypatch.delenv("TYPESAFE_API_KEY")
    loaded = runpy.run_path(str(config_path))
    assert loaded["TYPESAFE_API_KEY"].get_secret_value() == expected


@pytest.mark.parametrize("scope", ["escrita", "todas", "invalido"])
def test_config_validates_scope(scope):
    import os
    import subprocess
    import sys

    env = dict(os.environ, JEV_ESCOPO=scope)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from iai.app.core.config import JEV_ESCOPO; print(JEV_ESCOPO)",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    if scope == "invalido":
        assert result.returncode != 0
        assert "JEV_ESCOPO inválido" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == scope


def test_config_defaults_to_write_scope_without_dotenv(tmp_path, monkeypatch):
    config_path = tmp_path / "iai" / "app" / "core" / "config.py"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        (Path(judge.__file__).parents[1] / "config.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    monkeypatch.delenv("JEV_ESCOPO", raising=False)
    assert runpy.run_path(str(config_path))["JEV_ESCOPO"] == "escrita"
