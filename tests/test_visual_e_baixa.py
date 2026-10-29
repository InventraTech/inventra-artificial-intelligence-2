"""Cenários reportados em produção: gráfico que "não vai", baixa que não chega ao cartão.

LLM roteirizado: validam o fluxo e o backend, não a qualidade do provedor real.
"""

from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, HumanMessage

from iai.app.core.assistant import artifacts as a
from iai.app.core.assistant import graph, judge
from iai.app.core.assistant.continuidade import (
    LEMBRETE,
    SemPromessaVazia,
    eh_promessa_vazia,
)
from iai.app.core.assistant.tool_gate import JevToolGate
from iai.app.core.config import business_date
from iai.app.core.infra.security import current_context
from iai.app.core.inventra.business import (
    MockGateway,
    consumo_por_produto,
    gateway,
)
from iai.app.core.inventra.domain import Operation, Query, ToolResult
from iai.app.core.tools.operational import (
    PROPOSAL_TOOLS,
    consultar_inventra,
    planejar_baixa,
)
from tests_helpers import approved, context_for, jev_evaluation


class ScriptedModel(FakeMessagesListChatModel):
    def bind_tools(self, tools, **kwargs):
        return self


@pytest.fixture
def estoquista():
    marker = current_context.set(replace(context_for(), turn_id="visual-turn"))
    yield context_for()
    current_context.reset(marker)


def baixar(lote, quantidade):
    approved(Operation.CONSUME, {"batchId": lote, "quantity": str(quantidade)})


def plano(**argumentos):
    return ToolResult.model_validate_json(planejar_baixa.invoke(argumentos))


# ---------------------------------------------------------------- consumo (mais saem)
def test_consumo_estimado_sai_da_diferenca_entre_inicial_e_atual():
    lotes = [
        {
            "product": {"id": 1, "name": "Arroz", "unit": {"symbol": "kg"}},
            "initialQuantity": 50,
            "currentQuantity": 20,
        },
        {
            "product": {"id": 1, "name": "Arroz", "unit": {"symbol": "kg"}},
            "initialQuantity": 10,
            "currentQuantity": 10,
        },
        {
            "product": {"id": 2, "name": "Feijão"},
            "initialQuantity": 40,
            "currentQuantity": 5,
        },
        # sem quantidade inicial ou sem consumo: nada é inventado
        {"product": {"id": 3, "name": "Sal"}, "currentQuantity": 9},
        {
            "product": {"id": 4, "name": "Açúcar"},
            "initialQuantity": 5,
            "currentQuantity": 5,
        },
    ]

    ranking = consumo_por_produto(lotes, {2: {"symbol": "kg"}})

    assert [r["product"]["name"] for r in ranking] == ["Feijão", "Arroz"]
    assert [r["consumedQuantity"] for r in ranking] == [35.0, 30.0]
    assert ranking[0]["product"]["unit"] == {"symbol": "kg"}
    assert ranking[1]["currentQuantity"] == 30.0 and ranking[1]["batches"] == 2


def test_consulta_de_consumo_no_gateway_mock(estoquista):
    baixar(1, 20)
    baixar(2, 30)

    resultado = gateway().query(Query(resource="consumo"), estoquista)

    assert resultado.ok and isinstance(gateway(), MockGateway)
    assert [r["product"]["name"] for r in resultado.data] == ["Carne", "Arroz"]
    assert [r["consumedQuantity"] for r in resultado.data] == [30, 20]
    assert resultado.data[0]["product"]["unit"]["symbol"] == "kg"


