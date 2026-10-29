"""Revisão do turno pelo JEV (revisao.py): agente falso, JEV roteirizado, Mongo e regras reais."""

import json
from dataclasses import replace

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from iai.app.core.assistant import judge, revisao
from iai.app.core.infra.security import current_context
from iai.app.core.infra.storage import get_db
from iai.app.core.inventra.approvals import cancelar_pelo_juiz, list_proposals
from iai.app.core.inventra.domain import Operation
from iai.app.core.tools.operational import PROPOSAL_TOOLS
from tests_helpers import context_for, jev_evaluation, jev_fidelidade

PROPOSTA = PROPOSAL_TOOLS[Operation.CONSUME]
FERRAMENTAS = {PROPOSTA.name: PROPOSTA}
PEDIDO = "Usei 10 kg de arroz"


@pytest.fixture(autouse=True)
def contexto():
    marker = current_context.set(replace(context_for(), turn_id="turno-1"))
    yield
    current_context.reset(marker)


class AgenteFalso:
    """Cada chamada a invoke consome uma fábrica de resposta; guarda o que recebeu."""

    def __init__(self, *fabricas):
        self.fabricas = list(fabricas)
        self.entradas = []

    def invoke(self, entrada, config=None):
        self.entradas.append(entrada["messages"])
        return self.fabricas.pop(0)(entrada["messages"])


def propoe(quantidade="10", identificador="p1", batch=1):
    """Cria uma proposta de verdade (pendente no Mongo) e devolve as mensagens do turno."""
    texto = PROPOSTA.invoke(
        {"batchId": batch, "quantity": quantidade, "justification": "Consumo informado"}
    )
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": identificador,
                    "name": PROPOSTA.name,
                    "args": {
                        "batchId": batch,
                        "quantity": quantidade,
                        "justification": "Consumo informado",
                    },
                }
            ],
        ),
        ToolMessage(content=texto, name=PROPOSTA.name, tool_call_id=identificador),
    ]


def resposta_de(mensagens, final="Proposta criada.", **extras):
    return {"messages": [*mensagens, AIMessage(content=final)], **extras}


def entrada():
    return [HumanMessage(content=PEDIDO)]


def escreve(quantidade="10", identificador="p2", final="Proposta criada."):
    """A proposta nasce quando o especialista roda (no contexto do turno de repetição)."""

    def fabrica(msgs):
        return resposta_de([*msgs, *propoe(quantidade, identificador)], final)

    return fabrica


def visiveis():
    return get_db().approvals.count_documents({"judge_cancelled": {"$ne": True}})


def canceladas():
    return get_db().approvals.count_documents({"judge_cancelled": True})


