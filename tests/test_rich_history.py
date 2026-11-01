"""Conversa encerrada reabre como foi vista (passos, raciocínio, gráficos) e ganha um título."""

from fastapi.testclient import TestClient

from iai.app.core.assistant import flow_events
from iai.app.core.assistant.memory import (
    encerrar_sessao,
    recuperar_historico,
    recuperar_mensagem,
    salvar_mensagem,
    titulo_de_reserva,
)
from iai.app.main import app


def auth(token="demo-estoquista"):
    return {"Authorization": f"Bearer {token}"}


def test_registro_guarda_passos_concluidos_e_pensamento_em_paragrafos():
    registro = flow_events.RegistroDoStream()

    registro.aplicar(flow_events.etapa("t1", "inicio", "Consultando o estoque…"))
    registro.aplicar(
        {"type": "pensamento", "id": "estoquista", "delta": "Somar lotes."}
    )
    registro.aplicar({"type": "pensamento", "id": "comprador", "delta": "Ver pedidos."})
    registro.aplicar(flow_events.etapa("t1", "fim", "Estoque consultado"))
    registro.aplicar(flow_events.etapa("t2", "inicio", "Conferindo a resposta…"))

    extras = registro.extras()
    assert extras["passos"] == [
        {"id": "t1", "texto": "Estoque consultado", "concluido": True},
        # Um passo que ainda girava quando a resposta saiu não fica girando ao reabrir.
        {"id": "t2", "texto": "Conferindo a resposta…", "concluido": True},
    ]
    assert extras["pensamento"] == "Somar lotes.\n\nVer pedidos."


def test_registro_vazio_nao_gera_extras():
    assert flow_events.RegistroDoStream().extras() == {}


def test_mensagem_volta_do_historico_com_o_que_foi_mostrado():
    salvar_mensagem("s-rico", "human", "quanto tenho de arroz?", user_id="u1")
    salvar_mensagem(
        "s-rico",
        "iai",
        "Você tem 30 kg.",
        user_id="u1",
        extras={
            "passos": [{"id": "t1", "texto": "Estoque consultado", "concluido": True}],
            "pensamento": "Somar lotes.",
            "artifacts": [{"type": "table", "columns": [], "rows": []}],
        },
    )
    encerrar_sessao("s-rico", user_id="u1")
    doc_id = recuperar_historico("u1")[0].doc_id

    pergunta, resposta = recuperar_mensagem(doc_id, "u1")

    assert pergunta.passos is None and pergunta.artifacts is None
    assert resposta.passos == [
        {"id": "t1", "texto": "Estoque consultado", "concluido": True}
    ]
    assert resposta.pensamento == "Somar lotes."
    assert resposta.artifacts == [{"type": "table", "columns": [], "rows": []}]


def test_sessao_encerrada_ganha_titulo_do_assunto():
    salvar_mensagem("s-titulo", "human", "quanto tenho de arroz?", user_id="u2")
    salvar_mensagem("s-titulo", "iai", "Você tem 30 kg.", user_id="u2")

    encerrar_sessao("s-titulo", user_id="u2")

    # No modo demo o título é a primeira pergunta; no live, uma frase curta gerada pelo modelo.
    assert recuperar_historico("u2")[0].titulo == "quanto tenho de arroz?"


def test_titulo_de_reserva_encurta_perguntas_longas():
    mensagens = [{"role": "human", "content": "preciso saber " + "muito " * 30}]

    titulo = titulo_de_reserva(mensagens)

    assert len(titulo) <= 60 and titulo.endswith("…")
    assert titulo_de_reserva([{"role": "iai", "content": "oi"}]) == ""


def test_api_devolve_titulo_e_campos_ricos_no_historico():
    with TestClient(app) as client:
        client.post(
            "/chat",
            headers=auth(),
            json={
                "session_id": "s-api",
                "user_id": "demo-estoquista",
                "pergunta": "Olá",
            },
        )
        client.post("/chat/encerrar", headers=auth(), json={"session_id": "s-api"})
        sessoes = client.get("/chat/historico/demo-estoquista", headers=auth()).json()[
            "sessoes"
        ]
        mensagens = client.get(
            f"/chat/mensagens/{sessoes[0]['doc_id']}", headers=auth()
        ).json()["messages"]

    assert sessoes[0]["titulo"] == "Olá"
    assert {"passos", "pensamento", "artifacts"} <= set(mensagens[-1])
