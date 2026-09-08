from iai.app.guardrail import anonimizar_entrada


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