"""/chat/stream: eventos NDJSON ao vivo; o texto só sai depois do grafo (e do juiz) terminar."""

import json
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage

from iai.app.core.assistant import eventos_do_fluxo, graph
from iai.app.core.infra.security import current_context
from iai.app.core.infra.storage import get_db
from iai.app.main import app
from tests_helpers import context_for


def auth(token="demo-estoquista"):
    return {"Authorization": f"Bearer {token}"}


def corpo(pergunta="Olá", user_id="demo-estoquista"):
    return {"session_id": "s-stream", "user_id": user_id, "pergunta": pergunta}


def linhas(response):
    return [json.loads(linha) for linha in response.text.split("\n") if linha.strip()]


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(eventos_do_fluxo, "INTERVALO_ENTRE_PEDACOS", 0)
    with TestClient(app) as client:
        yield client


def test_stream_exige_token_e_identidade(client):
    assert client.post("/chat/stream", json=corpo()).status_code == 401
    forjado = client.post("/chat/stream", headers=auth(), json=corpo(user_id="outro"))
    assert forjado.status_code == 403


def test_stream_demo_entrega_texto_em_pedacos_e_termina_com_fim(client):
    with client.stream(
        "POST", "/chat/stream", headers=auth(), json=corpo("Olá")
    ) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/x-ndjson")
        assert response.headers["cache-control"] == "no-cache"
        response.read()
    eventos = linhas(response)
    assert eventos[-1] == {"type": "fim", "status": "aprovado"}
    textos = [e["delta"] for e in eventos if e["type"] == "texto"]
    assert len(textos) > 1
    # O histórico guarda a frase final inteira, nunca os pedaços.
    sessao = get_db().sessions.find_one({"user_id": "demo-estoquista"})
    salvas = [m for m in sessao["messages"] if m["role"] == "iai"]
    assert len(salvas) == 1
    assert salvas[0]["content"] == "".join(textos)


class FluxoFalso:
    """Substitui o grafo compilado: devolve o que `stream(stream_mode=[...])` devolveria."""

    def __init__(self, itens):
        self.itens = itens
        self.modos = None

    def stream(self, estado, config=None, stream_mode=None, subgraphs=False):
        self.modos = stream_mode
        self.subgraphs = subgraphs
        yield from self.itens


def ao_vivo(monkeypatch, itens):
    fluxo = FluxoFalso(itens)
    monkeypatch.setattr(graph, "fluxo_agentes", fluxo)
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "_exigir_provedores", lambda: None)
    return fluxo


def saida(resposta):
    return {
        "messages": [
            HumanMessage(content="Quanto tenho?"),
            AIMessage(content=resposta),
        ],
        "jev_calls": {},
        "jev_events": [],
        "jev_halt": None,
    }


def executar(pergunta="Quanto tenho?", cancelado=None):
    marker = current_context.set(replace(context_for(), turn_id="t-stream"))
    try:
        return list(
            graph.executar_fluxo_chat_stream(
                pergunta, "s-live", context_for().identity.user_id, cancelado
            )
        )
    finally:
        current_context.reset(marker)


