from langchain.agents import create_agent
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from iai.app.guardrail import (
    anonimizar_entrada,
    guardrail_escopo,
    guardrail_insulto,
    guardrail_saida,
)
from iai.app.llms import llm_especialista, llm_rapido
from iai.app.memory import iniciar_sessao, salvar_mensagem
from iai.app.prompts import (
    COMPRADOR_SYSTEM_PROMPT,
    ESTOQUISTA_SYSTEM_PROMPT,
    FAQ_SYSTEM_PROMPT,
    ORQUESTRADOR_SYSTEM_PROMPT,
    ROTEADOR_SYSTEM_PROMPT,
    SUPERVISOR_SYSTEM_PROMPT,
)
from iai.app.schemas import Estado
from iai.app.tools.faq import faq_retriever
from iai.app.tools.memoria import TOOLS_MEMORIA


def extrair_texto(mensagem: BaseMessage) -> str:
    """Normaliza o content de uma BaseMessage (str ou list) para str puro."""
    conteudo = mensagem.content
    if isinstance(conteudo, str):
        return conteudo
    if isinstance(conteudo, list):
        return " ".join(
            item if isinstance(item, str) else str(item.get("text", ""))
            for item in conteudo
        )
    return str(conteudo)


@tool
def consultar_estoque_mock(item: str) -> str:
    """Consulta a quantidade e a validade de um item no estoque."""
    if "tomate" in item.lower():
        return "Temos 5kg de tomate. Validade: amanhã (Risco de desperdício alto)."
    return f"Temos 10 unidades de {item} em estoque. Validade longa."

@tool
def criar_requisicao_mock(item: str, quantidade: int) -> str:
    """Cria uma requisição de compra no sistema."""
    return f"Requisição de {quantidade}x {item} criada com sucesso (ID: 998)."

@tool
def relatorio_desperdicio_mock() -> str:
    """Gera um panorama de itens críticos."""
    return "Relatório: 5kg de tomate vencem amanhã. 2L de leite vencem em 2 dias."

router_app = create_agent(
    model=llm_rapido,
    tools=TOOLS_MEMORIA,
    system_prompt=ROTEADOR_SYSTEM_PROMPT
)

orquestrador_prompt = ChatPromptTemplate.from_messages([
    ("system", ORQUESTRADOR_SYSTEM_PROMPT), 
    ("human", "{mensagens}")
])
orquestrador_app = orquestrador_prompt | llm_rapido

estoquista_app = create_agent(  # type: ignore[call-overload]
    model=llm_especialista,
    tools=[consultar_estoque_mock] + TOOLS_MEMORIA,
    system_prompt=ESTOQUISTA_SYSTEM_PROMPT
)

comprador_app = create_agent(  # type: ignore[call-overload]
    model=llm_especialista,
    tools=[criar_requisicao_mock] + TOOLS_MEMORIA,
    system_prompt=COMPRADOR_SYSTEM_PROMPT
)

supervisor_app = create_agent(  # type: ignore[call-overload]
    model=llm_especialista,
    tools=[relatorio_desperdicio_mock] + TOOLS_MEMORIA,
    system_prompt=SUPERVISOR_SYSTEM_PROMPT
)

faq_app = create_agent(
    model=llm_rapido,
    tools=[faq_retriever],
    system_prompt=FAQ_SYSTEM_PROMPT
)

def no_guardrail_insulto(estado: Estado) -> dict:
    mensagem_original = list(estado["messages"])[-1]
    texto_original = extrair_texto(mensagem_original)

    texto_anonimizado, mapa = anonimizar_entrada(texto_original)
    resultado = guardrail_insulto(texto_anonimizado)

    if resultado["bloqueado"]:
        return {
            "messages":         [{"role": "assistant", "content": resultado["mensagem"]}],
            "rota":             "fim",
            "mapa_pii":         mapa,
            "agentes_chamados": [f"guardrail_insulto:{resultado['motivo']}"]
        }

    if mensagem_original.id is None:
        raise ValueError("Mensagem sem ID, não é possível remover")

    return {
        "messages": [
            RemoveMessage(id=mensagem_original.id),
            {"role": "human", "content": texto_anonimizado}
        ],
        "mapa_pii":         mapa,
        "agentes_chamados": ["guardrail_insulto:aprovado"],
    }

def no_guardrail_escopo(estado: Estado) -> dict:
    texto = extrair_texto(estado["messages"][-1])
    resultado = guardrail_escopo(texto)

    if resultado["bloqueado"]:
        return {
            "messages":         [{"role": "assistant", "content": resultado["mensagem"]}],
            "rota":             "fim",
            "agentes_chamados": [f"guardrail_escopo:{resultado['motivo']}"]
        }

    return {
        "agentes_chamados": ["guardrail_escopo:aprovado"],
    }

