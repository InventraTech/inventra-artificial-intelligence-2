import json
import runpy
from pathlib import Path

import httpx
import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pydantic import SecretStr

import iai.app.judge as judge_module
from iai.app.judge import (
    FATOS_VAZIOS,
    LIMIAR_CONFIANCA_JUIZ,
    avaliar_alucinacao,
    extrair_pergunta_fatos_e_resposta,
    ultima_mensagem_ai,
)
from iai.app.schemas import ResultadoJuiz


def test_extrair_pergunta_fatos_e_resposta_rodada_simples():
    mensagens = [
        HumanMessage(content="quantos tomates temos?"),
        ToolMessage(content="Temos 5kg de tomate.", tool_call_id="1"),
        AIMessage(content="Você tem 5kg de tomate."),
    ]
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert pergunta == "quantos tomates temos?"
    assert fatos == "Temos 5kg de tomate."
    assert resposta == "Você tem 5kg de tomate."


def test_extrair_pergunta_fatos_e_resposta_sem_tool_fica_com_fatos_vazio():
    mensagens = [
        HumanMessage(content="cria uma requisição de cebola"),
        AIMessage(content="Requisição criada com sucesso."),
    ]
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert pergunta == "cria uma requisição de cebola"
    assert fatos == ""
    assert resposta == "Requisição criada com sucesso."


def test_extrair_pergunta_fatos_e_resposta_ignora_mensagem_de_correcao_do_juiz():
    mensagens = [
        HumanMessage(content="quantos tomates temos?"),
        ToolMessage(content="Temos 5kg de tomate.", tool_call_id="1"),
        AIMessage(content="Você tem 50kg de tomate."),
        HumanMessage(content="corrija isso", additional_kwargs={"juiz_feedback": True}),
        AIMessage(content="Você tem 5kg de tomate."),
    ]
    pergunta, fatos, resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert pergunta == "quantos tomates temos?"
    assert fatos == "Temos 5kg de tomate."
    assert resposta == "Você tem 5kg de tomate."


def test_extrair_pergunta_fatos_e_resposta_junta_varios_fatos_em_ordem():
    mensagens = [
        HumanMessage(content="pergunta"),
        ToolMessage(content="fato 1", tool_call_id="1"),
        ToolMessage(content="fato 2", tool_call_id="2"),
        AIMessage(content="resposta"),
    ]
    _pergunta, fatos, _resposta = extrair_pergunta_fatos_e_resposta(mensagens)
    assert fatos == "fato 1\nfato 2"


def test_ultima_mensagem_ai_encontra_a_mais_recente():
    mensagens = [
        AIMessage(content="primeira"),
        HumanMessage(content="oi"),
        AIMessage(content="segunda"),
    ]
    assert ultima_mensagem_ai(mensagens).content == "segunda"


def test_ultima_mensagem_ai_sem_nenhuma_retorna_none():
    assert ultima_mensagem_ai([HumanMessage(content="oi")]) is None


@pytest.fixture
def jev_transport(monkeypatch):
    client_type = httpx.Client
    monkeypatch.setattr(
        judge_module, "TYPESAFE_API_KEY", SecretStr("typesafe-test-key")
    )

    def install(handler):
        def client(**kwargs):
            assert kwargs["follow_redirects"] is False
            assert kwargs["timeout"] == judge_module.JEV_HTTP_TIMEOUT
            return client_type(transport=httpx.MockTransport(handler), **kwargs)

        monkeypatch.setattr(judge_module.httpx, "Client", client)

    return install


def jev_response(probability):
    return {
        "model": "jev-test",
        "answers": {"fundamentada": {"type": "noul", "noul": probability}},
        "usage": {"input_tokens": 120, "output_tokens": 12},
    }