def test_ao_vivo_etapas_pensamento_e_texto_so_depois_do_grafo(monkeypatch):
    monkeypatch.setattr(eventos_do_fluxo, "INTERVALO_ENTRE_PEDACOS", 0)
    pensamento = AIMessageChunk(
        content=[{"type": "thinking", "thinking": "Vou somar os lotes."}]
    )
    resposta_parcial = AIMessageChunk(content=[{"type": "text", "text": "rascunho"}])
    fluxo = ao_vivo(
        monkeypatch,
        [
            ((), "tasks", {"id": "a", "name": "guardrail_insulto", "input": {}}),
            ((), "tasks", {"id": "b", "name": "roteador", "input": {}}),
            ((), "tasks", {"id": "b", "name": "roteador", "result": []}),
            # Nós internos do agente (subgrafo) não viram etapa do grafo principal.
            (("estoquista:9",), "tasks", {"id": "m", "name": "faq", "input": {}}),
            (
                ("estoquista:9",),
                "custom",
                {
                    "type": "etapa",
                    "id": "t1",
                    "status": "inicio",
                    "texto": "Consultando…",
                },
            ),
            (("estoquista:9",), "messages", (pensamento, {})),
            (("estoquista:9",), "messages", (resposta_parcial, {})),
            # AIMessage inteira (não chunk) já foi vista em pedaços: não repete o pensamento.
            (
                ("estoquista:9",),
                "messages",
                (AIMessage(content=[{"type": "thinking", "thinking": "repetido"}]), {}),
            ),
            (("estoquista:9",), "values", {"messages": []}),
            ((), "values", saida("Você tem 12 produtos em estoque.")),
        ],
    )
    eventos = executar()
    assert fluxo.modos == ["tasks", "custom", "messages", "values"]
    assert fluxo.subgraphs is True
    tipos = [e["type"] for e in eventos]
    assert tipos[:4] == ["etapa", "etapa", "etapa", "pensamento"]
    assert eventos[2]["texto"] == "Consultando…"
    assert eventos[0] == {
        "type": "etapa",
        "id": "b",
        "status": "inicio",
        "texto": "Entendendo o pedido…",
    }
    assert eventos[1]["texto"] == "Entendi o pedido"
    assert eventos[3] == {
        "type": "pensamento",
        "id": "estoquista",
        "delta": "Vou somar os lotes.",
    }
    # Rascunho do modelo (bloco de texto) nunca vira evento; o texto vem depois do grafo.
    assert "rascunho" not in json.dumps(eventos)
    assert "repetido" not in json.dumps(eventos)
    assert tipos.index("texto") > tipos.index("pensamento")
    assert (
        "".join(e["delta"] for e in eventos if e["type"] == "texto")
        .strip()
        .endswith("Você tem 12 produtos em estoque.")
    )
    assert eventos[-1] == {"type": "fim", "status": "aprovado"}


def test_resposta_segura_do_juiz_termina_como_segura(monkeypatch):
    monkeypatch.setattr(eventos_do_fluxo, "INTERVALO_ENTRE_PEDACOS", 0)
    parada = "Não consegui confirmar essa resposta com os dados consultados."
    ao_vivo(monkeypatch, [((), "values", {**saida(parada), "jev_halt": parada})])
    eventos = executar()
    assert eventos[-1] == {"type": "fim", "status": "segura"}
    assert (
        "".join(e["delta"] for e in eventos if e["type"] == "texto")
        .strip()
        .endswith(parada)
    )


def test_falha_no_meio_vira_evento_de_erro(monkeypatch):
    class Quebrado(FluxoFalso):
        def stream(self, estado, config=None, stream_mode=None, subgraphs=False):
            yield ((), "tasks", {"id": "b", "name": "roteador", "input": {}})
            raise RuntimeError("segredo-do-provedor")

    monkeypatch.setattr(graph, "fluxo_agentes", Quebrado([]))
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "_exigir_provedores", lambda: None)
    eventos = executar()
    assert eventos[-1]["type"] == "erro"
    assert "segredo-do-provedor" not in json.dumps(eventos)


def test_cliente_que_desiste_para_o_grafo(monkeypatch):
    from threading import Event

    consumidos = []

    class Longo(FluxoFalso):
        def stream(self, estado, config=None, stream_mode=None, subgraphs=False):
            for i in range(100):
                consumidos.append(i)
                yield ((), "tasks", {"id": str(i), "name": "roteador", "input": {}})

    cancelado = Event()
    cancelado.set()
    monkeypatch.setattr(graph, "fluxo_agentes", Longo([]))
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "_exigir_provedores", lambda: None)
    assert executar(cancelado=cancelado) == []
    assert len(consumidos) == 1


def test_pensamento_do_modelo_fica_fora_do_texto_da_resposta():
    mensagem = AIMessage(
        content=[
            {"type": "thinking", "thinking": "raciocínio interno"},
            {"type": "text", "text": "Resposta final."},
        ]
    )
    assert graph.extrair_texto(mensagem) == "Resposta final."


def test_fatiar_nao_quebra_palavras():
    texto = "Você tem doze produtos com estoque abaixo do mínimo hoje."
    pedacos = list(eventos_do_fluxo.fatiar(texto, 12))
    assert "".join(pedacos) == texto
    assert all(p.strip().count(" ") >= 0 and not p.startswith(" ") for p in pedacos)
