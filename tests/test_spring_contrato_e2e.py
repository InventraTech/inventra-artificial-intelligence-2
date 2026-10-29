"""Ponta a ponta contra um Spring fiel ao contrato real: cartão -> aprovação -> banco muda.

Cada uma das 16 operações é proposta, aprovada e executada, e o teste confere o ESTADO do
"banco" do Spring de mentira depois. As leituras passam pelas formas reais de resposta.
"""

import uuid
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from iai.app.core.config import business_date
from iai.app.core.infra.security import Identity, RequestContext, current_context
from iai.app.core.inventra import approvals
from iai.app.core.inventra.business import SpringGateway
from iai.app.core.inventra.domain import (
    Action,
    DecisionInput,
    ProposalInput,
    Query,
)
from iai.app.core.tools import operational
from tests.spring_fake import KITCHEN, USER_ID, FakeSpring


@pytest.fixture(params=[False, True], ids=["nulos", "sem-nulos"])
def spring(request, monkeypatch):
    fake = FakeSpring(today=business_date(), omit_nulls=request.param)
    monkeypatch.setattr(httpx, "request", fake)
    monkeypatch.setattr(approvals, "BUSINESS_MODE", "spring")
    for modulo in (approvals, operational):
        monkeypatch.setattr(modulo, "gateway", lambda: SpringGateway())
    ctx = RequestContext(
        Identity(user_id=USER_ID, kitchen_id=KITCHEN, role="SUPERVISOR", name="Davi"),
        SecretStr("token-do-usuario"),
        turn_id="t1",
    )
    marker = current_context.set(ctx)
    yield fake
    current_context.reset(marker)


def ctx():
    return current_context.get()


def rodar(operacao, parametros):
    """Propõe, aprova e devolve o documento final da proposta."""
    proposta = approvals.create_proposal(
        ProposalInput(
            action=Action(operation=operacao, parameters=parametros),
            justification="Teste ponta a ponta",
            idempotency_key=str(uuid.uuid4()),
        ),
        ctx(),
    )
    assert proposta["status"] == "AGUARDANDO_APROVACAO"
    return approvals.decide(
        proposta["id"],
        DecisionInput(decision="approve", proposal_hash=proposta["proposal_hash"]),
        ctx(),
    )


def concluir(operacao, parametros):
    doc = rodar(operacao, parametros)
    assert doc["status"] == "CONCLUIDA", (
        operacao,
        doc["status"],
        doc.get("execution_error"),
        doc.get("result"),
    )
    return doc


# --------------------------------------------------------------------- leituras
RECURSOS_SEM_ID = [
    "produtos",
    "estoque",
    "lotes",
    "validade",
    "estoque_minimo",
    "fornecedores",
    "fornecedores_recorrentes",
    "requisicoes",
    "inventarios",
    "alertas",
    "categorias",
    "unidades",
    "cozinha",
    "consumo",
]


@pytest.mark.parametrize("recurso", RECURSOS_SEM_ID)
def test_leituras_aceitam_as_formas_reais_de_resposta(spring, recurso):
    out = SpringGateway().query(Query(resource=recurso), ctx())

    assert out.ok, (recurso, out.code, out.message)
    assert out.code in {"OK", "VAZIO"}


def test_leituras_que_pedem_id(spring):
    gw = SpringGateway()
    assert gw.query(Query(resource="parametros_produto", product_id=1), ctx()).ok
    assert gw.query(Query(resource="estoque", product_id=2), ctx()).ok
    assert gw.query(Query(resource="fornecedores_produto", product_id=1), ctx()).ok
    assert gw.query(Query(resource="itens_requisicao", record_id=1), ctx()).ok
    assert gw.query(Query(resource="requisicoes", mine=True), ctx()).ok
    assert gw.query(Query(resource="requisicoes", status="DRAFT"), ctx()).ok


def test_estoque_da_cozinha_lista_o_total_por_produto(spring):
    out = SpringGateway().query(Query(resource="estoque"), ctx())

    nomes = {linha["product"]["name"]: linha["totalQuantity"] for linha in out.data}
    assert nomes["Abóbora cabotiá"] == 38.0


