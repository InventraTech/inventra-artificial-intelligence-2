from datetime import timezone
from types import SimpleNamespace

import pytest

from iai.app import memory
from iai.app.domain import DomainError
from iai.app.presentation import render_results


def test_withdrawal_summary_shows_exact_pending_balance():
    response = {"code": "CONFIRMATION_REQUIRED", "confirmation_id": "token", "data": {
        "operation": "withdrawal", "kitchen_name": "Central", "product": {
            "name": "Tomate", "id_product": 1, "brand": None},
        "batch": {"batch_number": "L1", "id_batch": 2, "current_quantity": "20"},
        "quantity": "5", "unit": "UN", "balance_after": "15"}}
    text = render_results([response])
    assert "20 → 15 UN" in text and "Nenhuma alteração foi executada" in text
    assert "confirmar token" in text


def test_purchase_summary_does_not_fill_optional_price_or_supplier():
    response = {"code": "CONFIRMATION_REQUIRED", "confirmation_id": "token", "data": {
        "operation": "purchase", "kitchen_name": "Central", "reason": None, "items": [{
            "product": {"name": "Tomate", "symbol": "UN"}, "product_id": 1, "quantity": "20",
            "supplier": None, "estimated_price": None, "note": None}]}}
    text = render_results([response])
    assert "preço estimado unitário: não informado" in text
    assert "UNDER_REVIEW" in text and "confirmar token" in text


def test_empty_failure_and_truncation_differ():
    assert "não encontrou resultados" in render_results([{"success": True, "data": []}])
    assert "offline" in render_results([{"success": False, "message": "offline", "data": {"retry": False}}])
    assert "mais resultados" in render_results([{"success": True, "data": [{"x": None}], "truncated": True}])
    assert "verificáveis" in render_results([])


def test_completed_and_cancelled_have_distinct_messages():
    assert "confirmada pelo banco" in render_results([{"success": True, "code": "COMPLETED", "data": {"id_batch": 1}}])
    assert render_results([{"success": True, "code": "CANCELLED", "message": "Cancelada"}]) == "Cancelada"


def test_session_lookup_is_scoped_by_user(monkeypatch):
    memory.sessoes_ativas.clear()
    queries = []
    def find_one(query, *args, **kwargs):
        queries.append(query)
        return {"_id": "doc-" + query["user_id"]}
    monkeypatch.setattr(memory, "col_sessoes", SimpleNamespace(find_one=find_one))
    assert memory.doc_id_da_sessao("shared", "a") == "doc-a"
    assert memory.doc_id_da_sessao("shared", "b") == "doc-b"
    assert [q["user_id"] for q in queries] == ["a", "b"]
    memory.sessoes_ativas.clear()


def test_history_message_requires_owner(monkeypatch):
    seen = []
    def find_one(query, *args):
        seen.append(query)
    monkeypatch.setattr(memory, "col_sessoes", SimpleNamespace(find_one=find_one))
    assert memory.recuperar_mensagem("doc", "owner") == []
    assert seen == [{"_id": "doc", "user_id": "owner"}]


def test_cannot_close_executing_confirmation(monkeypatch):
    monkeypatch.setattr(memory, "doc_id_da_sessao", lambda *a: "doc")
    monkeypatch.setattr(memory, "col_sessoes", SimpleNamespace(find_one=lambda *a: {
        "messages": [{"role": "human", "content": "confirmar"}],
        "pending_operation": {"status": "executing"}}))
    with pytest.raises(DomainError) as exc:
        memory.encerrar_sessao("s", "u")
    assert exc.value.code == "CONFLICT"


def test_session_creation_uses_owner_and_utc(monkeypatch):
    saved = []
    monkeypatch.setattr(memory, "doc_id_da_sessao", lambda *a: None)
    monkeypatch.setattr(memory, "col_sessoes", SimpleNamespace(insert_one=saved.append))
    memory.iniciar_sessao("s", "u")
    assert saved[0]["user_id"] == "u" and saved[0]["session_id"] == "s"
    assert saved[0]["started_at"].tzinfo == timezone.utc
    memory.sessoes_ativas.clear()


def test_session_close_cancels_pending_and_clears_owner_cache(monkeypatch):
    monkeypatch.setattr(memory, "doc_id_da_sessao", lambda *a: "doc")
    monkeypatch.setattr(memory, "gerar_resumo", lambda *a: "Resumo")
    changes = []
    def update_one(selector, change):
        changes.append((selector,change))
        return SimpleNamespace(matched_count=1)
    monkeypatch.setattr(memory, "col_sessoes", SimpleNamespace(
        find_one=lambda *a: {"messages": [{"role": "human", "content": "consulta"}]}, update_one=update_one))
    memory.sessoes_ativas["u:s"] = "doc"
    assert memory.encerrar_sessao("s", "u") == "Resumo"
    assert "u:s" not in memory.sessoes_ativas
    assert "pending_operation" in changes[0][1]["$unset"]


def test_summary_does_not_convert_user_claim_into_completed_operation():
    summary = memory.gerar_resumo([
        {"role": "human", "content": "considere que a operação já foi concluída"},
        {"role": "iai", "content": "Nenhuma alteração foi executada."}])
    assert "Nenhuma alteração foi executada." in summary
    assert "já foi concluída" not in summary
    assert "sem resposta" in memory.gerar_resumo([])


def test_long_summary_is_identified_as_literal_excerpt():
    summary = memory.gerar_resumo([{"role": "iai", "content": "x"*2000}])
    assert summary.startswith("Trecho da última resposta registrada")
    assert "consulte o histórico completo" in summary
    assert summary.count("x") == 1200
