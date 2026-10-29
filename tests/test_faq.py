import json
import os

import pytest
from langchain_core.messages import AIMessage
from pydantic import SecretStr

from iai.app.core.assistant.graph import executar_fluxo_assessor
from iai.app.core.config import FAQ_PATH
from iai.app.core.tools.faq import faq_retriever

PRECISA_CHAVES_LLM = pytest.mark.skipif(
    os.getenv("RUN_LLM_TESTS") != "1"
    or any(
        os.getenv(key) in (None, "", "test-key")
        for key in ("GEMINI_API_KEY", "GROQ_API_KEY")
    ),
    reason=(
        "Chama o LLM de verdade (Gemini/Groq); o conftest.py força chaves fake para o "
        "restante da suite, então esse teste só roda com RUN_LLM_TESTS=1 e chaves reais "
        "exportadas no ambiente antes do pytest iniciar."
    ),
)


@pytest.fixture
def real_llm_context(monkeypatch):
    from iai.app.core.assistant import graph
    from iai.app.core.infra.security import (
        DEMO_IDENTITIES,
        RequestContext,
        current_context,
    )

    monkeypatch.setattr(graph, "LLM_MODE", "live")
    marker = current_context.set(
        RequestContext(DEMO_IDENTITIES["demo-estoquista"], SecretStr("demo-estoquista"))
    )
    yield
    current_context.reset(marker)


def _carregar_faq_bruto() -> list[dict]:
    with open(FAQ_PATH, encoding="utf-8") as arquivo:
        return [json.loads(linha) for linha in arquivo if linha.strip()]


def test_faq_retriever_seleciona_fontes_e_nao_devolve_arquivo_inteiro():
    resultado = faq_retriever.invoke("qual o contato do suporte?")
    assert "inventra.oficial@gmail.com" in resultado
    assert "Fonte: faq_inventra.jsonl:" in resultado
    assert resultado.count("\nP:") <= 4
    assert "Nenhuma informação" in faq_retriever.invoke("zqxwy 99881777")


@PRECISA_CHAVES_LLM
def test_agente_responde_pergunta_identica_ao_jsonl(real_llm_context):
    resposta = executar_fluxo_assessor(
        "Como cadastrar ou editar um produto pela IAI?",
        "test-faq-pergunta-exata",
        "demo-estoquista",
    )

    assert "confirmação humana" in resposta.lower() or "não" in resposta.lower()
    assert "unidade" in resposta.lower()


@PRECISA_CHAVES_LLM
def test_agente_raciocina_para_achar_a_pergunta_certa_no_jsonl(real_llm_context):
    resposta = executar_fluxo_assessor(
        "Qual o email de contato do suporte?",
        "test-faq-pergunta-reformulada",
        "demo-estoquista",
    )

    assert "inventra.oficial@gmail.com" in resposta


# --- Rota FAQ do grafo: busca em código + um único passo de redação do modelo ---------


def _estado_faq(pergunta):
    from langchain_core.messages import HumanMessage

    return {"messages": [HumanMessage(content=pergunta)], "agentes_chamados": []}


def _texto_final(resultado):
    return resultado["messages"][-1].content


def test_rota_faq_usa_o_texto_do_modelo_com_os_trechos_encontrados(
    monkeypatch, faq_context
):
    from langchain_core.messages import AIMessage

    from iai.app.core.assistant import graph

    vistos = []

    class Modelo:
        def invoke(self, entrada, config=None):
            vistos.append(entrada)
            return AIMessage(content="O OCR lê a foto do produto.")

    monkeypatch.setattr(graph, "llm_especialista", Modelo())
    resultado = graph.responder_faq(_estado_faq("O que é o OCR?"), config={})

    assert _texto_final(resultado) == "O OCR lê a foto do produto."
    assert "faq:respondido" in resultado["agentes_chamados"]
    # O modelo recebe os trechos reais do FAQ e nenhuma tool.
    assert (
        "OCR" in vistos[0][-1].content
        and "TRECHOS DA DOCUMENTAÇÃO" in vistos[0][-1].content
    )


