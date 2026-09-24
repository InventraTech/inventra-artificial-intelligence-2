import json
from hashlib import sha256
from threading import RLock
from typing import Any

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langchain_core.prompts import ChatPromptTemplate
from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from iai.app.guardrail import (
    anonimizar_entrada,
    guardrail_escopo,
    guardrail_insulto,
    guardrail_saida,
)
from iai.app.llms import llm_especialista, llm_rapido, specialist_fallback
from iai.app.memory import iniciar_sessao, salvar_mensagem
from iai.app.presentation import faq_entries, render_results
from iai.app.prompts import (
    COMPRADOR_SYSTEM_PROMPT,
    ESTOQUISTA_SYSTEM_PROMPT,
    FAQ_SYSTEM_PROMPT,
    ROTEADOR_SYSTEM_PROMPT,
    SUPERVISOR_SYSTEM_PROMPT,
)
from iai.app.schemas import ActionSelection, Estado, FAQSelection
from iai.app.security import ExecutionContext, execution_context
from iai.app.tools.faq import faq_retriever
from iai.app.tools.inventory import COMPRADOR_TOOLS, ESTOQUISTA_TOOLS, SUPERVISOR_TOOLS


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


def model_messages(messages) -> list[Any]:
    # Previously restored PII in assistant history must not be sent back to a provider.
    return [msg.model_copy(update={"content": anonimizar_entrada(extrair_texto(msg))[0]})
            for msg in messages]


router_prompt = ChatPromptTemplate.from_messages([
    ("system", ROTEADOR_SYSTEM_PROMPT), 
    ("human", "{mensagens}")
])
router_app = router_prompt | llm_rapido

estoquista_app = create_agent(
    model=llm_especialista,
    tools=ESTOQUISTA_TOOLS,
    middleware=[specialist_fallback],
    response_format=ToolStrategy(ActionSelection),
    system_prompt=ESTOQUISTA_SYSTEM_PROMPT
)

comprador_app = create_agent(
    model=llm_especialista,
    tools=COMPRADOR_TOOLS,
    middleware=[specialist_fallback],
    response_format=ToolStrategy(ActionSelection),
    system_prompt=COMPRADOR_SYSTEM_PROMPT
)

supervisor_app = create_agent(
    model=llm_especialista,
    tools=SUPERVISOR_TOOLS,
    middleware=[specialist_fallback],
    response_format=ToolStrategy(ActionSelection),
    system_prompt=SUPERVISOR_SYSTEM_PROMPT
)

faq_app = create_agent(
    model=llm_rapido,
    tools=[faq_retriever],
    system_prompt=FAQ_SYSTEM_PROMPT,
    response_format=ToolStrategy(FAQSelection),
)


def specialist_node(agent):
    def run(estado: Estado) -> dict:
        # Explicit wrappers collect only this turn's genuine ToolMessages.
        existing = {msg.id for msg in estado["messages"] if msg.id}
        try:
            response = agent.invoke({"messages": model_messages(estado["messages"])}, config={"recursion_limit": 20})
        except Exception:  # noqa: BLE001 -- provider failures must never imply business success
            return {"tool_results": [{"success": False, "code": "AGENT_UNAVAILABLE",
                                      "message": "Não foi possível concluir a consulta. Tente novamente."}]}
        results = []
        for msg in response["messages"]:
            if msg.type != "tool" or (msg.id and msg.id in existing):
                continue
            if msg.name not in {"consultar_dados", "preparar_baixa", "preparar_requisicao"}:
                continue
            try:
                value = json.loads(extrair_texto(msg))
                if isinstance(value, dict) and "success" in value:
                    results.append(value)
                else:
                    raise ValueError("not a tool result")
            except (ValueError, TypeError):
                results.append({"success": False, "code": "TOOL_ERROR",
                                "message": "A ferramenta recusou os parâmetros. Informe os dados necessários."})
        selection = response.get("structured_response")
        if isinstance(selection, ActionSelection) and selection.missing_fields:
            results.append({"success": False, "code": "MISSING_INFORMATION",
                            "message": "Informe: " + ", ".join(selection.missing_fields) + "."})
        return {"tool_results": results}
    return run


