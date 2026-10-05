import iai.app.memory as memory_module


class _ColSessoesFalsa:
    def __init__(self, doc=None):
        self.doc = doc
        self.updates = []

    def find_one(self, *_a, **_k):
        return self.doc

    def update_one(self, filtro, update):
        self.updates.append((filtro, update))


def test_formatar_conversa_ignora_mensagens_do_juiz():
    mensagens = [
        {"role": "human", "content": "oi"},
        {"role": "judge", "content": "avaliação interna", "confianca": 0.9},
        {"role": "iai", "content": "olá"},
    ]
    resultado = memory_module.formatar_conversa(mensagens)
    assert "avaliação interna" not in resultado
    assert "human: oi" in resultado
    assert "iai: olá" in resultado


def test_recuperar_mensagem_filtra_role_judge(monkeypatch):
    doc = {
        "messages": [
            {"role": "human", "content": "oi"},
            {"role": "judge", "content": "avaliação interna", "confianca": 0.4},
            {"role": "iai", "content": "olá"},
        ]
    }
    monkeypatch.setattr(memory_module, "col_sessoes", _ColSessoesFalsa(doc=doc))
    resultado = memory_module.recuperar_mensagem("doc-1")
    assert [m.role for m in resultado] == ["human", "iai"]


def test_recuperar_mensagem_doc_inexistente_retorna_lista_vazia(monkeypatch):
    monkeypatch.setattr(memory_module, "col_sessoes", _ColSessoesFalsa(doc=None))
    assert memory_module.recuperar_mensagem("doc-x") == []


def test_salvar_mensagem_juiz_grava_role_judge_com_confianca(monkeypatch):
    fake = _ColSessoesFalsa()
    monkeypatch.setattr(memory_module, "col_sessoes", fake)
    monkeypatch.setattr(memory_module, "iniciar_sessao", lambda *_a, **_k: None)
    monkeypatch.setattr(memory_module, "doc_id_da_sessao", lambda _sid: "doc-1")

    memory_module.salvar_mensagem_juiz("sessao-1", "motivo da reprovação", 0.3, user_id="user-1")

    assert len(fake.updates) == 1
    filtro, update = fake.updates[0]
    assert filtro == {"_id": "doc-1"}
    assert update["$push"]["messages"] == {
        "role": "judge",
        "content": "motivo da reprovação",
        "confianca": 0.3,
    }
