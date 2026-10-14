"""Métricas técnicas sem conteúdo conversacional ou segredos."""

import json
import math
import os
from datetime import UTC, datetime

from pymongo.errors import PyMongoError

from iai.app.config import LLM_MODE
from iai.app.storage import get_db


def estimate_cost(usage):
    if not usage:
        return None
    try:
        prices = json.loads(os.getenv("IAI_MODEL_PRICES_JSON", "{}"))
        cost = 0.0
        for model, counts in usage.items():
            price = prices[model]
            if any(
                not isinstance(price[k], (int, float))
                or price[k] < 0
                or isinstance(price[k], bool)
                or not math.isfinite(price[k])
                for k in ("input", "output")
            ):
                return None
            if any(
                not isinstance(counts[k], int)
                or isinstance(counts[k], bool)
                or counts[k] < 0
                for k in ("input_tokens", "output_tokens")
            ):
                return None
            cost += (
                counts["input_tokens"] * price["input"]
                + counts["output_tokens"] * price["output"]
            ) / 1_000_000
        return cost
    except (KeyError, ValueError, TypeError):
        return None


def record_metric(identity, latency, status, agents, usage=None, error_type=None):
    try:
        get_db().metrics.insert_one(
            {
                "created_at": datetime.now(UTC),
                "user_id": identity.user_id,
                "kitchen_id": identity.kitchen_id,
                "latency_seconds": latency,
                "status": status,
                "error_type": error_type,
                "agents": [str(agent).split(":")[0] for agent in agents],
                "llm_mode": LLM_MODE,
                "usage": usage or {},
                "estimated_cost_usd": estimate_cost(usage),
            }
        )
    except PyMongoError:
        pass  # telemetria não muda o resultado da operação


def summarize(identity):
    query = {"kitchen_id": identity.kitchen_id}
    if identity.role != "SUPERVISOR":
        query["user_id"] = identity.user_id
    rows = list(get_db().metrics.find(query).sort("created_at", -1).limit(1000))
    return {
        "requests": len(rows),
        "errors": sum(r["status"] == "erro" for r in rows),
        "average_latency_seconds": sum(r["latency_seconds"] for r in rows) / len(rows)
        if rows
        else 0,
        "window": "últimas 1000 execuções autorizadas",
        "estimated_cost_usd": sum(r["estimated_cost_usd"] for r in rows)
        if rows and all(r.get("estimated_cost_usd") is not None for r in rows)
        else None,
        "cost_note": "Estimativa pelos tokens reportados e tabela IAI_MODEL_PRICES_JSON configurada; não é fatura. Sem tabela completa, valor nulo.",
    }
