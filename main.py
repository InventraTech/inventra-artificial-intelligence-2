from dotenv import load_dotenv
import os
import operator
from typing import Annotated
from langgraph.graph import StateGraph, MessagesState, END
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain.agents import create_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import RemoveMessage
from langchain_core.tools import tool
from langchain_core.prompts import ChatPromptTemplate

from prompts import (
    ROTEADOR_SYSTEM_PROMPT,
    ORQUESTRADOR_SYSTEM_PROMPT,
    ESTOQUISTA_SYSTEM_PROMPT,
    COMPRADOR_SYSTEM_PROMPT,
    SUPERVISOR_SYSTEM_PROMPT,
    FAQ_SYSTEM_PROMPT
)
from guardrail import guardrail_entrada, guardrail_saida, anonimizar_entrada

load_dotenv()

llm_gemini = ChatGoogleGenerativeAI(
    model="gemini-2.5-flash",
    temperature=0.2, 
    top_p=0.95,
    google_api_key=os.getenv("GEMINI_API_KEY")
)

llm_groq = ChatGroq(
    model="openai/gpt-oss-20b",
    temperature=0.0,
    top_p=0.95,
    api_key=os.getenv("GROQ_API_KEY")
)

llm_especialista = llm_gemini.with_fallbacks([llm_groq])
llm_rapido = llm_groq

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

@tool
def faq_retriever_mock(pergunta: str) -> str:
    """Consulta a base de conhecimento do app Inventra."""
    return "Para cadastrar um produto, vá no menu lateral esquerdo e clique em 'Produtos'."

router_prompt = ChatPromptTemplate.from_messages([
    ("system", ROTEADOR_SYSTEM_PROMPT), 
    ("human", "{mensagens}")
])
router_app = router_prompt | llm_rapido

orquestrador_prompt = ChatPromptTemplate.from_messages([
    ("system", ORQUESTRADOR_SYSTEM_PROMPT), 
    ("human", "{mensagens}")
])
orquestrador_app = orquestrador_prompt | llm_rapido

estoquista_app = create_agent(
    model=llm_especialista,
    tools=[consultar_estoque_mock],
    system_prompt=ESTOQUISTA_SYSTEM_PROMPT
)

comprador_app = create_agent(
    model=llm_especialista,
    tools=[criar_requisicao_mock],
    system_prompt=COMPRADOR_SYSTEM_PROMPT
)

supervisor_app = create_agent(
    model=llm_especialista,
    tools=[relatorio_desperdicio_mock],
    system_prompt=SUPERVISOR_SYSTEM_PROMPT
)

faq_app = create_agent(
    model=llm_rapido,
    tools=[faq_retriever_mock],
    system_prompt=FAQ_SYSTEM_PROMPT
)

class Estado(MessagesState):
    agentes_chamados:   Annotated[list[str], operator.add]
    rota:               str
    mapa_pii:           dict

def no_guardrail_entrada(estado: Estado) -> dict:
    mensagem_original = list(estado["messages"])[-1]
    texto_original = mensagem_original.content if hasattr(mensagem_original, 'content') else mensagem_original.text
    
    texto_anonimizado, mapa = anonimizar_entrada(texto_original)
    resultado = guardrail_entrada(texto_anonimizado)

    if resultado["bloqueado"]:
        return {
            "messages":         [{"role": "assistant", "content": resultado["mensagem"]}],
            "rota":             "fim",
            "mapa_pii":         mapa,
            "agentes_chamados": [f"guardrail_entrada:{resultado['motivo']}"]
        }
    
    return {
        "messages": [
            RemoveMessage(id=mensagem_original.id),
            {"role": "human", "content": texto_anonimizado}
        ],
        "mapa_pii":         mapa,
        "agentes_chamados": ["guardrail_entrada:aprovado"],
    }

def no_guardrail_saida(estado: Estado) -> dict:
    ultima = ""
    for msg in reversed(estado["messages"]):
        if msg.type == "ai" and msg.content:
            ultima = msg.content
            break
    
    resultado = guardrail_saida(ultima, estado.get("mapa_pii"), {})

    return {
       "messages":         [{"role": "assistant", "content": resultado["conteudo"]}],
       "agentes_chamados": ["guardrail_saida"]
    }

def no_roteador(estado: Estado) -> dict:
    ultima_mensagem = estado["messages"][-1].content if hasattr(estado["messages"][-1], 'content') else estado["messages"][-1].get('content', '')
    saida = router_app.invoke({"mensagens": ultima_mensagem})
    
    texto = saida.content

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
            ultima_especialista = mensagem.content
            break

    texto_para_orquestrar = f"Formate a resposta a seguir para o usuário final de forma amigável: {ultima_especialista}"
    
    saida = orquestrador_app.invoke({
        "mensagens": texto_para_orquestrar
    })
    
    return {
        "agentes_chamados": [estado["rota"], "orquestrador"],
        "messages":         [{"role": "assistant", "content": saida.content}],
    }

def decidir_especialista(estado: Estado) -> str:
    return estado["rota"] if estado["rota"] in ("estoquista", "comprador", "supervisor", "faq") else "fim"

def decidir_pos_guardrail_entrada(estado: Estado) -> str:
    return "roteador" if estado["rota"] != "fim" else "fim"

grafo = StateGraph(Estado)

grafo.add_node("guardrail_entrada", no_guardrail_entrada)
grafo.add_node("roteador",     no_roteador)
grafo.add_node("estoquista",   estoquista_app)
grafo.add_node("comprador",    comprador_app)
grafo.add_node("supervisor",   supervisor_app)
grafo.add_node("faq",          faq_app)
grafo.add_node("orquestrador", no_orquestrador)
grafo.add_node("guardrail_saida", no_guardrail_saida)

grafo.set_entry_point("guardrail_entrada")

grafo.add_conditional_edges(
    "guardrail_entrada",
    decidir_pos_guardrail_entrada,
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

def executar_fluxo_assessor(pergunta_usuario: str, session_id: str) -> str:
    estado_inicial = {
        "messages":           [{"role": "human", "content": pergunta_usuario}],
        "agentes_chamados":   [],
        "rota":               "",
        "mapa_pii":           {},
    }

    estado_final = fluxo_agentes.invoke(
        estado_inicial,
        config={"configurable": {"thread_id": session_id}},
    )

    print(f"\n[Debug] Agentes chamados: {estado_final['agentes_chamados']}")
    
    ultima_msg = estado_final["messages"][-1]
    return ultima_msg.content if hasattr(ultima_msg, 'content') else ultima_msg.get('content', '')

if __name__ == "__main__":
    session_id = "teste_usuario" 
    print("=====================================================")
    print("  IAI (Inventra AI) - Teste de Terminal v1")
    print("  Digite 'sair' para encerrar a conversa.")
    print("=====================================================\n")

    while True:
        try:
            user_input = input("Você: ")
            if user_input.lower() in ("sair", "end", "fim", "tchau", "bye"):
                print("IAI: Encerrando a conversa. Até logo!")
                break

            resposta = executar_fluxo_assessor(
                pergunta_usuario=user_input,
                session_id=session_id,
            )
            print(f"IAI: {resposta}\n")

        except KeyboardInterrupt:
            print("\nSaindo...")
            break
        except Exception as e:
            print("Erro ao consumir a API:", e)
            continue