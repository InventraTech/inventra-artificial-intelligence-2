import json
import os

import pytest

from iai.app.config import FAQ_PATH
from iai.app.graph import executar_fluxo_assessor
from iai.app.tools.faq import faq_retriever

PRECISA_CHAVES_LLM = pytest.mark.skipif(
    os.getenv("RUN_LLM_TESTS") != "1",
    reason=(
        "Chama o LLM de verdade (Gemini/Groq); o conftest.py força chaves fake para o "
        "restante da suite, então esse teste só roda com RUN_LLM_TESTS=1 e chaves reais "
        "exportadas no ambiente antes do pytest iniciar."
    ),
)


def _carregar_faq_bruto() -> list[dict]:
    with open(FAQ_PATH, encoding="utf-8") as arquivo:
        return [json.loads(linha) for linha in arquivo if linha.strip()]


def test_faq_retriever_traz_todas_as_perguntas_e_respostas_do_jsonl():
    entradas = _carregar_faq_bruto()
    resultado = faq_retriever.invoke("qualquer coisa")

    for entrada in entradas:
        assert f"P: {entrada['p']}" in resultado
        assert f"R: {entrada['r']}" in resultado


@PRECISA_CHAVES_LLM
def test_agente_responde_pergunta_identica_ao_jsonl():
    resposta = executar_fluxo_assessor(
        "Como cadastrar um produto novo no estoque?", "test-faq-pergunta-exata"
    )

    assert "produtos" in resposta.lower()
    assert "foto" in resposta.lower()


@PRECISA_CHAVES_LLM
def test_agente_raciocina_para_achar_a_pergunta_certa_no_jsonl():
    resposta = executar_fluxo_assessor(
        "Qual o email de contato do suporte?", "test-faq-pergunta-reformulada"
    )

    assert "inventra.oficial@gmail.com" in resposta
