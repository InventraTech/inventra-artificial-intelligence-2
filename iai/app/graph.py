from typing import cast

from langchain.agents import create_agent
from langchain_core.messages import BaseMessage, HumanMessage, RemoveMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, StateGraph

from iai.app.config import BUSINESS_MODE, LLM_MODE
from iai.app.guardrail import (
    anonimizar_entrada,
    guardrail_escopo,
    guardrail_insulto,
    guardrail_saida,
)
from iai.app.judge import (
    FATOS_VAZIOS,
    LIMIAR_CONFIANCA_JUIZ,
    MAX_TENTATIVAS_JUIZ,
    avaliar_alucinacao,
    extrair_pergunta_fatos_e_resposta,
    jev_antes_da_tool,
    ultima_mensagem_ai,
)
from iai.app.llms import llm_especialista, llm_rapido
from iai.app.memory import (
    iniciar_sessao,
    mensagens_ativas,
    salvar_mensagem,
    salvar_mensagem_juiz,
)
from iai.app.prompts import (
    COMPRADOR_SYSTEM_PROMPT,
    ESTOQUISTA_SYSTEM_PROMPT,
    FAQ_SYSTEM_PROMPT,
    ORQUESTRADOR_SYSTEM_PROMPT,
    ROTEADOR_SYSTEM_PROMPT,
    SUPERVISOR_SYSTEM_PROMPT,
)
from iai.app.schemas import Estado
from iai.app.security import context
from iai.app.tools.faq import faq_retriever
from iai.app.tools.memoria import TOOLS_MEMORIA
from iai.app.tools.operational import BUY_TOOLS, STOCK_TOOLS, SUPERVISOR_TOOLS


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


router_app = create_agent(
    model=llm_rapido,
    tools=TOOLS_MEMORIA,
    system_prompt=ROTEADOR_SYSTEM_PROMPT,
    middleware=[jev_antes_da_tool],
)

orquestrador_prompt = ChatPromptTemplate.from_messages(
    [("system", ORQUESTRADOR_SYSTEM_PROMPT), ("human", "{mensagens}")]
)
orquestrador_app = orquestrador_prompt | llm_rapido

estoquista_app = create_agent(  # type: ignore[call-overload]
    model=llm_especialista,
    tools=STOCK_TOOLS + TOOLS_MEMORIA + [faq_retriever],
    system_prompt=ESTOQUISTA_SYSTEM_PROMPT,
    middleware=[jev_antes_da_tool],
)

comprador_app = create_agent(  # type: ignore[call-overload]
    model=llm_especialista,
    tools=BUY_TOOLS + TOOLS_MEMORIA + [faq_retriever],
    system_prompt=COMPRADOR_SYSTEM_PROMPT,
    middleware=[jev_antes_da_tool],
)

supervisor_app = create_agent(  # type: ignore[call-overload]
    model=llm_especialista,
    tools=SUPERVISOR_TOOLS + TOOLS_MEMORIA + [faq_retriever],
    system_prompt=SUPERVISOR_SYSTEM_PROMPT,
    middleware=[jev_antes_da_tool],
)

faq_app = create_agent(
    model=llm_rapido,
    tools=[faq_retriever],
    system_prompt=FAQ_SYSTEM_PROMPT,
    middleware=[jev_antes_da_tool],
)


def no_guardrail_insulto(estado: Estado) -> dict:
    mensagem_original = list(estado["messages"])[-1]
    texto_original = extrair_texto(mensagem_original)

    texto_anonimizado, mapa = anonimizar_entrada(texto_original)
    resultado = guardrail_insulto(texto_anonimizado)

    if resultado["bloqueado"]:
        return {
            "messages": [{"role": "assistant", "content": resultado["mensagem"]}],
            "rota": "fim",
            "mapa_pii": mapa,
            "agentes_chamados": [f"guardrail_insulto:{resultado['motivo']}"],
        }

    if mensagem_original.id is None:
        raise ValueError("Mensagem sem ID, não é possível remover")

    return {
        "messages": [
            RemoveMessage(id=mensagem_original.id),
            {"role": "human", "content": texto_anonimizado},
        ],
        "mapa_pii": mapa,
        "agentes_chamados": ["guardrail_insulto:aprovado"],
    }