def test_ranking_de_consumo_vira_barras_pizza_e_tabela(estoquista):
    baixar(1, 20)
    baixar(2, 30)
    outcome = gateway().query(Query(resource="consumo", size=100), estoquista)
    fonte = a.make_source("consumo", outcome)

    barras = a.build_artifact(a.PresentationChoice(kind="bar"), [fonte])
    pizza = a.build_artifact(a.PresentationChoice(kind="pie"), [fonte])
    tabela = a.build_artifact(a.PresentationChoice(kind="table"), [fonte])

    assert barras.labels == ["Carne", "Arroz"] and barras.datasets[0].data == [30, 20]
    assert barras.unit == "kg" and "Estimativa" in barras.note
    assert (
        pizza.chart_type == "pie" and pizza.datasets[0].label == "Quantidade consumida"
    )
    assert [c.key for c in tabela.columns][:2] == ["produto", "consumo"]
    texto = a.describe(barras, "consumo")
    assert "Carne, Arroz" not in texto and "Carne e Arroz" in texto
    assert "30" not in texto and "20" not in texto


# ----------------------------------------------------- barras/pizza com dados reais
def linhas_de_estoque(unidades):
    return [
        {
            "product": {"name": f"Produto {i:02d}", "unit": {"symbol": unidade}},
            "totalQuantity": 10 + i,
        }
        for i, unidade in enumerate(unidades)
    ]


def fonte(linhas, resource="estoque"):
    outcome = ToolResult(
        ok=True,
        code="OK",
        message="m",
        data=linhas,
        source="Spring",
        mocked=False,
    )
    return a.make_source(resource, outcome)


def test_barras_com_unidades_misturadas_usam_a_mais_comum_e_avisam():
    origem = fonte(linhas_de_estoque(["kg", "kg", "kg", "L", "un"]))

    grafico = a.build_artifact(a.PresentationChoice(kind="bar"), [origem])

    assert grafico.unit == "kg" and len(grafico.labels) == 3
    assert "2 itens ficaram de fora" in grafico.note and "L, un" in grafico.note


def linhas(*itens):
    return [
        {
            "product": {"name": nome, "unit": {"symbol": unidade}},
            "totalQuantity": quantidade,
            **({"minStock": minimo} if minimo is not None else {}),
        }
        for nome, unidade, quantidade, minimo in itens
    ]


def test_gramas_e_quilos_viram_uma_unidade_so_com_conta_exata():
    origem = fonte(
        linhas(
            ("Sal", "g", 500, None),
            ("Arroz", "kg", 2, None),
            ("Pimenta", "mg", 250000, None),
        )
    )

    grafico = a.build_artifact(a.PresentationChoice(kind="bar"), [origem])

    assert grafico.unit == "kg"
    assert dict(zip(grafico.labels, grafico.datasets[0].data)) == {
        "Sal": 0.5,
        "Arroz": 2,
        "Pimenta": 0.25,
    }
    assert "convertidos para kg" in grafico.note


def test_litros_e_mililitros_se_convertem_mas_nunca_para_quilos():
    origem = fonte(
        linhas(
            ("Leite", "L", 3, None),
            ("Óleo", "mL", 900, None),
            ("Arroz", "kg", 2, None),
        )
    )

    grafico = a.build_artifact(a.PresentationChoice(kind="bar"), [origem])

    assert grafico.unit == "L" and grafico.labels == ["Leite", "Óleo"]
    assert grafico.datasets[0].data == [3, 0.9]
    assert "densidade" in grafico.note and "1 item ficou de fora" in grafico.note


def test_estoque_minimo_acompanha_a_conversao_da_quantidade():
    origem = fonte(linhas(("Sal", "g", 500, 1000), ("Arroz", "kg", 2, 1)))

    grafico = a.build_artifact(a.PresentationChoice(kind="bar"), [origem])

    assert grafico.unit == "kg"
    assert [d.data for d in grafico.datasets] == [[0.5, 2], [1, 1]]


def test_nota_longa_e_cortada_para_nao_derrubar_o_grafico():
    assert len(a.join_notes("x" * 400)) == 300


def test_barras_nao_misturam_unidades_quando_nenhuma_tem_par():
    origem = fonte(linhas_de_estoque(["kg", "L"]))

    assert a.build_artifact(a.PresentationChoice(kind="bar"), [origem]) is None


