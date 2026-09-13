from langchain_core.runnables import RunnableLambda

import iai.app.guardrail as guardrail_module
from iai.app.guardrail import anonimizar_entrada, guardrail_entrada
from iai.app.schemas import ResultadoGuardrail


class _LlmFalso:
    def __init__(self, resultado=None, erro=None):
        self._resultado = resultado
        self._erro = erro

    def with_structured_output(self, *_a, **_k):
        def _executar(_entrada):
            if self._erro:
                raise self._erro
            return self._resultado

        return RunnableLambda(_executar)


def test_guardrail_entrada_aprova_quando_llm_libera(monkeypatch):
    resultado_fake = ResultadoGuardrail(bloqueado=False, motivo="dentro_do_escopo", mensagem="")
    monkeypatch.setattr(guardrail_module, "llm_rapido", _LlmFalso(resultado=resultado_fake))
    resultado = guardrail_entrada("quantos tomates temos em estoque?")
    assert resultado == {"bloqueado": False, "motivo": "dentro_do_escopo", "mensagem": ""}


def test_guardrail_entrada_falha_segura_quando_llm_da_erro(monkeypatch):
    monkeypatch.setattr(
        guardrail_module, "llm_rapido", _LlmFalso(erro=RuntimeError("falha simulada de API"))
    )
    resultado = guardrail_entrada("quantos tomates temos em estoque?")
    assert resultado["bloqueado"] is True
    assert resultado["motivo"] == "erro_api"


def test_anonimiza_email():
    texto = "meu email é joao@teste.com"
    resultado, mapa = anonimizar_entrada(texto)
    assert "joao@teste.com" not in resultado
    assert "[EMAIL_0]" in resultado
    assert mapa["[EMAIL_0]"] == "joao@teste.com"

def test_anonimiza_cpf():
    texto = "meu cpf é 123.456.789-00"
    resultado, _mapa = anonimizar_entrada(texto)
    assert "123.456.789-00" not in resultado
    assert "[CPF_0]" in resultado

def test_sem_pii_nao_altera_texto():
    texto = "quantos tomates temos em estoque?"
    resultado, mapa = anonimizar_entrada(texto)
    assert resultado == texto
    assert mapa == {}