def test_juiz_envia_contrato_typesafe_com_chave_apenas_no_header(jev_transport):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=jev_response(0.95))

    jev_transport(handler)
    resultado = avaliar_alucinacao("Quanto arroz?", "Saldo: 5 kg.", "Há 5 kg.")
    assert resultado == ResultadoJuiz(
        confianca=0.95, motivo="jev:resposta_fundamentada"
    )
    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
    assert request.headers["authorization"] == "Bearer typesafe-test-key"
    payload = json.loads(request.content)
    assert payload["model"] == judge_module.JEV_MODEL
    assert payload["state"] == {
        "pergunta": "Quanto arroz?",
        "fatos": "Saldo: 5 kg.",
        "resposta": "Há 5 kg.",
    }
    assert payload["questions"]["fundamentada"]["type"] == "noul"
    assert (
        "AGUARDANDO_APROVACAO" in payload["questions"]["fundamentada"]["instructions"]
    )
    assert "typesafe-test-key" not in request.content.decode()


@pytest.mark.parametrize("probability", [0, 0.1, 0.69, 0.7, 0.95, 1])
def test_noul_alimenta_limiar_existente_sem_confundir_com_certeza(
    jev_transport, probability
):
    payload = jev_response(probability)
    payload["answers"]["fundamentada"]["confidence"] = 1.0
    jev_transport(lambda request: httpx.Response(200, json=payload))
    result = avaliar_alucinacao("pergunta", "fatos", "resposta")
    assert result.confianca == probability
    assert result.motivo == (
        "jev:resposta_fundamentada"
        if probability >= 0.7
        else "jev:fundamentacao_insuficiente"
    )


@pytest.mark.parametrize("status", [302, 401, 403, 422, 429, 500, 503])
def test_erro_http_nao_aprova_nem_expoe_segredos_ou_repete_chamada(
    jev_transport, status
):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            status,
            json={"error": "typesafe-test-key"},
            headers={"Location": "https://outro.example"},
        )

    jev_transport(handler)
    result = avaliar_alucinacao("pergunta", "fatos", "resposta")
    assert result == ResultadoJuiz(confianca=0, motivo="erro_api_juiz")
    assert len(calls) == 1


@pytest.mark.parametrize("error_type", [httpx.ReadTimeout, httpx.ConnectError])
def test_falha_de_rede_bloqueia_sem_fallback(jev_transport, error_type):
    def handler(request):
        raise error_type("typesafe-test-key", request=request)

    jev_transport(handler)
    assert avaliar_alucinacao("pergunta", "fatos", "resposta") == ResultadoJuiz(
        confianca=0, motivo="erro_api_juiz"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"answers": {}},
        {"answers": {"fundamentada": {"type": "noul"}}},
        {"answers": {"fundamentada": {"type": "choice", "noul": 0.99}}},
        *[jev_response(value) for value in [-0.1, 1.1, "0.99", True, None]],
    ],
)
def test_contrato_invalido_nunca_aprova(jev_transport, payload):
    jev_transport(lambda request: httpx.Response(200, json=payload))
    assert avaliar_alucinacao("pergunta", "fatos", "resposta") == ResultadoJuiz(
        confianca=0, motivo="erro_api_juiz"
    )


def test_json_invalido_bloqueia(jev_transport):
    jev_transport(lambda request: httpx.Response(200, content=b"not json"))
    assert avaliar_alucinacao("pergunta", "fatos", "resposta").confianca == 0


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity"])
def test_probabilidade_nao_finita_bloqueia(jev_transport, value):
    body = '{"answers":{"fundamentada":{"type":"noul","noul":' + value + "}}}"
    jev_transport(lambda request: httpx.Response(200, content=body.encode()))
    assert avaliar_alucinacao("pergunta", "fatos", "resposta").confianca == 0


def test_chave_ausente_nao_faz_requisicao(jev_transport, monkeypatch):
    def handler(request):
        pytest.fail("Não deve chamar JEV sem chave")

    jev_transport(handler)
    monkeypatch.setattr(judge_module, "TYPESAFE_API_KEY", SecretStr(""))
    assert avaliar_alucinacao("pergunta", "fatos", "resposta") == ResultadoJuiz(
        confianca=0, motivo="jev_nao_configurado"
    )