def no_guardrail_escopo(estado: Estado) -> dict:
    texto = extrair_texto(estado["messages"][-1])
    resultado = guardrail_escopo(texto)

    if resultado["bloqueado"]:
        return {
            "messages": [{"role": "assistant", "content": resultado["mensagem"]}],
            "rota": "fim",
            "agentes_chamados": [f"guardrail_escopo:{resultado['motivo']}"],
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
        "messages": [{"role": "assistant", "content": resultado["conteudo"]}],
        "agentes_chamados": ["guardrail_saida"],
    }


def no_roteador(estado: Estado, config: RunnableConfig) -> dict:
    saida = router_app.invoke({"messages": list(estado["messages"])}, config=config)

    texto = extrair_texto(saida["messages"][-1])

    if "ROUTE=" not in texto:
        from iai.app.guardrail import normalizar_texto

        greeting = normalizar_texto(extrair_texto(estado["messages"][-1])).strip(
            " .!?\n"
        )
        _, facts, _ = extrair_pergunta_fatos_e_resposta(saida["messages"])
        if greeting not in {
            "oi",
            "ola",
            "bom dia",
            "boa tarde",
            "boa noite",
            "obrigado",
            "obrigada",
            "tudo bem",
        } and (
            not facts
            or avaliar_alucinacao(
                extrair_texto(estado["messages"][-1]), facts, texto
            ).confianca
            < LIMIAR_CONFIANCA_JUIZ
        ):
            # Falha de protocolo não vira resposta operacional sem evidência.
            return {
                "agentes_chamados": ["roteador:protocolo_invalido"],
                "rota": "supervisor",
            }
        if not facts:
            texto = "Olá! Posso ajudar com consultas do INVENTRA e propostas para aprovação."
        return {
            "agentes_chamados": ["roteador"],
            "rota": "fim",
            "messages": [{"role": "assistant", "content": texto}],
        }

    rota = "fim"
    for linha in texto.splitlines():
        if linha.startswith("ROUTE="):
            rota = linha.split("=", 1)[1].strip().lower()
            break

    if rota not in {"estoquista", "comprador", "supervisor", "faq"}:
        rota = "supervisor"

    return {
        "agentes_chamados": ["roteador"],
        "rota": rota,
    }


def no_orquestrador(estado: Estado) -> dict:
    ultima_especialista = ""
    for mensagem in reversed(estado["messages"]):
        if mensagem.type == "ai" and mensagem.content:
            ultima_especialista = extrair_texto(mensagem)
            break

    texto_para_orquestrar = f"Formate a resposta a seguir para o usuário final de forma amigável: {ultima_especialista}"

    saida = orquestrador_app.invoke({"mensagens": texto_para_orquestrar})

    pergunta, fatos, _ = extrair_pergunta_fatos_e_resposta(estado["messages"])
    final_text = extrair_texto(saida)
    if (
        avaliar_alucinacao(pergunta, fatos or FATOS_VAZIOS, final_text).confianca
        < LIMIAR_CONFIANCA_JUIZ
    ):
        final_text = ultima_especialista

    return {
        "agentes_chamados": [estado["rota"], "orquestrador"],
        "messages": [{"role": "assistant", "content": final_text}],
    }


def no_juiz(estado: Estado, config: RunnableConfig) -> dict:
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(estado["messages"])

    if not resposta:
        return {
            "agentes_chamados": ["juiz:sem_resposta_para_validar"],
            "repetir_especialista": False,
        }

    resultado = avaliar_alucinacao(pergunta, fatos or FATOS_VAZIOS, resposta)
    aprovado = resultado.confianca >= LIMIAR_CONFIANCA_JUIZ

    if aprovado:
        return {"agentes_chamados": ["juiz:aprovado"], "repetir_especialista": False}

    configuravel = (config or {}).get("configurable", {})
    salvar_mensagem_juiz(
        session_id=configuravel.get("thread_id", ""),
        conteudo=f'Resposta avaliada: "{resposta}" — Motivo: {resultado.motivo}',
        confianca=resultado.confianca,
        user_id=configuravel.get("user_id", "user_test"),
        kitchen_id=configuravel.get("kitchen_id", 1),
    )

    tentativas = estado.get("tentativas_juiz", 0) + 1

    if tentativas >= MAX_TENTATIVAS_JUIZ:
        mensagens: list = [
            {
                "role": "assistant",
                "content": (
                    "Não consegui confirmar essa informação com segurança nos dados "
                    "do sistema. Pode reformular a pergunta ou falar com o suporte?"
                ),
            }
        ]

        mensagem_anterior = ultima_mensagem_ai(estado["messages"])
        if mensagem_anterior and mensagem_anterior.id:
            mensagens.insert(0, RemoveMessage(id=mensagem_anterior.id))

        return {
            "agentes_chamados": [f"juiz:reprovado_final:{resultado.motivo}"],
            "repetir_especialista": False,
            "tentativas_juiz": tentativas,
            "messages": mensagens,
        }

    return {
        "agentes_chamados": [f"juiz:reprovado:{resultado.motivo}"],
        "repetir_especialista": True,
        "tentativas_juiz": tentativas,
        "messages": [
            HumanMessage(
                content=(
                    f"Sua última resposta não pôde ser confirmada pelos dados "
                    f"retornados pelas ferramentas (motivo: {resultado.motivo}). Responda "
                    "novamente usando ESTRITAMENTE os dados já obtidos; se necessário, "
                    "chame as ferramentas novamente."
                ),
                additional_kwargs={"juiz_feedback": True},
            )
        ],
    }


