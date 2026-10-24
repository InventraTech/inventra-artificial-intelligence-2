from iai.app.core.assistant.respostas import (
    descrever_proposta,
    mensagem_de_falha,
    resposta_de_propostas,
    resumo_para_log,
)


def proposta_de_requisicao():
    return {
        "id": "a1b2c3d4e5f6a7b8",
        "status": "NAO_EXECUTADA_SEM_CONFIRMACAO_HUMANA",
        "action": {
            "operation": "criar_requisicao",
            "parameters": {"type": "PURCHASE", "origin": "IAI"},
        },
        "before": [],
    }


def test_proposta_vira_texto_legivel_sem_json():
    texto = descrever_proposta(proposta_de_requisicao())

    assert "Criar requisição" in texto
    assert "Tipo: Compra" in texto
    assert "a1b2c3d4" in texto and "a1b2c3d4e5f6" not in texto
    assert "{" not in texto and '"' not in texto


def test_proposta_mostra_nome_do_produto_quando_o_snapshot_tem():
    proposta = {
        "id": "ff00ff00ff00",
        "status": "NAO_EXECUTADA_SEM_CONFIRMACAO_HUMANA",
        "action": {
            "operation": "adicionar_item_requisicao",
            "parameters": {"requisitionId": 7, "productId": 12, "quantity": "10"},
        },
        "before": [
            {"parameter": "productId", "record": {"id": 12, "name": "Picanha"}},
            {
                "parameter": "requisitionId",
                "record": {"id": 7, "status": "UNDER_REVIEW"},
            },
        ],
    }

    texto = descrever_proposta(proposta)

    assert "Produto: Picanha (nº 12)" in texto
    assert "Requisição: nº 7" in texto
    assert "Quantidade: 10" in texto


def test_resposta_de_propostas_avisa_que_nada_foi_alterado_e_lista_falhas():
    texto = resposta_de_propostas([proposta_de_requisicao()], ["CONFLITO: sem saldo"])

    assert "Nada foi alterado" in texto
    assert "Não consegui concluir" in texto and "CONFLITO: sem saldo" in texto


def test_mensagem_de_falha_explica_a_causa_sem_vazar_texto_do_sdk():
    class RateLimitError(Exception):
        pass

    mensagem, tipo = mensagem_de_falha(RateLimitError("key=AIzaSECRETO123"))

    assert "limite de uso" in mensagem
    assert tipo == "RateLimitError"
    assert "SECRETO" not in mensagem


def test_mensagem_de_falha_olha_a_cadeia_de_excecoes():
    erro = RuntimeError("wrapper")
    erro.__cause__ = TimeoutError("lento")

    assert "demorou demais" in mensagem_de_falha(erro)[0]


def test_mensagem_de_falha_por_status_http_e_limite_de_passos():
    erro = Exception("x")
    erro.status_code = 401  # type: ignore[attr-defined]
    assert "sem acesso ao provedor" in mensagem_de_falha(erro)[0]

    class GraphRecursionError(Exception):
        pass

    assert "passos demais" in mensagem_de_falha(GraphRecursionError())[0]


def test_erro_desconhecido_vira_mensagem_interna_com_codigo():
    mensagem, _ = mensagem_de_falha(ValueError("boom"))

    assert "erro interno" in mensagem and "ValueError" in mensagem


def test_resumo_para_log_mostra_cadeia_e_remove_segredos():
    erro = RuntimeError("falhou token=abc123segredo")
    erro.__cause__ = TimeoutError("lento")

    resumo = resumo_para_log(erro)

    assert "RuntimeError <- TimeoutError" in resumo
    assert "abc123segredo" not in resumo