def roteiro_jev(monkeypatch, *avaliacoes):
    """JEV de escrita: devolve as avaliações em ordem (a última se repete) e guarda os estados."""
    estados = []
    restantes = list(avaliacoes)

    def evaluate(state):
        estados.append(state)
        item = restantes.pop(0) if len(restantes) > 1 else restantes[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(judge, "evaluate", evaluate)
    return estados


def aprovada():
    return jev_evaluation(fields=["batchId", "quantity", "justification"])


def reparo():
    return jev_evaluation(
        failed={"arguments_grounded": 0.1},
        route="agent_repair",
        fields=["batchId", "quantity", "justification"],
    )


def pergunta_ao_usuario():
    return jev_evaluation(
        failed={"missing_user_information": 0.99},
        route="user_input",
        missing="quantity",
        fields=["batchId", "quantity", "justification"],
    )


def revisa(agente, resposta, ent=None):
    return revisao.revisar(
        "estoquista", agente, ent or entrada(), resposta, {}, FERRAMENTAS
    )


# ---- escrita ----


def test_escrita_aprovada_mantem_a_proposta_e_chama_o_jev_uma_vez(monkeypatch):
    estados = roteiro_jev(monkeypatch, aprovada())
    agente = AgenteFalso()
    resposta = revisa(agente, resposta_de([*entrada(), *propoe()]))
    assert len(estados) == 1 and not agente.entradas
    assert visiveis() == 1 and canceladas() == 0
    assert not resposta.get("jev_halt")
    (registro,) = resposta["jev_calls"].values()
    assert registro["tool"] == PROPOSTA.name and registro["decision"] == "APPROVE"
    assert registro["duration_ms"] >= 0 and registro["model"].startswith("jev-")


def test_jev_recebe_a_proposta_ja_criada_sem_segredos(monkeypatch):
    estados = roteiro_jev(monkeypatch, aprovada())
    ctx = current_context.get()
    revisa(AgenteFalso(), resposta_de([*entrada(), *propoe()]))
    (estado,) = estados
    assert estado["candidate"]["status"] == "PROPOSAL_PENDING"
    assert estado["user_request"] == PEDIDO
    assert estado["tool"]["name"] == PROPOSTA.name
    assert estado["tool"]["arguments"]["quantity"] == "10"
    assert ctx.token.get_secret_value() not in str(estado)


def test_escrita_reprovada_cancela_e_o_especialista_repete_uma_vez(monkeypatch):
    estados = roteiro_jev(monkeypatch, reparo(), aprovada())
    agente = AgenteFalso(escreve(final="Proposta refeita."))
    resposta = revisa(agente, resposta_de([*entrada(), *propoe()]))
    assert len(estados) == 2
    assert [e["candidate"]["attempt"] for e in estados] == [1, 2]
    # A primeira proposta foi cancelada; a nova (mesma ação) tem outra chave de idempotência.
    assert visiveis() == 1 and canceladas() == 1
    assert not resposta.get("jev_halt")
    # O especialista viu o motivo e a proposta cancelada, e a instrução não vai para o estado final.
    (recebido,) = agente.entradas
    instrucao = recebido[-1].content
    assert "confirme produto, lote e fornecedor" in instrucao
    assert "Não mencione esta revisão" in instrucao
    assert revisao.CODIGO_CANCELADA in str([m.content for m in recebido])
    assert all("Revisão interna" not in str(m.content) for m in resposta["messages"])
    assert [r["attempt"] for r in resposta["jev_calls"].values()] == [1, 2]


def test_escrita_reprovada_duas_vezes_termina_com_recusa(monkeypatch):
    roteiro_jev(monkeypatch, reparo())
    agente = AgenteFalso(escreve())
    resposta = revisa(agente, resposta_de([*entrada(), *propoe()]))
    assert len(agente.entradas) == 1  # só uma volta
    assert visiveis() == 0 and canceladas() == 2
    assert resposta["jev_halt"] and "não foi realizada" in resposta["jev_halt"]
    assert resposta["messages"][-1].content == resposta["jev_halt"]
    assert "JEV" not in resposta["jev_halt"]


def test_jev_pede_dado_ao_usuario_sem_repetir_o_especialista(monkeypatch):
    roteiro_jev(monkeypatch, pergunta_ao_usuario())
    agente = AgenteFalso()
    resposta = revisa(agente, resposta_de([*entrada(), *propoe()]))
    assert not agente.entradas
    assert visiveis() == 0 and canceladas() == 1
    assert "quantidade e a unidade" in resposta["jev_halt"]


def test_jev_fora_do_ar_cancela_a_proposta_e_registra_o_erro(monkeypatch):
    roteiro_jev(monkeypatch, judge.JevUnavailable("Bearer private-server-secret"))
    resposta = revisa(AgenteFalso(), resposta_de([*entrada(), *propoe()]))
    assert visiveis() == 0 and canceladas() == 1
    assert "indisponível" in resposta["jev_halt"]
    (registro,) = resposta["jev_calls"].values()
    assert registro["error_type"] == "JevUnavailable"
    assert "private-server-secret" not in str(resposta)


def test_sem_chave_do_jev_a_escrita_nao_chega_ao_usuario(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(judge, "TYPESAFE_API_KEY", SecretStr(""))
    resposta = revisa(AgenteFalso(), resposta_de([*entrada(), *propoe()]))
    assert visiveis() == 0 and resposta["jev_halt"]


def test_reprovacao_parcial_mantem_a_aprovada_e_pergunta_sobre_a_outra(monkeypatch):
    def evaluate(state):
        if state["tool"]["arguments"]["batchId"] == 2:
            return pergunta_ao_usuario()
        return aprovada()

    monkeypatch.setattr(judge, "evaluate", evaluate)
    agente = AgenteFalso()
    mensagens = [*entrada(), *propoe("10", "p1", 1), *propoe("40", "p2", 2)]
    resposta = revisa(agente, resposta_de(mensagens))
    assert not agente.entradas  # repetir duplicaria o que o usuário vai ver
    assert visiveis() == 1 and canceladas() == 1
    assert "quantidade e a unidade" in resposta["jev_halt"]
    propostas = revisao.propostas_do_turno(resposta["messages"][1:])
    assert len(propostas) == 1  # a cancelada deixou de parecer proposta


def test_varias_propostas_sao_julgadas_ao_mesmo_tempo(monkeypatch):
    from threading import Barrier

    barreira = Barrier(2)

    def evaluate(state):
        barreira.wait(timeout=5)  # só passa se as duas chamadas estiverem em andamento
        return aprovada()

    monkeypatch.setattr(judge, "evaluate", evaluate)
    mensagens = [*entrada(), *propoe("10", "p1", 1), *propoe("5", "p2", 2)]
    resposta = revisa(AgenteFalso(), resposta_de(mensagens))
    assert visiveis() == 2 and not resposta.get("jev_halt")
    assert len(resposta["jev_calls"]) == 2


def test_proposta_cancelada_some_das_listas_do_usuario(monkeypatch):
    roteiro_jev(monkeypatch, pergunta_ao_usuario())
    revisa(AgenteFalso(), resposta_de([*entrada(), *propoe()]))
    assert list_proposals(current_context.get())["total"] == 0
    (doc,) = get_db().approvals.find({})
    assert doc["status"] == "RECUSADA" and doc["decided_by"] == "juiz"
    assert doc["audit"][-1]["event"] == "CANCELADA_PELO_JUIZ"


def test_cancelar_so_mexe_em_proposta_pendente_do_solicitante():
    ctx = current_context.get()
    mensagens = propoe()
    identificador = json.loads(mensagens[1].content)["proposal"]["id"]
    outro = replace(context_for("demo-comprador"), turn_id="outro")
    assert cancelar_pelo_juiz([identificador], outro, "x") == 0
    assert cancelar_pelo_juiz([identificador], ctx, "x") == 1
    assert cancelar_pelo_juiz([identificador], ctx, "x") == 0


# ---- leitura ----


def leitura(texto="Você tem 12 de arroz."):
    return [
        AIMessage(
            content="",
            tool_calls=[
                {
                    "id": "r1",
                    "name": "consultar_inventra",
                    "args": {"resource": "lotes"},
                }
            ],
        ),
        ToolMessage(
            content='{"ok": true, "data": [{"produto": "Arroz", "quantidade": "12"}]}',
            name="consultar_inventra",
            tool_call_id="r1",
        ),
        AIMessage(content=texto),
    ]


def roteiro_texto(monkeypatch, *avaliacoes):
    estados = []
    restantes = list(avaliacoes)

    def evaluate(state):
        estados.append(state)
        item = restantes.pop(0) if len(restantes) > 1 else restantes[0]
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(judge, "evaluate_fidelidade", evaluate)
    return estados


def revisa_leitura(
    agente, texto="Você tem 12 de arroz.", pergunta="quanto tenho de arroz?"
):
    ent = [HumanMessage(content=pergunta)]
    return revisao.revisar(
        "estoquista", agente, ent, {"messages": [*ent, *leitura(texto)]}, {}, {}
    )


def test_leitura_aprovada_entrega_o_texto_e_registra_a_decisao(monkeypatch):
    estados = roteiro_texto(monkeypatch, jev_fidelidade())
    resposta = revisa_leitura(AgenteFalso())
    assert resposta["messages"][-1].content == "Você tem 12 de arroz."
    (registro,) = resposta["jev_calls"].values()
    assert registro["tool"] == "resposta_final" and registro["decision"] == "APPROVE"
    (estado,) = estados
    assert estado["answer"] == "Você tem 12 de arroz."
    assert estado["evidence"][0]["tool"] == "consultar_inventra"


def test_leitura_reprovada_volta_ao_especialista_com_o_motivo_e_os_numeros(monkeypatch):
    roteiro_texto(
        monkeypatch,
        jev_fidelidade(failed={"quantities_supported": 0.1}),
        jev_fidelidade(),
    )
    agente = AgenteFalso(
        lambda msgs: {"messages": [*msgs, AIMessage(content="Você tem 12 de arroz.")]}
    )
    resposta = revisa_leitura(agente, "Você tem 99 de arroz.")
    (recebido,) = agente.entradas
    instrucao = recebido[-1].content
    assert "use só quantidades" in instrucao
    assert "99" in instrucao and "12" not in instrucao.split("números", 1)[1]
    assert resposta["messages"][-1].content == "Você tem 12 de arroz."
    assert not resposta.get("jev_halt")
    assert all("Revisão interna" not in str(m.content) for m in resposta["messages"])


def test_leitura_reprovada_duas_vezes_termina_sem_afirmar_nada(monkeypatch):
    roteiro_texto(monkeypatch, jev_fidelidade(failed={"products_supported": 0.1}))
    agente = AgenteFalso(
        lambda msgs: {"messages": [*msgs, AIMessage(content="Há 99 de feijão.")]}
    )
    resposta = revisa_leitura(agente, "Você tem 99 de arroz.")
    assert len(agente.entradas) == 1
    assert resposta["jev_halt"] == revisao.FRASE_SEM_APOIO
    assert resposta["messages"][-1].content == revisao.FRASE_SEM_APOIO


def test_leitura_com_jev_fora_do_ar_entrega_o_texto(monkeypatch):
    roteiro_texto(monkeypatch, judge.JevUnavailable("JEV_HTTP_500"))
    resposta = revisa_leitura(AgenteFalso())
    assert resposta["messages"][-1].content == "Você tem 12 de arroz."
    assert not resposta.get("jev_halt")
    (registro,) = resposta["jev_calls"].values()
    assert registro["error_type"] == "JevUnavailable"


def test_saudacao_sem_evidencia_nao_chama_o_jev(monkeypatch):
    def proibido(state):
        pytest.fail("O JEV não deve julgar uma saudação.")

    monkeypatch.setattr(judge, "evaluate_fidelidade", proibido)
    ent = [HumanMessage(content="oi")]
    resposta = revisao.revisar(
        "estoquista",
        AgenteFalso(),
        ent,
        {"messages": [*ent, AIMessage(content="Olá! Como posso ajudar?")]},
        {},
        {},
    )
    assert resposta["jev_calls"] == {}


def test_resposta_com_numero_e_sem_nenhuma_consulta_e_julgada(monkeypatch):
    estados = roteiro_texto(
        monkeypatch, jev_fidelidade(failed={"no_unverified_claims": 0.1})
    )
    ent = [HumanMessage(content="quanto tenho de arroz?")]
    agente = AgenteFalso(
        lambda msgs: {"messages": [*msgs, AIMessage(content="Preciso consultar.")]}
    )
    resposta = revisao.revisar(
        "estoquista",
        agente,
        ent,
        {"messages": [*ent, AIMessage(content="Você tem 99 de arroz.")]},
        {},
        {},
    )
    assert estados[0]["evidence"][0]["result"].startswith("No tool was consulted")
    assert resposta["messages"][-1].content == "Preciso consultar."


def test_pedido_de_grafico_nao_gasta_chamada_no_texto(monkeypatch):
    def proibido(state):
        pytest.fail("O texto de um gráfico é montado pelo backend.")

    monkeypatch.setattr(judge, "evaluate_fidelidade", proibido)
    revisa_leitura(AgenteFalso(), pergunta="Mostre o estoque em gráfico de barras")


def test_parada_do_gate_local_nao_e_julgada(monkeypatch):
    def proibido(state):
        pytest.fail("O gate já encerrou o turno.")

    monkeypatch.setattr(judge, "evaluate_fidelidade", proibido)
    monkeypatch.setattr(judge, "evaluate", proibido)
    resposta = revisao.revisar(
        "estoquista",
        AgenteFalso(),
        entrada(),
        {"messages": [*entrada(), AIMessage(content="Parei.")], "jev_halt": "Parei."},
        {},
        {},
    )
    assert resposta["jev_halt"] == "Parei."


def test_numeros_sem_apoio_so_viram_dica_nao_decisao(monkeypatch):
    # Mesmo com um número "novo" o veredito é do JEV: aprovado continua aprovado.
    roteiro_texto(monkeypatch, jev_fidelidade())
    resposta = revisa_leitura(AgenteFalso(), "Você tem 24 (12 + 12) de arroz.")
    assert not resposta.get("jev_halt")