def decidir_pos_juiz(estado: Estado) -> str:
    if estado.get("repetir_especialista"):
        return estado["rota"]
    return "guardrail_saida" if estado["rota"] == "faq" else "orquestrador"


def decidir_especialista(estado: Estado) -> str:
    return (
        estado["rota"]
        if estado["rota"] in ("estoquista", "comprador", "supervisor", "faq")
        else "fim"
    )


def decidir_pos_guardrail_insulto(estado: Estado) -> str:
    return "guardrail_escopo" if estado["rota"] != "fim" else "fim"


def decidir_pos_guardrail_escopo(estado: Estado) -> str:
    return "roteador" if estado["rota"] != "fim" else "fim"


grafo = StateGraph(Estado)

grafo.add_node("guardrail_insulto", no_guardrail_insulto)  # type: ignore[call-overload]
grafo.add_node("guardrail_escopo", no_guardrail_escopo)  # type: ignore[call-overload]
grafo.add_node("roteador", no_roteador)  # type: ignore[call-overload]


def especialista_node(nome):
    def invoke(estado, config):
        agente = {
            "estoquista": estoquista_app,
            "comprador": comprador_app,
            "supervisor": supervisor_app,
            "faq": faq_app,
        }[nome]
        resposta = agente.invoke({"messages": list(estado["messages"])}, config=config)
        return {"messages": resposta["messages"]}

    return invoke


grafo.add_node("estoquista", especialista_node("estoquista"))
grafo.add_node("comprador", especialista_node("comprador"))
grafo.add_node("supervisor", especialista_node("supervisor"))
grafo.add_node("faq", especialista_node("faq"))
grafo.add_node("juiz", no_juiz)  # type: ignore[call-overload]
grafo.add_node("orquestrador", no_orquestrador)  # type: ignore[call-overload]
grafo.add_node("guardrail_saida", no_guardrail_saida)  # type: ignore[call-overload]

grafo.set_entry_point("guardrail_insulto")

grafo.add_conditional_edges(
    "guardrail_insulto",
    decidir_pos_guardrail_insulto,
    {
        "guardrail_escopo": "guardrail_escopo",
        "fim": END,
    },
)

grafo.add_conditional_edges(
    "guardrail_escopo",
    decidir_pos_guardrail_escopo,
    {
        "roteador": "roteador",
        "fim": END,
    },
)

grafo.add_conditional_edges(
    "roteador",
    decidir_especialista,
    {
        "estoquista": "estoquista",
        "comprador": "comprador",
        "supervisor": "supervisor",
        "faq": "faq",
        "fim": "guardrail_saida",
    },
)

grafo.add_edge("estoquista", "juiz")
grafo.add_edge("comprador", "juiz")
grafo.add_edge("supervisor", "juiz")
grafo.add_edge("faq", "juiz")

grafo.add_conditional_edges(
    "juiz",
    decidir_pos_juiz,
    {
        "estoquista": "estoquista",
        "comprador": "comprador",
        "supervisor": "supervisor",
        "faq": "faq",
        "orquestrador": "orquestrador",
        "guardrail_saida": "guardrail_saida",
    },
)

grafo.add_edge("orquestrador", "guardrail_saida")
grafo.add_edge("guardrail_saida", END)

fluxo_agentes = grafo.compile()  # histórico reidratado do Mongo a cada rodada


