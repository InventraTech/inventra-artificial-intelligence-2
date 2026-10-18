from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from iai.app import audio


def cliente_falso(texto):
    def create(**kwargs):
        assert kwargs["language"] == "pt"
        assert kwargs["model"] == audio.modelo_transcricao
        return SimpleNamespace(text=texto)

    return SimpleNamespace(
        audio=SimpleNamespace(transcriptions=SimpleNamespace(create=create))
    )


def test_transcreve_e_remove_espacos(monkeypatch):
    monkeypatch.setattr(audio, "cliente", lambda: cliente_falso("  usei 2 kg de arroz "))
    assert audio.transcrever_audio(b"x", "voz.webm") == "usei 2 kg de arroz"


@pytest.mark.parametrize(
    ("conteudo", "nome", "status"),
    [(b"x", "virus.exe", 415), (b"", "voz.mp3", 422), (None, "voz.mp3", 413)],
)
def test_rejeita_entrada_invalida(conteudo, nome, status):
    if conteudo is None:
        conteudo = b"x" * (audio.TAMANHO_MAXIMO_BYTES + 1)
    with pytest.raises(HTTPException) as erro:
        audio.transcrever_audio(conteudo, nome)
    assert erro.value.status_code == status


def test_transcricao_vazia_vira_422(monkeypatch):
    monkeypatch.setattr(audio, "cliente", lambda: cliente_falso("   "))
    with pytest.raises(HTTPException) as erro:
        audio.transcrever_audio(b"x", "voz.wav")
    assert erro.value.status_code == 422


def post_audio(client, headers=None, user_id="demo-estoquista"):
    return client.post(
        "/chat/audio",
        headers=headers or {},
        data={"session_id": "s1", "user_id": user_id},
        files={"audio": ("voz.webm", b"bytes", "audio/webm")},
    )


def test_rota_transcreve_e_usa_o_fluxo_do_chat(monkeypatch):
    from fastapi.testclient import TestClient

    from iai.app import graph
    from iai.app.main import app
    from iai.app.routes import chat
    from iai.app.security import context

    monkeypatch.setattr(
        chat, "transcrever_audio", lambda conteudo, nome: "quanto tenho de arroz"
    )

    def fluxo(pergunta_usuario, session_id, user_id):
        assert context().identity.user_id == user_id == "demo-estoquista"
        return f"ok:{pergunta_usuario}"

    monkeypatch.setattr(graph, "executar_fluxo_assessor", fluxo)
    resposta = post_audio(
        TestClient(app), {"Authorization": "Bearer demo-estoquista"}
    )
    assert resposta.status_code == 200
    assert resposta.json() == {
        "transcricao": "quanto tenho de arroz",
        "resposta": "ok:quanto tenho de arroz",
    }


def test_rota_exige_token_e_user_id_igual_ao_do_token():
    from fastapi.testclient import TestClient

    from iai.app.main import app

    cliente = TestClient(app)
    assert post_audio(cliente).status_code == 401
    outro = post_audio(
        cliente, {"Authorization": "Bearer demo-estoquista"}, user_id="demo-comprador"
    )
    assert outro.status_code == 403
