from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import iai.app.judge as judge_module
from iai.app.judge import (
    FATOS_VAZIOS,
    LIMIAR_CONFIANCA_JUIZ,
    avaliar_alucinacao,
    extrair_pergunta_fatos_e_resposta,
    ultima_mensagem_ai,
)
from iai.app.schemas import ResultadoJuiz


def test_extrair_pergunta_fatos_e_resposta_rodada_simples():
    mensagens = [
        HumanMessage(content="quantos tomates temos?"),
        ToolMessage(content="Temos 5kg de tomate.", tool_call_id="1"),
        AIMessage(content="Você tem 5kg de tomate."),
    ]
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert pergunta == "quantos tomates temos?"
    assert fatos == "Temos 5kg de tomate."
    assert resposta == "Você tem 5kg de tomate."


def test_extrair_pergunta_fatos_e_resposta_sem_tool_fica_com_fatos_vazio():
    mensagens = [
        HumanMessage(content="cria uma requisição de cebola"),
        AIMessage(content="Requisição criada com sucesso."),
    ]
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert pergunta == "cria uma requisição de cebola"
    assert fatos == ""
    assert resposta == "Requisição criada com sucesso."


def test_extrair_pergunta_fatos_e_resposta_ignora_mensagem_de_correcao_do_juiz():
    mensagens = [
        HumanMessage(content="quantos tomates temos?"),
        ToolMessage(content="Temos 5kg de tomate.", tool_call_id="1"),
        AIMessage(content="Você tem 50kg de tomate."),
        HumanMessage(content="corrija isso", additional_kwargs={"juiz_feedback": True}),
        AIMessage(content="Você tem 5kg de tomate."),
    ]
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert pergunta == "quantos tomates temos?"
    assert fatos == "Temos 5kg de tomate."
    assert resposta == "Você tem 5kg de tomate."


def test_extrair_pergunta_fatos_e_resposta_junta_varios_fatos_em_ordem():
    mensagens = [
        HumanMessage(content="pergunta"),
        ToolMessage(content="fato 1", tool_call_id="1"),
        ToolMessage(content="fato 2", tool_call_id="2"),
        AIMessage(content="resposta"),
    ]
    _pergunta, fatos, _resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert fatos == "fato 1\nfato 2"


def test_ultima_mensagem_ai_encontra_a_mais_recente():
    mensagens = [
        AIMessage(content="primeira"),
        HumanMessage(content="oi"),
        AIMessage(content="segunda"),
    ]
    assert ultima_mensagem_ai(mensagens).content == "segunda"


def test_ultima_mensagem_ai_sem_nenhuma_retorna_none():
    assert ultima_mensagem_ai([HumanMessage(content="oi")]) is None


def test_avaliar_alucinacao_retorna_resultado_do_chain(monkeypatch):
    resultado_fake = ResultadoJuiz(confianca=0.9, motivo="dados batem com os fatos")
    monkeypatch.setattr(
        judge_module, "juiz_chain", SimpleNamespace(invoke=lambda _entrada: resultado_fake)
    )
    resultado = avaliar_alucinacao("pergunta", "fatos", "resposta")
    assert resultado == resultado_fake


def test_avaliar_alucinacao_falha_segura_quando_chain_da_erro(monkeypatch):
    def _explode(_entrada):
        raise RuntimeError("falha simulada de API")

    monkeypatch.setattr(judge_module, "juiz_chain", SimpleNamespace(invoke=_explode))
    resultado = avaliar_alucinacao("pergunta", "fatos", "resposta")
    assert resultado.confianca == 0.0
    assert resultado.motivo == "erro_api_juiz"


def test_limiar_confianca_juiz_e_0_7():
    assert LIMIAR_CONFIANCA_JUIZ == 0.7


def test_fatos_vazios_explica_que_nenhuma_tool_foi_chamada():
    assert "nenhuma ferramenta" in FATOS_VAZIOS.lower()