def no_faq(estado: Estado) -> dict:
    text = "Não encontrei uma resposta suficiente no FAQ oficial do Inventra."
    try:
        response = faq_app.invoke({"messages": model_messages(estado["messages"])}, config={"recursion_limit": 15})
        selection = response.get("structured_response")
        entries = faq_entries()
        if (isinstance(selection, FAQSelection) and selection.faq_index is not None
                and 1 <= selection.faq_index <= len(entries)):
            text = entries[selection.faq_index - 1]["r"]
    except Exception:  # noqa: BLE001 -- no fabricated FAQ answer on provider/retrieval errors
        text = "Não foi possível consultar o FAQ agora. Tente novamente."
    return {"messages": [{"role": "assistant", "content": text}], "agentes_chamados": ["faq"]}

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
    # Clarification replies such as '20 KG' need the recent, anonymized context.
    contexto = "\n".join(extrair_texto(msg) for msg in model_messages(estado["messages"][-4:-1]))
    resultado = guardrail_escopo(f"Contexto anterior: {contexto}\nMensagem atual: {texto}" if contexto else texto)

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

def no_roteador(estado: Estado) -> dict:
    ultima_mensagem = extrair_texto(estado["messages"][-1])
    contexto = "\n".join(extrair_texto(msg) for msg in model_messages(estado["messages"][-4:-1]))
    saida = router_app.invoke({"mensagens": f"Contexto anterior: {contexto}\nMensagem atual: {ultima_mensagem}" if contexto else ultima_mensagem})

    texto = extrair_texto(saida)

    if "ROUTE=" not in texto:
        return {
            "agentes_chamados": ["roteador"],
            "rota":             "fim",
            "messages":         [{"role": "assistant", "content": "Olá! Posso ajudar com estoque, compras e dúvidas sobre o Inventra."}],
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
    return {
        "agentes_chamados": [estado["rota"], "orquestrador"],
        "messages": [{"role": "assistant", "content": render_results(estado.get("tool_results", []))}],
    }

def decidir_especialista(estado: Estado) -> str:
    return estado["rota"] if estado["rota"] in ("estoquista", "comprador", "supervisor", "faq") else "fim"

def decidir_pos_guardrail_insulto(estado: Estado) -> str:
    return "guardrail_escopo" if estado["rota"] != "fim" else "fim"

def decidir_pos_guardrail_escopo(estado: Estado) -> str:
    return "roteador" if estado["rota"] != "fim" else "fim"

grafo = StateGraph(Estado)

# ignores abaixo: limitação do stub do langgraph 1.x, que não resolve o overload
# de add_node para funções simples recebendo um TypedDict de estado.
grafo.add_node("guardrail_insulto", no_guardrail_insulto)  # type: ignore[call-overload]
grafo.add_node("guardrail_escopo", no_guardrail_escopo)  # type: ignore[call-overload]
grafo.add_node("roteador",     no_roteador)  # type: ignore[call-overload]
grafo.add_node("estoquista",   specialist_node(estoquista_app))
grafo.add_node("comprador",    specialist_node(comprador_app))
grafo.add_node("supervisor",   specialist_node(supervisor_app))
grafo.add_node("faq",          no_faq)  # type: ignore[call-overload]
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

_session_locks = [RLock() for _ in range(128)]


def executar_fluxo_assessor(pergunta_usuario: str, session_id: str, user_id: str = "user_test") -> str:
    thread_id = sha256(json.dumps([user_id, session_id]).encode()).hexdigest()
    token = execution_context.set(ExecutionContext(user_id, session_id))
    try:
        with _session_locks[int(thread_id[:8], 16) % len(_session_locks)]:
            return _executar(pergunta_usuario, session_id, user_id, thread_id)
    finally:
        execution_context.reset(token)


def _executar(pergunta_usuario: str, session_id: str, user_id: str, thread_id: str) -> str:
    iniciar_sessao(session_id, user_id=user_id)

    estado_inicial: Estado = {
        "messages":           [HumanMessage(content=pergunta_usuario)],
        "agentes_chamados":   [],
        "rota":               "",
        "mapa_pii":           {},
        "tool_results":       [],
    }

    estado_final = fluxo_agentes.invoke(
        estado_inicial,
        config={"configurable": {"thread_id": thread_id}, "recursion_limit": 30},
    )

    pergunta_anonimizada, _ = anonimizar_entrada(pergunta_usuario)
    resposta_final = extrair_texto(estado_final["messages"][-1])

    salvar_mensagem(session_id, "human", pergunta_anonimizada, user_id=user_id)
    salvar_mensagem(session_id, "iai", resposta_final, user_id=user_id)

    print(f"\n[Debug] Agentes chamados: {estado_final['agentes_chamados']}")

    return resposta_final