# ------------------------------------------------------- as 16 operações, E2E
def test_produto_do_cartao_ao_banco(spring):
    criado = concluir("criar_produto", {"name": "ZZ Teste", "unitId": 1})
    pid = criado["result"]["data"]["id"]
    assert (
        spring.products[pid]["name"] == "ZZ Teste" and spring.products[pid]["unit"] == 1
    )

    concluir("editar_produto", {"productId": pid, "name": "ZZ Teste 2"})
    assert spring.products[pid]["name"] == "ZZ Teste 2"

    concluir("desativar_produto", {"productId": pid})
    assert spring.products[pid]["active"] is False


def test_fornecedor_do_cartao_ao_banco(spring):
    criado = concluir(
        "cadastrar_fornecedor",
        {"legalName": "ZZ Fornecedor", "cnpj": "11.222.333/0002-62"},
    )
    sid = criado["result"]["data"]["id"]
    assert spring.suppliers[sid]["legalName"] == "ZZ Fornecedor"

    concluir("desativar_fornecedor", {"supplierId": sid})
    assert spring.suppliers[sid]["active"] is False


def test_lote_entrada_saida_e_ajuste_do_cartao_ao_banco(spring):
    hoje = business_date()
    lote = concluir(
        "criar_lote",
        {
            "productId": 1,
            "batchNumber": "L-ZZ1",
            "initialQuantity": "10",
            "entryDate": hoje.isoformat(),
            "expirationDate": (hoje + timedelta(days=30)).isoformat(),
        },
    )
    bid = lote["result"]["data"]["id"]
    assert spring.batches[bid]["current"] == 10

    concluir("registrar_entrada", {"batchId": bid, "quantity": "5"})
    assert spring.batches[bid]["current"] == 15

    concluir("registrar_saida", {"batchId": bid, "quantity": "3"})
    assert spring.batches[bid]["current"] == 12

    concluir("ajustar_estoque", {"batchId": bid, "newQuantity": "8"})
    assert spring.batches[bid]["current"] == 8
    tipos = [m["type"] for m in spring.movements if m["batch"] == bid]
    assert tipos == ["ENTRY", "ENTRY", "WRITE_OFF", "ADJUSTMENT"]


def test_requisicao_item_remocao_e_cancelamento_do_cartao_ao_banco(spring):
    criada = concluir("criar_requisicao", {"type": "PURCHASE", "origin": "IAI"})
    rid = criada["result"]["data"]["id"]
    assert spring.requisitions[rid]["status"] == "DRAFT"

    concluir(
        "adicionar_item_requisicao",
        {"requisitionId": rid, "productId": 1, "quantity": "5"},
    )
    item = next(i for i, v in spring.items.items() if v["req"] == rid)
    assert spring.items[item]["quantity"] == 5

    concluir("remover_item_requisicao", {"requisitionId": rid, "itemId": item})
    assert item not in spring.items

    concluir("cancelar_requisicao", {"requisitionId": rid, "reason": "teste"})
    assert spring.requisitions[rid]["status"] == "CANCELLED"
    assert spring.requisitions[rid]["reason"] == "teste"


def test_inventario_contagem_e_alteracao_de_contagem_do_cartao_ao_banco(spring):
    aberto = concluir("registrar_inventario", {})
    iid = aberto["result"]["data"]["id"]
    assert spring.inventories[iid]["status"] == "OPEN"

    contada = concluir(
        "registrar_contagem",
        {"inventoryId": iid, "batchId": 1, "physicalQuantity": "28"},
    )
    cid = contada["result"]["data"]["id"]
    assert spring.counts[cid]["physical"] == 28

    concluir(
        "alterar_contagem",
        {"inventoryId": iid, "countId": cid, "batchId": 1, "physicalQuantity": "29"},
    )
    contagens = [c for c in spring.counts.values() if c["inv"] == iid]
    assert [c["physical"] for c in contagens] == [29]
    assert len(contagens) == 1