def test_muitos_itens_viram_os_dez_maiores():
    origem = fonte(linhas_de_estoque(["kg"] * 40))

    grafico = a.build_artifact(a.PresentationChoice(kind="bar"), [origem])

    assert len(grafico.labels) == 10
    assert grafico.labels[0] == "Produto 39" and "10 maiores de 40" in grafico.note


def test_pizza_sem_categoria_mostra_participacao_na_mesma_unidade():
    origem = fonte(linhas_de_estoque(["kg", "kg", "kg"]))

    pizza = a.build_artifact(a.PresentationChoice(kind="pie"), [origem])

    assert pizza.chart_type == "pie" and pizza.unit == "kg"
    assert pizza.datasets[0].data == [10, 11, 12]


def test_pizza_continua_contando_registros_quando_ha_categoria():
    linhas = [
        {
            "product": {"name": n, "category": {"name": c}, "unit": {"symbol": "kg"}},
            "totalQuantity": 1,
        }
        for n, c in [("A", "Grãos"), ("B", "Carnes"), ("C", "Grãos")]
    ]

    pizza = a.build_artifact(a.PresentationChoice(kind="pie"), [fonte(linhas)])

    assert pizza.labels == ["Grãos", "Carnes"] and pizza.datasets[0].data == [2, 1]


# ------------------------------------------- o backend lê os dados quando o modelo não lê
@pytest.mark.parametrize(
    ("pergunta", "recurso"),
    [
        ("Me dê um gráfico dos produtos que mais saem", "consumo"),
        ("gráfico dos mais consumidos", "consumo"),
        ("pizza das requisições por status", "requisicoes"),
        ("tabela dos lotes que vencem", "validade"),
        ("gráfico de barras do estoque", "estoque"),
    ],
)
def test_recurso_padrao_do_pedido_visual(pergunta, recurso):
    assert a.resource_for(pergunta) == recurso


def test_pedido_visual_sem_consulta_do_modelo_le_e_desenha(estoquista):
    visual, texto = a.present(
        "Me dê um gráfico de barras do estoque",
        [HumanMessage(content="Me dê um gráfico de barras do estoque")],
        fetch_missing=True,
    )

    assert visual[0].chart_type == "bar" and visual[0].labels == [
        "Arroz",
        "Carne",
        "Tomate",
    ]
    assert "Não há registros" not in texto


def test_sem_fetch_missing_o_comportamento_antigo_e_preservado():
    visual, texto = a.present(
        "gráfico de barras do estoque", [HumanMessage(content="x")], demo=True
    )

    assert visual == [] and "Não há registros confirmados" in texto


def test_grafico_impossivel_degrada_para_tabela_com_aviso(monkeypatch):
    # unidades sem par: barras impossíveis, mas a tabela é possível
    mista = fonte(linhas_de_estoque(["kg", "L"]))
    monkeypatch.setattr(a, "fallback_sources", lambda question: [mista])

    visual, texto = a.present(
        "gráfico de barras do estoque", [HumanMessage(content="x")], fetch_missing=True
    )

    assert visual[0].type == "table"
    assert texto.startswith("Não deu para montar o gráfico")


def test_roteador_nao_chama_o_llm_para_pedido_visual(monkeypatch):
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(invoke=lambda *a, **kw: pytest.fail("não deveria rotear")),
    )

    saida = graph.no_roteador(
        {
            "messages": [
                HumanMessage(content="Me de uma grafico dos produtos que mais saem")
            ]
        },
        {},
    )

    assert saida["rota"] == "fim" and saida["agentes_chamados"] == ["roteador:visual"]