def executar_fluxo_assessor(
    pergunta_usuario: str, session_id: str, user_id: str = "user_test"
) -> str:
    import json
    from time import perf_counter

    from fastapi import HTTPException
    from pymongo.errors import PyMongoError

    from iai.app.metrics import record_metric

    ctx = context()
    if user_id != ctx.identity.user_id:
        raise HTTPException(403, "user_id diverge da identidade autenticada.")
    k = ctx.identity.kitchen_id
    iniciar_sessao(session_id, user_id=user_id, kitchen_id=k)
    previous = mensagens_ativas(session_id, user_id, k)[-30:]
    history = [
        HumanMessage(content=m.content)
        if m.role == "human"
        else {"role": "assistant", "content": anonimizar_entrada(m.content)[0]}
        for m in previous
    ]
    estado_inicial = cast(
        Estado,
        {
            "messages": history + [HumanMessage(content=pergunta_usuario)],
            "agentes_chamados": [],
            "rota": "",
            "mapa_pii": {},
            "tentativas_juiz": 0,
            "repetir_especialista": False,
        },
    )
    started = perf_counter()
    status = "ok"
    error_type = None
    agents = []
    usage = {}
    output: dict
    try:
        if LLM_MODE == "demo":
            from iai.app.demo import demo_graph

            output = demo_graph.invoke(estado_inicial)
        else:
            from iai.app.config import GEMINI_API_KEY, GROQ_API_KEY, TYPESAFE_API_KEY

            if (
                not GROQ_API_KEY.get_secret_value()
                or not GEMINI_API_KEY.get_secret_value()
                or not TYPESAFE_API_KEY.get_secret_value()
            ):
                raise HTTPException(
                    503,
                    "Provedores não configurados. Configure GEMINI_API_KEY, GROQ_API_KEY e TYPESAFE_API_KEY ou habilite explicitamente o modo demo.",
                )
            from langchain_core.callbacks import UsageMetadataCallbackHandler

            usage_callback = UsageMetadataCallbackHandler()
            output = fluxo_agentes.invoke(
                estado_inicial,
                config={
                    "configurable": {
                        "thread_id": session_id,
                        "user_id": user_id,
                        "kitchen_id": k,
                    },
                    "recursion_limit": 80,
                    "callbacks": [usage_callback],
                },
            )
            usage = usage_callback.usage_metadata
        agents = output.get("agentes_chamados", [])
        response = extrair_texto(output["messages"][-1])
        proposals = []
        tool_failures = []
        for message in reversed(output["messages"]):
            if message.type == "human" and not message.additional_kwargs.get(
                "juiz_feedback"
            ):
                break
            if message.type == "tool":
                try:
                    data = json.loads(extrair_texto(message))
                    if isinstance(data, dict) and data.get("proposal"):
                        proposals.append(data["proposal"])
                    elif isinstance(data, dict) and data.get("ok") is False:
                        tool_failures.append(
                            f"{data.get('code', 'ERRO')}: {data.get('message', 'Etapa não confirmada.')}"
                        )
                except (ValueError, TypeError):
                    pass
        if proposals:
            response = "\n".join(
                f"Proposta {p['id']}: {p['action']['operation']} — {json.dumps(p['action']['parameters'], ensure_ascii=False)}. Estado: {p['status']}. Nenhuma alteração de negócio foi executada nesta conversa."
                for p in reversed(proposals)
            )
            if tool_failures:
                response += "\nEtapas não concluídas:\n" + "\n".join(
                    reversed(tool_failures)
                )
            response += "\nSomente as propostas listadas foram registradas; outros itens ou dados ainda não confirmados não foram propostos."
        if BUSINESS_MODE == "mock":
            response = "[DADOS OPERACIONAIS MOCK — demonstração]\n" + response
        if LLM_MODE == "demo":
            response = "[INTERPRETAÇÃO DEMO limitada — sem LLM]\n" + response
        from iai.app.privacy import remove_secrets

        response = remove_secrets(response)
        anonymous, _ = anonimizar_entrada(pergunta_usuario)
        salvar_mensagem(session_id, "human", anonymous, user_id=user_id, kitchen_id=k)
        salvar_mensagem(session_id, "iai", response, user_id=user_id, kitchen_id=k)
        return response
    except (HTTPException, PyMongoError) as exc:
        status = "erro"
        error_type = type(exc).__name__
        raise
    except Exception as exc:  # noqa: BLE001 - não expor mensagens de SDK com credenciais
        status = "erro"
        error_type = type(exc).__name__
        raise HTTPException(
            502,
            "Não foi possível concluir o fluxo. Verifique suas solicitações antes de repetir; nenhuma execução de negócio é realizada pelo modelo.",
        ) from None
    finally:
        record_metric(
            ctx.identity, perf_counter() - started, status, agents, usage, error_type
        )
