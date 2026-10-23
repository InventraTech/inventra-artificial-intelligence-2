import json
import os

import pytest
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

    assert "aprova" in resposta.lower()
    assert "unidade" in resposta.lower()


@PRECISA_CHAVES_LLM
def test_agente_raciocina_para_achar_a_pergunta_certa_no_jsonl(real_llm_context):
    resposta = executar_fluxo_assessor(
        "Qual o email de contato do suporte?",
        "test-faq-pergunta-reformulada",
        "demo-estoquista",
    )

    assert "inventra.oficial@gmail.com" in resposta