def test_acao_nunca_vira_atalho_visual(monkeypatch):
    chamou = []

    def router(*args, **kwargs):
        chamou.append(True)
        return {"messages": [AIMessage(content="ROUTE=estoquista")]}

    monkeypatch.setattr(graph, "router_app", SimpleNamespace(invoke=router))

    saida = graph.no_roteador(
        {
            "messages": [
                HumanMessage(content="dá baixa de 10 no arroz e mostra uma tabela")
            ]
        },
        {},
    )

    assert chamou and saida["rota"] == "estoquista"


def test_fluxo_completo_do_print_gera_grafico_mesmo_com_o_modelo_recusando(monkeypatch):
    """O especialista nem chega a rodar: o grafo termina no atalho e o backend desenha."""
    baixar(1, 20)
    baixar(2, 30)
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(invoke=lambda *a, **kw: pytest.fail("não deveria rotear")),
    )
    monkeypatch.setattr(
        graph,
        "estoquista_app",
        SimpleNamespace(invoke=lambda *a, **kw: pytest.fail("não deveria rodar")),
    )
    marker = current_context.set(replace(context_for(), turn_id="print-1"))
    try:
        resposta = graph.executar_fluxo_chat(
            "Me de Uma grafico dos produtos que mais Saem", "print", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)

    (grafico,) = resposta.artifacts
    assert grafico.chart_type == "bar" and grafico.labels == ["Carne", "Arroz"]
    assert "não consigo" not in resposta.resposta.lower()
    assert "Carne e Arroz" in resposta.resposta


# ------------------------------------------------------------------ planejar_baixa
def test_plano_de_baixa_acha_o_produto_pelo_nome(estoquista):
    resultado = plano(product_name="arroz", quantity="10")

    assert resultado.code == "PLANO_PRONTO"
    assert resultado.data["product"] == {"id": 1, "name": "Arroz", "unit": "kg"}
    assert [(i["batchId"], i["quantity"]) for i in resultado.data["plan"]] == [
        (1, "10")
    ]


def test_plano_aceita_a_unidade_dita_pelo_usuario_em_maiuscula(estoquista):
    resultado = plano(product_name="arroz", quantity="3", unit="KG")

    assert resultado.code == "PLANO_PRONTO"
    assert resultado.data["requested"] == "3"


def test_plano_converte_gramas_para_a_unidade_do_produto(estoquista):
    resultado = plano(product_name="arroz", quantity="2500", unit="g")

    assert resultado.code == "PLANO_PRONTO"
    assert resultado.data["requested"] == "2.5"
    assert [i["quantity"] for i in resultado.data["plan"]] == ["2.5"]


def test_plano_recusa_unidade_de_outra_grandeza_sem_chutar_densidade(estoquista):
    resultado = plano(product_name="arroz", quantity="2", unit="L")

    assert resultado.code == "UNIDADE_INCOMPATIVEL"
    assert "kg" in resultado.message
    assert "plan" not in (resultado.data or {})


def test_plano_usa_primeiro_o_lote_que_vence_antes_e_divide(estoquista):
    hoje = business_date()
    approved(
        Operation.CREATE_BATCH,
        {
            "productId": 1,
            "batchNumber": "L-CEDO",
            "initialQuantity": "5",
            "entryDate": hoje.isoformat(),
            "expirationDate": hoje.isoformat(),
        },
    )

    resultado = plano(product_name="Arroz", quantity="8")

    itens = resultado.data["plan"]
    assert [i["batchNumber"] for i in itens] == ["L-CEDO", "DEMO-1"]
    assert [i["quantity"] for i in itens] == ["5", "3"]


def test_plano_ignora_lote_vencido(estoquista):
    ontem = business_date() - timedelta(days=1)
    gateway().query(Query(resource="lotes"), estoquista)  # cria os dados de exemplo
    lote = get_lote(1)
    lote["expirationDate"] = ontem.isoformat()
    salvar_lote(lote)

    resultado = plano(product_name="Arroz", quantity="1")

    assert resultado.code == "SEM_SALDO"