def test_alterar_contagem_restaura_a_antiga_se_a_nova_falhar(spring, monkeypatch):
    iid = concluir("registrar_inventario", {})["result"]["data"]["id"]
    cid = concluir(
        "registrar_contagem",
        {"inventoryId": iid, "batchId": 1, "physicalQuantity": "28"},
    )["result"]["data"]["id"]
    original = spring.__call__
    estado = {"posts": 0}

    def quebra_o_primeiro_post(method, url, **kw):
        if method == "POST" and url.endswith(f"/inventories/{iid}/counts"):
            estado["posts"] += 1
            if estado["posts"] == 1:
                return httpx.Response(500)
        return original(method, url, **kw)

    monkeypatch.setattr(httpx, "request", quebra_o_primeiro_post)

    doc = rodar(
        "alterar_contagem",
        {"inventoryId": iid, "countId": cid, "batchId": 1, "physicalQuantity": "29"},
    )

    assert doc["status"] != "CONCLUIDA"
    restantes = [c["physical"] for c in spring.counts.values() if c["inv"] == iid]
    assert restantes == [28]  # a contagem original voltou


# --------------------------------------------------- baixa pelo nome, ponta a ponta
def test_baixa_pelo_nome_gera_um_cartao_por_lote_e_baixa_no_banco(spring):
    plano = operational.planejar_baixa.invoke(
        {"product_name": "abóbora cabotiá", "quantity": 20, "unit": "kg"}
    )
    dados = __import__("json").loads(plano)
    assert dados["code"] == "PLANO_PRONTO"
    itens = dados["data"]["plan"]
    assert [(i["batchId"], i["quantity"]) for i in itens] == [(2, "8"), (3, "12")]

    for item in itens:
        concluir(
            "registrar_saida",
            {"batchId": item["batchId"], "quantity": item["quantity"]},
        )

    assert (
        spring.batches[2]["current"] == 0
        and spring.batches[2]["status"] == "WRITTEN_OFF"
    )
    assert spring.batches[3]["current"] == 18
    baixas = [m for m in spring.movements if m["type"] == "WRITE_OFF"]
    assert sum(m["quantity"] for m in baixas) == 20


def test_baixa_em_lote_vencido_nem_vira_cartao_e_nao_muda_o_banco(spring):
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as erro:
        approvals.create_proposal(
            ProposalInput(
                action=Action(
                    operation="registrar_saida",
                    parameters={"batchId": 5, "quantity": "1"},
                ),
                justification="Teste",
                idempotency_key=str(uuid.uuid4()),
            ),
            ctx(),
        )

    assert erro.value.status_code == 409 and "vencido" in erro.value.detail
    assert spring.batches[5]["current"] == 5


# -------------------------------------------------------- consumo real (movimentações)
def test_consumo_usa_as_baixas_reais_do_historico(spring):
    from iai.app.core.inventra.business import CONSUMO_REAL

    concluir("registrar_saida", {"batchId": 2, "quantity": "3"})
    concluir("registrar_saida", {"batchId": 3, "quantity": "2"})
    concluir("registrar_saida", {"batchId": 1, "quantity": "4"})

    out = SpringGateway().query(Query(resource="consumo"), ctx())

    assert out.ok and out.message == CONSUMO_REAL
    ranking = {r["product"]["name"]: r["consumedQuantity"] for r in out.data}
    assert ranking == {"Abóbora cabotiá": 5.0, "Arroz": 4.0}
    assert out.data[0]["product"]["unit"]["symbol"] == "kg"


def test_consumo_sem_historico_cai_na_estimativa_pelos_lotes(spring):
    out = SpringGateway().query(Query(resource="consumo"), ctx())

    assert out.ok and out.message.startswith("Consumo estimado")


# ------------------------------------------- Gemini preenche opcionais com "", 0 e null
def test_consulta_com_campos_vazios_vale_como_sem_filtro():
    consulta = Query.model_validate(
        {
            "resource": "estoque",
            "product_id": 0,
            "record_id": 0,
            "name": "",
            "status": None,
            "days": 7,
        }
    )

    assert consulta.product_id is None and consulta.name is None


def test_proposta_com_fornecedor_zero_e_nota_vazia_nao_quebra():
    acao = Action(
        operation="adicionar_item_requisicao",
        parameters={
            "requisitionId": 1,
            "productId": 2,
            "quantity": "3",
            "suggestedSupplierId": 0,
            "note": "",
        },
    )

    assert acao.parameters == {"requisitionId": 1, "productId": 2, "quantity": "3"}


def test_ajustar_para_zero_continua_valendo():
    acao = Action(
        operation="ajustar_estoque", parameters={"batchId": 1, "newQuantity": "0"}
    )

    assert acao.parameters["newQuantity"] == "0"
