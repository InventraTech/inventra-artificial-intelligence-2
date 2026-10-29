"""Filtro local por nome: "tomates" tem que achar "Tomate Cereja" (bug da cozinha do Davi)."""

from dataclasses import replace

import httpx
import pytest
from pydantic import SecretStr
from spring_fake import KITCHEN, USER_ID, FakeSpring

from iai.app.core.infra.retrieval import nome_confere, termos_de_busca_alternativos
from iai.app.core.infra.security import Identity, RequestContext, current_context
from iai.app.core.inventra.business import MockGateway, SpringGateway
from iai.app.core.inventra.domain import ToolResult
from iai.app.core.tools.operational import consultar_inventra, planejar_baixa
from tests_helpers import context_for


# ------------------------------------------------------------------ a função
@pytest.mark.parametrize(
    ("busca", "alvo"),
    [
        ("tomates", "Tomate Cereja"),  # plural na busca, nome completo no alvo
        ("tomate", "Tomate Cereja"),  # nome incompleto
        ("cereja", "Tomate Cereja"),  # palavra do meio/fim
        ("cereja tomate", "Tomate Cereja"),  # qualquer ordem
        ("tomate cereja", "Tomate Cereja"),  # nome inteiro
        ("tomates cereja", "Tomate Cereja"),
        ("Tomate Cereja", "tomates cereja"),  # plural no alvo
        ("abobora", "Abóbora cabotiá"),  # sem acento na busca
        ("Abóbora", "abobora cabotia"),  # acento só na busca
        ("ABOBORA CABOTIA", "Abóbora cabotiá"),  # maiúsculas
        ("limoes", "Limão Siciliano"),  # -ões -> -ão
        ("limao", "Limões"),
        ("paes", "Pão Francês"),
        ("ovos", "Ovo"),
        ("ovo", "Ovos caipira"),
        ("farinha de trigo", "Farinha de Trigo Tipo 1"),
        ("farinha trigo", "Farinha de Trigo Tipo 1"),  # stopword "de" é ignorada
        ("de farinha", "Farinha de Trigo Tipo 1"),
        ("tom", "Tomate Cereja"),  # prefixo com 3+ letras
        ("cer", "Tomate Cereja"),
        ("cerej", "Tomate Cereja"),
        ("5kg", "Arroz 5 kg"),  # número colado na unidade
        ("arroz 5", "Arroz 5 kg"),
        ("colheres", "Colher"),  # -es
        ("itens", "Item de teste"),  # -ns -> -m
    ],
)
def test_nome_confere_acha(busca, alvo):
    assert nome_confere(busca, alvo)


@pytest.mark.parametrize(
    ("busca", "alvo"),
    [
        ("arroz", "Feijão"),
        ("arroz", "Feijão Preto"),
        ("tomate", "Arroz"),
        ("tomates", "Arroz"),
        ("tomate molho", "Tomate Cereja"),  # TODAS as palavras são exigidas
        ("arroz feijao", "Arroz"),
        ("arroz 500", "Arroz 5000"),  # número não é prefixo de outro número
        ("500g", "Arroz 5000 g"),
        (
            "cereja",
            "Cerejeira",
        ),  # palavra inteira (ou prefixo) da PALAVRA do alvo, não trecho
        ("rroz", "Arroz"),  # trecho do meio não vale
        ("ss", "Massa"),
        ("tomatess", "Tomate"),  # plural inventado
    ],
)
def test_nome_confere_nao_acha(busca, alvo):
    assert not nome_confere(busca, alvo)


@pytest.mark.parametrize("busca", ["a", "e", "t", "to", "ar", "co", "1", "12"])
def test_busca_de_uma_ou_duas_letras_nao_casa_com_tudo(busca):
    alvos = ["Arroz", "Tomate Cereja", "Carne", "Abóbora cabotiá", "Leite", "Ovos"]
    assert not [alvo for alvo in alvos if nome_confere(busca, alvo)]


@pytest.mark.parametrize("busca", ["", "   ", "de", "de da do", "  de  ", "?!", None])
def test_busca_vazia_ou_so_stopword_nao_casa_com_nada(busca):
    # Decisão documentada em retrieval.py: sem palavra útil, nada casa (nunca "tudo").
    assert not nome_confere(busca, "Tomate Cereja")
    assert not nome_confere(busca, "")