def test_plano_informa_saldo_insuficiente_sem_propor(estoquista):
    resultado = plano(product_name="Arroz", quantity="500")

    assert resultado.code == "SALDO_INSUFICIENTE"
    assert (
        resultado.data["availableTotal"] == "50"
        and resultado.data["requested"] == "500"
    )


def test_plano_sem_lote_para_o_nome(estoquista):
    assert plano(product_name="Abobora cabotia", quantity="10").code == "SEM_LOTES"


def test_plano_com_busca_de_uma_letra_nao_chuta_nem_lista_tudo(estoquista):
    # Antes, "a" casava com todos os produtos e virava PRODUTO_AMBIGUO. Uma letra não é nome:
    # a ambiguidade de verdade (duas marcas de arroz) está em test_nome_confere.py.
    assert plano(product_name="a", quantity="1").code == "SEM_LOTES"


def test_plano_aceita_acentos_e_product_id(estoquista):
    assert plano(product_id=2, quantity="1").data["product"]["name"] == "Carne"
    assert plano(product_name="TOMATE", quantity="1").code == "PLANO_PRONTO"


def test_plano_exige_produto_e_quantidade_positiva(estoquista):
    for argumentos in ({"quantity": "1"}, {"product_name": "Arroz", "quantity": "0"}):
        assert "incompletos" in planejar_baixa.invoke(argumentos)


def get_lote(identificador):
    from iai.app.core.infra.storage import get_db

    return get_db().business_mock.find_one({"_id": f"1:batch:{identificador}"})["data"]


def salvar_lote(dados):
    from iai.app.core.infra.storage import get_db

    get_db().business_mock.update_one(
        {"_id": f"1:batch:{dados['id']}"}, {"$set": {"data": dados}}
    )


# ------------------------------------------------- "vou buscar" não pode ser resposta
@pytest.mark.parametrize(
    "texto",
    [
        (
            "Para dar baixa na abóbora cabotiá, preciso saber qual lote você quer usar.\n"
            "Primeiro, vou buscar os lotes disponíveis para a abóbora cabotiá."
        ),
        "Vou consultar o estoque agora.",
        "Um momento, vou verificar.",
        "Deixa eu procurar os lotes.",
        "Aguarde um instante.",
    ],
)
def test_promessa_de_acao_sem_ferramenta_e_detectada(texto):
    assert eh_promessa_vazia(AIMessage(content=texto))


@pytest.mark.parametrize(
    "texto",
    [
        "Quantos kg de arroz você usou?",
        "Qual lote você quer usar? Vou propor a baixa em seguida.",
        "Criei a proposta 1234 para dar baixa de 10 kg. Aguarda sua aprovação.",
        "Você tem 3 produtos: Arroz, Carne e Tomate.",
        "",
    ],
)
def test_resposta_final_ou_pergunta_nao_e_promessa(texto):
    assert not eh_promessa_vazia(AIMessage(content=texto))


def test_mensagem_com_ferramenta_nunca_e_promessa():
    chamada = {"id": "1", "name": "consultar_inventra", "args": {"resource": "estoque"}}

    assert not eh_promessa_vazia(AIMessage(content="Vou buscar.", tool_calls=[chamada]))


def agente_com(respostas, ferramentas):
    return create_agent(
        ScriptedModel(responses=respostas),
        tools=ferramentas,
        middleware=[JevToolGate("estoquista", ferramentas), SemPromessaVazia()],
    )