def no_guardrail_saida(estado: Estado) -> dict:
    ultima = ""
    for msg in reversed(estado["messages"]):
        if msg.type == "ai" and msg.content:
            ultima = extrair_texto(msg)
            break

    resultado = guardrail_saida(ultima, estado.get("mapa_pii"), {})

    return {
       "messages":         [{"role": "assistant", "content": resultado["conteudo"]}],
       "agentes_chamados": ["guardrail_saida"]
    }

def no_roteador(estado: Estado, config: RunnableConfig) -> dict:
    ultima_mensagem = extrair_texto(estado["messages"][-1])
    saida = router_app.invoke({"messages": [HumanMessage(content=ultima_mensagem)]}, config=config)

    texto = extrair_texto(saida["messages"][-1])

    if "ROUTE=" not in texto:
        return {
            "agentes_chamados": ["roteador"],
            "rota":             "fim",
            "messages":         [{"role": "assistant", "content": texto}],
        }

    rota = "fim"
    for linha in texto.splitlines():
        if linha.startswith("ROUTE="):
            rota = linha.split("=", 1)[1].strip().lower()
            break

    return {
        "agentes_chamados": ["roteador"],
        "rota":             rota,
    }

def no_orquestrador(estado: Estado) -> dict:
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = extrair_texto(mensagem)
            break

    texto_para_orquestrar = f"Formate a resposta a seguir para o usuário final de forma amigável: {ultima_especialista}"

    saida = orquestrador_app.invoke({
        "mensagens": texto_para_orquestrar
    })

    return {
        "agentes_chamados": [estado["rota"], "orquestrador"],
        "messages":         [{"role": "assistant", "content": extrair_texto(saida)}],
    }

def decidir_especialista(estado: Estado) -> str:
    return estado["rota"] if estado["rota"] in ("estoquista", "comprador", "supervisor", "faq") else "fim"

def decidir_pos_guardrail_insulto(estado: Estado) -> str:
    return "guardrail_escopo" if estado["rota"] != "fim" else "fim"

def decidir_pos_guardrail_escopo(estado: Estado) -> str:
    return "roteador" if estado["rota"] != "fim" else "fim"

grafo = StateGraph(Estado)

grafo.add_node("guardrail_insulto", no_guardrail_insulto)  # type: ignore[call-overload]
grafo.add_node("guardrail_escopo", no_guardrail_escopo)  # type: ignore[call-overload]
grafo.add_node("roteador",     no_roteador)  # type: ignore[call-overload]
grafo.add_node("estoquista",   estoquista_app)
grafo.add_node("comprador",    comprador_app)
grafo.add_node("supervisor",   supervisor_app)
grafo.add_node("faq",          faq_app)
grafo.add_node("orquestrador", no_orquestrador)  # type: ignore[call-overload]
grafo.add_node("guardrail_saida", no_guardrail_saida)  # type: ignore[call-overload]

grafo.set_entry_point("guardrail_insulto")

grafo.add_conditional_edges(
    "guardrail_insulto",
    decidir_pos_guardrail_insulto,
    {
        "guardrail_escopo": "guardrail_escopo",
        "fim":                END,
    },
)

grafo.add_conditional_edges(
    "guardrail_escopo",
    decidir_pos_guardrail_escopo,
    {
        "roteador": "roteador",
        "fim":        END,
    },
)

grafo.add_conditional_edges(
    "roteador",
    decidir_especialista,
    {
        "estoquista": "estoquista",
        "comprador":  "comprador",
        "supervisor": "supervisor",
        "faq":        "faq",
        "fim":        "guardrail_saida",
    },
)

grafo.add_edge("estoquista",   "orquestrador")
grafo.add_edge("comprador",    "orquestrador")
grafo.add_edge("supervisor",   "orquestrador")
grafo.add_edge("orquestrador", "guardrail_saida")
grafo.add_edge("guardrail_saida", END)
grafo.add_edge("faq",          "guardrail_saida")

memory = MemorySaver()
fluxo_agentes = grafo.compile(checkpointer=memory)

def executar_fluxo_assessor(pergunta_usuario: str, session_id: str, user_id: str = "user_test") -> str:
    iniciar_sessao(session_id, user_id=user_id)

    estado_inicial: Estado = {
        "messages":           [HumanMessage(content=pergunta_usuario)],
        "agentes_chamados":   [],
        "rota":               "",
        "mapa_pii":           {},
    }

    estado_final = fluxo_agentes.invoke(
        estado_inicial,
        config={"configurable": {"thread_id": session_id, "user_id": user_id}},
    )

    pergunta_anonimizada, _ = anonimizar_entrada(pergunta_usuario)
    resposta_final = extrair_texto(estado_final["messages"][-1])

    salvar_mensagem(session_id, "human", pergunta_anonimizada, user_id=user_id)
    salvar_mensagem(session_id, "iai", resposta_final, user_id=user_id)

    print(f"\n[Debug] Agentes chamados: {estado_final['agentes_chamados']}")

    return resposta_final