def test_alvo_vazio_ou_ausente_nao_casa():
    assert not nome_confere("tomate", "")
    assert not nome_confere("tomate", None)


def test_termos_alternativos_sao_a_palavra_mais_longa_e_o_singular():
    termos = termos_de_busca_alternativos("tomates cereja")
    assert termos[0] == "tomates" or "tomate" in termos
    assert "tomates cereja" not in termos
    assert len(termos) <= 2
    assert termos_de_busca_alternativos("de") == []


# ---------------------------------------------------------- mock da cozinha
@pytest.fixture
def cozinha():
    marker = current_context.set(replace(context_for(), turn_id="nome-turn"))
    yield context_for()
    current_context.reset(marker)


def semear(produto_id, nome, quantidade, lote_id=None):
    """Produto + lote ativo na cozinha demo (id próprio, não mexe nos de exemplo)."""
    colecao = MockGateway()._collection(1)
    produto = {
        "id": produto_id,
        "name": nome,
        "unitId": 1,
        "unit": {"id": 1, "symbol": "kg"},
        "active": True,
        "minStock": 1,
    }
    lote = {
        "id": lote_id or produto_id,
        "product": {"id": produto_id, "name": nome},
        "supplier": {"id": 1, "legalName": "Fornecedor demonstração"},
        "batchNumber": f"T-{produto_id}",
        "currentQuantity": quantidade,
        "initialQuantity": quantidade,
        "entryDate": "2026-01-01",
        "expirationDate": "2999-01-01",
        "status": "ACTIVE",
    }
    for tipo, dados in (("product", produto), ("batch", lote)):
        colecao.update_one(
            {"_id": f"1:{tipo}:{dados['id']}"},
            {"$set": {"kitchen": 1, "kind": tipo, "data": dados}},
            upsert=True,
        )


def remover(nome):
    """Tira um produto de exemplo (e os lotes dele) para o cenário ficar só com o semeado."""
    colecao = MockGateway()._collection(1)  # semeia antes, senão o seed volta depois
    colecao.delete_many({"$or": [{"data.name": nome}, {"data.product.name": nome}]})


def consultar(**argumentos):
    return ToolResult.model_validate_json(consultar_inventra.invoke(argumentos))


def test_estoque_com_name_no_plural_acha_o_nome_completo(cozinha):
    semear(31, "Tomate Cereja", 4)

    saida = consultar(resource="estoque", name="tomates")

    assert saida.ok and saida.code == "OK"
    assert [r["product"]["name"] for r in saida.data] == ["Tomate", "Tomate Cereja"]


def test_estoque_com_name_so_acha_quem_casa(cozinha):
    semear(31, "Tomate Cereja", 4)

    assert consultar(resource="estoque", name="cereja").data[0]["product"]["id"] == 31
    assert consultar(resource="estoque", name="feijao").code == "VAZIO"
    assert consultar(resource="estoque", name="to").code == "VAZIO"


def test_lotes_tambem_usam_o_filtro_novo(cozinha):
    semear(31, "Tomate Cereja", 4)

    saida = consultar(resource="lotes", name="tomates cereja")

    assert [r["product"]["name"] for r in saida.data] == ["Tomate Cereja"]


def test_baixa_pelo_nome_incompleto_acha_tomate_cereja(cozinha):
    remover("Tomate")
    semear(31, "Tomate Cereja", 4)

    plano = ToolResult.model_validate_json(
        planejar_baixa.invoke({"product_name": "tomates", "quantity": "2"})
    )

    assert plano.code == "PLANO_PRONTO"
    assert plano.data["product"]["name"] == "Tomate Cereja"