def test_modelo_que_promete_e_refeito_e_chama_a_ferramenta(monkeypatch, estoquista):
    ferramentas = [consultar_inventra, planejar_baixa]
    agente = agente_com(
        [
            AIMessage(content="Primeiro, vou buscar os lotes disponíveis."),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "plano",
                        "name": "planejar_baixa",
                        "args": {"product_name": "arroz", "quantity": 10},
                    }
                ],
            ),
            AIMessage(content="Achei o lote. Posso propor a baixa."),
        ],
        ferramentas,
    )
    monkeypatch.setattr(
        judge,
        "evaluate",
        lambda state: jev_evaluation(
            fields=state["tool"]["schema"].get("properties", {})
        ),
    )

    saida = agente.invoke({"messages": [HumanMessage(content="baixa de 10 no arroz")]})

    nomes = [m.name for m in saida["messages"] if m.type == "tool"]
    assert nomes == ["planejar_baixa"]
    # o lembrete é só da requisição refeita: não entra no histórico
    assert all(LEMBRETE not in str(m.content) for m in saida["messages"])
    # e a promessa original também não vira mensagem do histórico
    assert all("vou buscar" not in str(m.content) for m in saida["messages"])


def test_pergunta_legitima_nao_e_refeita(monkeypatch, estoquista):
    agente = agente_com(
        [AIMessage(content="Quantos kg de arroz você usou?")],
        [consultar_inventra, planejar_baixa],
    )

    saida = agente.invoke({"messages": [HumanMessage(content="dá baixa no arroz")]})

    assert saida["messages"][-1].content == "Quantos kg de arroz você usou?"


# --------------------------------------------- da conversa do print até o cartão (HITL)
def test_baixa_por_nome_chega_ao_cartao_de_aprovacao(monkeypatch):
    from iai.app.core.infra.storage import get_db

    ferramentas = [
        consultar_inventra,
        planejar_baixa,
        PROPOSAL_TOOLS[Operation.CONSUME],
    ]
    agente = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="Primeiro, vou buscar os lotes disponíveis para o arroz."
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "plano",
                            "name": "planejar_baixa",
                            "args": {"product_name": "arroz", "quantity": 10},
                        }
                    ],
                ),
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "proposta",
                            "name": "propor_registrar_saida",
                            "args": {
                                "batchId": 1,
                                "quantity": 10,
                                "justification": "Baixa de 10 kg de arroz pedida pelo usuário",
                            },
                        }
                    ],
                ),
                AIMessage(content="Preparei a proposta de baixa."),
            ]
        ),
        tools=ferramentas,
        middleware=[JevToolGate("estoquista", ferramentas), SemPromessaVazia()],
    )
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "estoquista_app", agente)
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {
                "messages": [AIMessage(content="ROUTE=estoquista")]
            }
        ),
    )
    monkeypatch.setattr(
        judge,
        "evaluate",
        lambda state: jev_evaluation(
            fields=state["tool"]["schema"].get("properties", {})
        ),
    )
    monkeypatch.setattr(
        graph,
        "orquestrador_app",
        SimpleNamespace(invoke=lambda *a, **kw: AIMessage(content="ok")),
    )
    marker = current_context.set(replace(context_for(), turn_id="hitl-1"))
    try:
        resposta = graph.executar_fluxo_chat(
            "baixa de 10 no arroz", "hitl", "demo-estoquista"
        )
    finally:
        current_context.reset(marker)

    (acao,) = resposta.pending_actions
    assert acao.operation == "registrar_saida"
    assert acao.parameters == {"batchId": 1, "quantity": "10"}
    assert acao.status == "AGUARDANDO_APROVACAO" and acao.before
    assert get_db().execution_mock.count_documents({}) == 0
    assert get_lote(1)["currentQuantity"] == 50


# ------------------------------------------ especialistas em paralelo (fan-out real)
def especialista_falso(nome, texto, barreira, ordem):
    def invoke(entrada, config=None):
        ordem.append(f"{nome}:inicio")
        barreira.wait(timeout=5)  # só passa se o OUTRO especialista também já começou
        ordem.append(f"{nome}:fim")
        return {"messages": [*entrada["messages"], AIMessage(content=texto)]}

    return SimpleNamespace(invoke=invoke)


