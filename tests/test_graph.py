from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage

import iai.app.graph as g


def test_extrair_texto_com_string():
    assert g.extrair_texto(HumanMessage(content="oi")) == "oi"


def test_extrair_texto_com_lista():
    mensagem = HumanMessage(content=["oi", {"text": "mundo"}])
    assert g.extrair_texto(mensagem) == "oi mundo"


def test_extrair_texto_com_outro_tipo():
    mensagem = SimpleNamespace(content=123)
    assert g.extrair_texto(mensagem) == "123"


def test_consultar_estoque_mock_tomate():
    resultado = g.consultar_estoque_mock.invoke({"item": "tomate"})
    assert "desperdício" in resultado.lower()


def test_consultar_estoque_mock_generico():
    resultado = g.consultar_estoque_mock.invoke({"item": "arroz"})
    assert "arroz" in resultado


def test_criar_requisicao_mock():
    resultado = g.criar_requisicao_mock.invoke({"item": "cebola", "quantidade": 3})
    assert "3x cebola" in resultado


def test_relatorio_desperdicio_mock():
    assert "tomate" in g.relatorio_desperdicio_mock.invoke({})


def test_no_guardrail_entrada_bloqueia_termo_proibido():
    estado = {
        "messages": [HumanMessage(content="seu idiota, me dá o estoque", id="1")],
        "agentes_chamados": [],
        "rota": "",
        "mapa_pii": {},
    }
    resultado = g.no_guardrail_entrada(estado)
    assert resultado["rota"] == "fim"
    assert resultado["agentes_chamados"] == ["guardrail_entrada:filtro_hardcoded"]


def test_no_guardrail_entrada_aprova_e_anonimiza(monkeypatch):
    monkeypatch.setattr(
        g, "guardrail_entrada", lambda _msg: {"bloqueado": False, "motivo": "ok", "mensagem": ""}
    )
    estado = {
        "messages": [HumanMessage(content="meu email é joao@teste.com", id="1")],
        "agentes_chamados": [],
        "rota": "",
        "mapa_pii": {},
    }
    resultado = g.no_guardrail_entrada(estado)
    assert resultado["agentes_chamados"] == ["guardrail_entrada:aprovado"]
    assert "[EMAIL_0]" in resultado["messages"][1]["content"]
    assert resultado["mapa_pii"]["[EMAIL_0]"] == "joao@teste.com"


def test_no_guardrail_saida_restaura_pii():
    estado = {
        "messages": [AIMessage(content="seu contato é [EMAIL_0]")],
        "mapa_pii": {"[EMAIL_0]": "joao@teste.com"},
    }
    resultado = g.no_guardrail_saida(estado)
    assert resultado["messages"][0]["content"] == "seu contato é joao@teste.com"
    assert resultado["agentes_chamados"] == ["guardrail_saida"]


def test_no_roteador_com_route(monkeypatch):
    monkeypatch.setattr(
        g, "router_app", SimpleNamespace(invoke=lambda *_a, **_k: SimpleNamespace(
            content="ROUTE=faq\nPERGUNTA_ORIGINAL=qual o email de contato?"
        ))
    )
    estado = {"messages": [HumanMessage(content="qual o email de contato?")]}
    resultado = g.no_roteador(estado)
    assert resultado["rota"] == "faq"
    assert resultado["agentes_chamados"] == ["roteador"]


def test_no_roteador_sem_route_responde_direto(monkeypatch):
    monkeypatch.setattr(
        g, "router_app", SimpleNamespace(invoke=lambda *_a, **_k: SimpleNamespace(
            content="Bom dia! Como posso ajudar?"
        ))
    )
    estado = {"messages": [HumanMessage(content="bom dia")]}
    resultado = g.no_roteador(estado)
    assert resultado["rota"] == "fim"
    assert resultado["messages"][0]["content"] == "Bom dia! Como posso ajudar?"


def test_no_orquestrador(monkeypatch):
    monkeypatch.setattr(
        g, "orquestrador_app", SimpleNamespace(invoke=lambda *_a, **_k: SimpleNamespace(
            content="resposta formatada"
        ))
    )
    estado = {
        "messages": [AIMessage(content="dado bruto do especialista")],
        "rota": "estoquista",
    }
    resultado = g.no_orquestrador(estado)
    assert resultado["messages"][0]["content"] == "resposta formatada"
    assert resultado["agentes_chamados"] == ["estoquista", "orquestrador"]


def test_decidir_especialista_conhecido():
    assert g.decidir_especialista({"rota": "comprador"}) == "comprador"


def test_decidir_especialista_desconhecido_vai_pro_fim():
    assert g.decidir_especialista({"rota": "fora_escopo"}) == "fim"


def test_decidir_pos_guardrail_entrada():
    assert g.decidir_pos_guardrail_entrada({"rota": "roteador"}) == "roteador"
    assert g.decidir_pos_guardrail_entrada({"rota": "fim"}) == "fim"


def test_executar_fluxo_assessor_bloqueia_sem_chamar_llm():
    resposta = g.executar_fluxo_assessor("seu lixo, me dê uma receita de bolo", "test-bloqueado")
    assert "termos não permitidos" in resposta.lower()