def test_baixa_com_duas_marcas_nao_escolhe_a_primeira(cozinha):
    remover("Arroz")
    semear(41, "Arroz Tio João", 10)
    semear(42, "Arroz Camil", 10)

    plano = ToolResult.model_validate_json(
        planejar_baixa.invoke({"product_name": "arroz", "quantity": "1"})
    )

    assert plano.code == "PRODUTO_AMBIGUO"
    assert {c["name"] for c in plano.data["candidatos"]} == {
        "Arroz Tio João",
        "Arroz Camil",
    }
    # com o ID escolhido pelo usuário, o plano sai
    escolhido = ToolResult.model_validate_json(
        planejar_baixa.invoke({"product_id": 42, "quantity": "1"})
    )
    assert escolhido.code == "PLANO_PRONTO"


def test_baixa_com_nome_exato_entre_varias_marcas_usa_o_exato(cozinha):
    remover("Arroz")
    semear(41, "Arroz", 10)
    semear(42, "Arroz Integral", 10)

    plano = ToolResult.model_validate_json(
        planejar_baixa.invoke({"product_name": "arroz", "quantity": "1"})
    )

    assert plano.code == "PLANO_PRONTO"
    assert plano.data["product"]["name"] == "Arroz"


def test_baixa_com_busca_de_uma_letra_nao_chuta_produto(cozinha):
    plano = ToolResult.model_validate_json(
        planejar_baixa.invoke({"product_name": "a", "quantity": "1"})
    )

    assert plano.code == "SEM_LOTES"


# ------------------------------------------------- gateway real (Spring de mentira)
@pytest.fixture
def spring(monkeypatch):
    from iai.app.core.config import business_date
    from iai.app.core.inventra import approvals  # noqa: F401  (mesmo patch dos e2e)
    from iai.app.core.tools import operational

    fake = FakeSpring(today=business_date())
    monkeypatch.setattr(httpx, "request", fake)
    monkeypatch.setattr(operational, "gateway", lambda: SpringGateway())
    ctx = RequestContext(
        Identity(user_id=USER_ID, kitchen_id=KITCHEN, role="SUPERVISOR", name="Davi"),
        SecretStr("token-do-usuario"),
        turn_id="t1",
    )
    marker = current_context.set(ctx)
    yield fake
    current_context.reset(marker)


def chamadas_produtos(fake):
    return [c for c in fake.calls if c[1] == "/api/products"]


def test_produtos_com_plural_tenta_o_singular_e_acha(spring):
    saida = consultar(resource="produtos", name="leites", size=7)

    assert saida.ok and saida.code == "OK"
    assert [p["name"] for p in saida.data["content"]] == ["Leite"]
    tentativas = chamadas_produtos(spring)
    assert tentativas[0][2]["name"] == "leites"
    assert tentativas[-1][2]["name"] == "leite"
    assert all(t[2]["size"] == 7 for t in tentativas)  # o tamanho da página se mantém


def test_produtos_sem_acento_tenta_sem_name_e_filtra_localmente(spring):
    saida = consultar(resource="produtos", name="abobora")

    assert saida.code == "OK"
    assert [p["name"] for p in saida.data["content"]] == ["Abóbora cabotiá"]
    assert "name" not in chamadas_produtos(spring)[-1][2]


def test_produtos_sem_correspondencia_nao_vaza_o_catalogo(spring):
    saida = consultar(resource="produtos", name="acucar")

    assert saida.code == "VAZIO"
    assert saida.data["content"] == []
    assert (
        len(chamadas_produtos(spring)) <= 4
    )  # busca inteira + até 2 termos + sem name


def test_produtos_que_acha_de_primeira_nao_repete_a_chamada(spring):
    saida = consultar(resource="produtos", name="leite")

    assert saida.code == "OK"
    assert len(chamadas_produtos(spring)) == 1


def test_produtos_continua_exigindo_name(spring):
    saida = consultar(resource="produtos")

    assert not saida.ok and saida.code == "USE_ESTOQUE"
    assert chamadas_produtos(spring) == []


def test_estoque_da_cozinha_real_acha_por_nome_parcial(spring):
    saida = consultar(resource="estoque", name="aboboras")

    assert saida.ok
    assert [r["product"]["name"] for r in saida.data] == ["Abóbora cabotiá"]


def test_busca_ampla_so_vale_para_produtos(spring):
    consultar(resource="estoque", name="xyz")

    assert chamadas_produtos(spring) == []