def prepara_grafo_live(monkeypatch, rota):
    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(graph, "guardrail_insulto", lambda text: {"bloqueado": False})
    monkeypatch.setattr(graph, "guardrail_escopo", lambda text: {"bloqueado": False})
    monkeypatch.setattr(
        graph,
        "router_app",
        SimpleNamespace(
            invoke=lambda *a, **kw: {"messages": [AIMessage(content=f"ROUTE={rota}")]}
        ),
    )
    # Caminho de agente único: o orquestrador chamaria o LLM de verdade.
    monkeypatch.setattr(
        graph,
        "orquestrador_app",
        SimpleNamespace(invoke=lambda entrada: AIMessage(content="ok")),
    )


def conversa(pergunta, sessao):
    marker = current_context.set(replace(context_for(), turn_id=f"t-{sessao}"))
    try:
        return graph.executar_fluxo_chat(pergunta, sessao, "demo-estoquista")
    finally:
        current_context.reset(marker)


def test_estoquista_e_comprador_rodam_ao_mesmo_tempo_e_a_resposta_junta_os_dois(
    monkeypatch,
):
    from threading import Barrier

    ordem = []
    barreira = Barrier(2)
    prepara_grafo_live(monkeypatch, "estoquista,comprador")
    monkeypatch.setattr(
        graph,
        "estoquista_app",
        especialista_falso("e", "Arroz está abaixo do mínimo.", barreira, ordem),
    )
    monkeypatch.setattr(
        graph,
        "comprador_app",
        especialista_falso("c", "Há 2 requisições abertas.", barreira, ordem),
    )

    resposta = conversa(
        "O que está abaixo do mínimo e quais requisições abertas?", "par"
    )

    # Se rodassem em sequência, o primeiro esperaria 5 s pelo segundo e o teste falharia.
    assert sorted(ordem[:2]) == ["c:inicio", "e:inicio"]
    texto = resposta.resposta
    assert texto.index("**Estoque**") < texto.index("**Compras**")
    assert "Arroz está abaixo do mínimo." in texto and "2 requisições abertas" in texto


def test_pedido_de_acao_nunca_vai_para_o_paralelo(monkeypatch):
    prepara_grafo_live(monkeypatch, "estoquista,comprador")
    chamados = []
    for nome in ("supervisor_app", "estoquista_app", "comprador_app"):
        monkeypatch.setattr(
            graph,
            nome,
            SimpleNamespace(
                invoke=lambda entrada, config=None, n=nome: (
                    chamados.append(n)
                    or {"messages": [*entrada["messages"], AIMessage(content="ok")]}
                )
            ),
        )

    conversa("dá baixa no arroz e cria uma requisição de compra", "acao")

    assert chamados == ["supervisor_app"]


def test_combinacao_com_faq_cai_no_supervisor(monkeypatch):
    prepara_grafo_live(monkeypatch, "estoquista,faq")
    chamados = []
    monkeypatch.setattr(
        graph,
        "supervisor_app",
        SimpleNamespace(
            invoke=lambda entrada, config=None: (
                chamados.append("supervisor")
                or {"messages": [*entrada["messages"], AIMessage(content="ok")]}
            )
        ),
    )

    conversa("mostre o estoque e explique o estoque mínimo", "faq")

    assert chamados == ["supervisor"]


def test_uma_falha_de_um_especialista_nao_apaga_a_resposta_do_outro(monkeypatch):
    from threading import Barrier

    barreira = Barrier(2)
    prepara_grafo_live(monkeypatch, "estoquista,comprador")
    monkeypatch.setattr(
        graph, "estoquista_app", especialista_falso("e", "Estoque ok.", barreira, [])
    )
    monkeypatch.setattr(
        graph,
        "comprador_app",
        especialista_falso(
            "c", "A consulta está temporariamente indisponível.", barreira, []
        ),
    )

    texto = conversa("estoque e requisições", "falha").resposta

    assert "Estoque ok." in texto and "temporariamente indisponível" in texto