def test_sem_fatos_e_com_segredos_sanitiza_state(jev_transport):
    def handler(request):
        state = json.loads(request.content)["state"]
        assert state["fatos"] == FATOS_VAZIOS
        assert "typesafe-test-key" not in state["pergunta"]
        assert "gsk_exemplo123" not in state["resposta"]
        assert "[SEGREDO_REMOVIDO]" in state["pergunta"]
        return httpx.Response(200, json=jev_response(0.1))

    jev_transport(handler)
    avaliar_alucinacao("typesafe-test-key", "", "Bearer gsk_exemplo123")


@pytest.mark.parametrize("probability,repeat", [(0.95, False), (0.1, True)])
def test_grafo_usa_resultado_http_do_jev(jev_transport, probability, repeat):
    from iai.app.graph import no_juiz

    jev_transport(lambda request: httpx.Response(200, json=jev_response(probability)))
    result = no_juiz(
        {
            "messages": [
                HumanMessage(content="Quanto arroz?"),
                ToolMessage(content="5 kg", tool_call_id="consulta"),
                AIMessage(content="Há 5 kg.", id="resposta"),
            ],
            "tentativas_juiz": 0,
        },
        {"configurable": {}},
    )
    assert result["repetir_especialista"] is repeat


def test_modo_live_exige_chave_typesafe(monkeypatch):
    from fastapi import HTTPException

    from iai.app import config, graph
    from iai.app.security import current_context
    from tests_helpers import context_for

    monkeypatch.setattr(graph, "LLM_MODE", "live")
    monkeypatch.setattr(config, "GEMINI_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(config, "GROQ_API_KEY", SecretStr("test-key"))
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", SecretStr(""))
    marker = current_context.set(context_for())
    try:
        with pytest.raises(HTTPException) as error:
            graph.executar_fluxo_assessor("Quanto arroz?", "sem-jev", "demo-estoquista")
        assert error.value.status_code == 503
        assert "TYPESAFE_API_KEY" in error.value.detail
    finally:
        current_context.reset(marker)


@pytest.mark.parametrize(
    "process_key,base_key,expected",
    [
        (None, None, "app-key"),
        (None, "base-key", "base-key"),
        ("env-key", "base-key", "env-key"),
    ],
)
def test_config_carrega_app_env_preservando_prioridade(
    tmp_path, monkeypatch, process_key, base_key, expected
):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    config_path = app_dir / "config.py"
    config_path.write_text(
        Path(judge_module.__file__).with_name("config.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (app_dir / ".env").write_text('TYPESAFE_API_KEY="app-key"\n', encoding="utf-8")
    if base_key:
        (tmp_path / ".env").write_text(
            f"TYPESAFE_API_KEY={base_key}\n", encoding="utf-8"
        )

    # Registrar também a ausência para o dotenv não vazar configuração entre testes.
    monkeypatch.setenv("TYPESAFE_API_KEY", process_key or "reset")
    if process_key is None:
        monkeypatch.delenv("TYPESAFE_API_KEY")
    loaded = runpy.run_path(str(config_path))
    assert loaded["TYPESAFE_API_KEY"].get_secret_value() == expected


@pytest.mark.parametrize(
    "verdict", [0.95, 0.7, 0.69, "http_error", "timeout", "invalid", "missing"]
)
def test_tool_so_cria_proposta_depois_da_aprovacao_jev(
    jev_transport, monkeypatch, verdict
):
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import (
        FakeMessagesListChatModel,
    )

    from iai.app.domain import Operation
    from iai.app.security import current_context
    from iai.app.storage import get_db
    from iai.app.tools import operational
    from tests_helpers import context_for

    class ScriptedModel(FakeMessagesListChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    events = []
    prepare = operational.create_proposal

    def tracked_proposal(*args, **kwargs):
        events.append("tool")
        assert events == ["jev", "tool"]
        return prepare(*args, **kwargs)

    def handler(request):
        events.append("jev")
        assert get_db().approvals.count_documents({}) == 0
        payload = json.loads(request.content)
        assert payload["state"]["ferramenta"] == "propor_registrar_saida"
        assert json.loads(payload["state"]["parametros"])["quantity"] == 2
        assert (
            "Consultas podem buscar dados"
            in payload["questions"]["fundamentada"]["instructions"]
        )
        if verdict == "timeout":
            raise httpx.ReadTimeout("falha simulada", request=request)
        if verdict == "http_error":
            return httpx.Response(401)
        if verdict == "invalid":
            return httpx.Response(200, json={})
        return httpx.Response(200, json=jev_response(verdict))

    monkeypatch.setattr(operational, "create_proposal", tracked_proposal)
    jev_transport(handler)
    if verdict == "missing":
        monkeypatch.setattr(judge_module, "TYPESAFE_API_KEY", SecretStr(""))
    agent = create_agent(
        ScriptedModel(
            responses=[
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "propor_registrar_saida",
                            "id": "proposal",
                            "args": {
                                "batchId": 1,
                                "quantity": 2,
                                "justification": "Consumo informado",
                            },
                        }
                    ],
                ),
                AIMessage(content="Fim da rodada."),
            ]
        ),
        tools=[operational.PROPOSAL_TOOLS[Operation.CONSUME]],
        middleware=[judge_module.jev_antes_da_tool],
    )
    marker = current_context.set(context_for())
    try:
        output = agent.invoke(
            {"messages": [HumanMessage(content="Consumimos 2 kg do lote 1.")]}
        )
    finally:
        current_context.reset(marker)
    approved = isinstance(verdict, float) and verdict >= 0.7
    assert get_db().approvals.count_documents({}) == int(approved)
    assert get_db().execution_mock.count_documents({}) == 0
    assert events == (
        ["jev", "tool"] if approved else [] if verdict == "missing" else ["jev"]
    )
    tool_message = next(m for m in output["messages"] if m.type == "tool")
    assert tool_message.tool_call_id == "proposal"
    if not approved:
        assert tool_message.status == "error"
        assert json.loads(tool_message.content)["code"] == "JEV_TOOL_BLOQUEADA"
        assert json.loads(tool_message.content)["executed"] is False


def test_pre_tool_considera_fatos_anteriores_e_nao_avalia_resultado_inexistente(
    jev_transport,
):
    def handler(request):
        payload = json.loads(request.content)
        state = payload["state"]
        assert state["pergunta"] == "Consumimos 2 kg de arroz."
        assert state["fatos"] == "Arroz: lote 7, saldo 10 kg."
        assert json.loads(state["parametros"]) == {"batchId": 7, "quantity": 2}
        assert "resposta" not in state
        return httpx.Response(200, json=jev_response(0.95))

    jev_transport(handler)
    result = judge_module.avaliar_chamada_tool(
        [
            HumanMessage(content="Consumimos 2 kg de arroz."),
            ToolMessage(content="Arroz: lote 7, saldo 10 kg.", tool_call_id="read"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "propor_registrar_saida",
                        "id": "proposal",
                        "args": {"batchId": 7, "quantity": 2},
                    }
                ],
            ),
        ],
        "propor_registrar_saida",
        {"batchId": 7, "quantity": 2},
        "Cria proposta, não executa a saída.",
        {},
    )
    assert result.confianca == 0.95


def test_limiar_confianca_juiz_e_0_7():
    assert LIMIAR_CONFIANCA_JUIZ == 0.7


def test_fatos_vazios_explica_que_nenhuma_tool_foi_chamada():
    assert "nenhuma ferramenta" in FATOS_VAZIOS.lower()