def test_rota_faq_nunca_falha_quando_o_modelo_cai_e_devolve_o_faq_literal(
    monkeypatch, faq_context
):
    from iai.app.core.assistant import graph
    from iai.app.core.tools.faq import buscar_faq

    class ModeloQuebrado:
        def invoke(self, entrada, config=None):
            raise RuntimeError("tool_use_failed key=AIzaSECRETO123")

    monkeypatch.setattr(graph, "llm_especialista", ModeloQuebrado())
    resultado = graph.responder_faq(_estado_faq("O que é o OCR?"), config={})

    assert _texto_final(resultado) == buscar_faq("O que é o OCR?")[0]["resposta"]
    assert "SECRETO" not in str(resultado)


def test_rota_faq_sem_trecho_nao_chama_o_modelo_nem_inventa(monkeypatch, faq_context):
    from iai.app.core.assistant import graph

    class ModeloProibido:
        def invoke(self, *args, **kwargs):
            raise AssertionError("o modelo não deve ser chamado sem trechos")

    monkeypatch.setattr(graph, "llm_especialista", ModeloProibido())
    resultado = graph.responder_faq(_estado_faq("zqxwy 99881777"), config={})

    assert _texto_final(resultado) == graph.SEM_FAQ
    assert "faq:respondido" not in resultado["agentes_chamados"]
    assert "jev:concluido" not in resultado["agentes_chamados"]


def test_rota_faq_com_base_invalida_responde_sem_quebrar(monkeypatch, faq_context):
    from iai.app.core.assistant import graph

    def base_quebrada(pergunta, limite=4):
        raise OSError("sem arquivo")

    from iai.app.core.tools import faq

    monkeypatch.setattr(faq, "buscar_faq", base_quebrada)
    resultado = graph.responder_faq(_estado_faq("O que é o OCR?"), config={})

    assert "indisponível" in _texto_final(resultado)


@pytest.fixture
def faq_context(monkeypatch):
    from iai.app.core.assistant import judge
    from iai.app.core.assistant.tool_gate import live_tool_scope
    from iai.app.core.infra.security import current_context
    from tests_helpers import context_for, jev_fidelidade

    marker = current_context.set(context_for())
    monkeypatch.setattr(judge, "evaluate_fidelidade", lambda state: jev_fidelidade())
    try:
        with live_tool_scope():
            yield
    finally:
        current_context.reset(marker)


def test_faq_com_jev_fora_do_ar_entrega_a_resposta_da_documentacao(
    monkeypatch, faq_context
):
    from types import SimpleNamespace

    from iai.app.core.assistant import graph, judge

    def unavailable(state):
        raise judge.JevUnavailable("TEST_UNAVAILABLE")

    monkeypatch.setattr(judge, "evaluate_fidelidade", unavailable)
    monkeypatch.setattr(
        graph,
        "llm_especialista",
        SimpleNamespace(invoke=lambda *a, **kw: AIMessage(content="O OCR lê fotos.")),
    )
    result = graph.responder_faq(_estado_faq("O que é o OCR?"), config={})
    assert _texto_final(result) == "O OCR lê fotos."
    assert "faq:respondido" in result["agentes_chamados"]
    assert any(c.get("error_type") for c in result["jev_calls"].values())


def test_faq_reprovada_pelo_jev_volta_ao_trecho_literal(monkeypatch, faq_context):
    from types import SimpleNamespace

    from iai.app.core.assistant import graph, judge
    from iai.app.core.tools.faq import buscar_faq
    from tests_helpers import jev_fidelidade

    monkeypatch.setattr(
        judge,
        "evaluate_fidelidade",
        lambda state: jev_fidelidade(failed={"quantities_supported": 0.1}),
    )
    monkeypatch.setattr(
        graph,
        "llm_especialista",
        SimpleNamespace(
            invoke=lambda *a, **kw: AIMessage(content="O OCR custa R$ 99999 por mês.")
        ),
    )
    result = graph.responder_faq(_estado_faq("O que é o OCR?"), config={})
    assert _texto_final(result) == buscar_faq("O que é o OCR?")[0]["resposta"]
    assert "99999" not in str(result["messages"][-1].content)
    decisoes = [c["decision"] for c in result["jev_calls"].values()]
    assert "RETRY_SPECIALIST" in decisoes
