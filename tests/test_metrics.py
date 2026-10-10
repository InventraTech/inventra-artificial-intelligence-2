from datetime import datetime, timezone
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

import iai.app.metrics as m


def _turno(custo=0.01, status="ok", resolvida=True, erros_componentes=None, ms=1000.0):
    return {
        "timestamp": datetime.now(timezone.utc),
        "latencia_total_ms": ms,
        "status": status,
        "nos": [{"no": "roteador", "ms": ms / 2, "erro": False}],
        "chamadas": [{"modelo": "gemini-2.5-flash", "tokens_entrada": 10, "tokens_saida": 5,
                      "custo_usd": custo, "latencia_ms": 100.0}],
        "tokens_entrada": 10,
        "tokens_saida": 5,
        "custo_usd": custo,
        "erros_componentes": erros_componentes or [],
        "resolvida": resolvida,
    }


class _ColFalsa:
    def __init__(self, docs):
        self.docs = docs
        self.filtros = []

    def find(self, filtro=None, *_a, **_k):
        self.filtros.append(filtro)
        return self.docs


def test_custo_usd_por_modelo():
    assert m.custo_usd("gemini-2.5-flash", 1_000_000, 1_000_000) == 0.30 + 2.50


def test_custo_usd_modelo_desconhecido_custa_zero():
    assert m.custo_usd("x", 10, 10) == 0.0


def test_status_da_requisicao():
    assert m.status_da_requisicao(["roteador", "juiz:aprovado"], falhou=False) == "ok"
    assert m.status_da_requisicao(["guardrail_insulto:prompt_injection"], falhou=False) == "bloqueado"
    assert m.status_da_requisicao(["guardrail_escopo:fora_de_escopo"], falhou=False) == "bloqueado"
    assert m.status_da_requisicao(["guardrail_insulto:aprovado", "guardrail_escopo:aprovado"], falhou=False) == "ok"
    assert m.status_da_requisicao([], falhou=True) == "erro"


def test_foi_resolvida():
    assert m.foi_resolvida(["roteador", "juiz:aprovado"])
    assert not m.foi_resolvida(["juiz:reprovado_final:x"])


def test_coletor_mede_nos_do_grafo_e_ignora_subnos():
    coletor = m.ColetorMetricas()
    top, interno = uuid4(), uuid4()
    coletor.on_chain_start({}, {}, run_id=top, name="juiz",
                           metadata={"langgraph_node": "juiz", "langgraph_checkpoint_ns": "juiz:abc"})
    coletor.on_chain_start({}, {}, run_id=interno, name="model",
                           metadata={"langgraph_node": "model", "langgraph_checkpoint_ns": "estoquista:abc|model:def"})
    coletor.on_chain_end({}, run_id=interno)
    coletor.on_chain_end({"agentes_chamados": ["juiz:aprovado"]}, run_id=top)

    assert [n["no"] for n in coletor.nos] == ["juiz"]
    assert coletor.agentes_chamados == ["juiz:aprovado"]


def test_coletor_registra_tokens_e_custo_da_chamada():
    coletor = m.ColetorMetricas()
    run = uuid4()
    coletor.on_chat_model_start({}, [], run_id=run, metadata={"ls_model_name": "openai/gpt-oss-20b"})
    mensagem = AIMessage(
        content="oi",
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
        response_metadata={"model_name": "models/gemini-2.5-flash"},
    )
    coletor.on_llm_end(LLMResult(generations=[[ChatGeneration(message=mensagem)]]), run_id=run)

    c = coletor.chamadas[0]
    assert (c["modelo"], c["tokens_entrada"], c["tokens_saida"]) == ("gemini-2.5-flash", 10, 5)
    assert c["custo_usd"] > 0


def test_coletor_registra_erros():
    coletor = m.ColetorMetricas()
    coletor.on_tool_error(ValueError("x"), run_id=uuid4())
    coletor.on_llm_error(RuntimeError("y"), run_id=uuid4())
    assert coletor.erros == ["tool:ValueError", "llm:RuntimeError"]


@pytest.fixture
def turnos_exemplo(monkeypatch):
    turnos = [
        _turno(custo=0.01, ms=1000),
        _turno(custo=0.03, ms=3000),
        _turno(custo=0.02, status="erro", resolvida=False, erros_componentes=["llm:X"], ms=2000),
        _turno(custo=0.02, status="bloqueado", resolvida=False, ms=2000),
    ]
    col = _ColFalsa(turnos)
    monkeypatch.setattr(m, "col_metricas", col)
    return col


def test_gerar_metricas_cobre_as_cinco_demandas(turnos_exemplo):
    r = m.gerar_metricas()

    assert r.requisicoes == 4
    # latência total e entre agentes
    assert r.latencia.total_media_ms == 2000
    assert r.latencia.por_no[0].no == "roteador"
    # índice de erros: bloqueio não conta
    assert r.erros.indice_erros == 0.25
    assert r.erros.indice_erros_componentes == 0.25
    assert r.erros.bloqueios == 1
    # custo e custo por resolução
    assert r.custo.total_usd == 0.08
    assert r.custo.por_modelo[0].modelo == "gemini-2.5-flash"
    assert r.resolucao.resolucoes == 2
    assert r.resolucao.custo_por_resolucao_usd == 0.04
    # ROI
    assert r.roi.retorno_usd == 2 * m.VALOR_POR_RESOLUCAO_USD
    assert r.roi.roi == pytest.approx((r.roi.retorno_usd - 0.08) / 0.08)
    # cenários 100 e 1000 usuários semanais
    assert [c.usuarios for c in r.cenarios] == [100, 1000]
    assert r.cenarios[1].custo_usd == pytest.approx(1000 * m.REQUISICOES_POR_USUARIO_SEMANA * 0.02)


def test_periodo_mes_amplia_janela_e_projecao(turnos_exemplo):
    semana = m.gerar_metricas(periodo="semana", usuarios=[100])
    mes = m.gerar_metricas(periodo="mes", usuarios=[100])

    assert (semana.dias_observados, mes.dias_observados) == (7, 30)
    assert mes.cenarios[0].custo_usd == pytest.approx(semana.cenarios[0].custo_usd * 52 / 12, rel=1e-3)


def test_gerar_metricas_sem_dados(monkeypatch):
    monkeypatch.setattr(m, "col_metricas", _ColFalsa([]))
    r = m.gerar_metricas()
    assert r.requisicoes == 0
    assert r.erros.indice_erros == 0.0
    assert r.resolucao.custo_por_resolucao_usd is None
    assert r.roi.roi is None
    assert r.cenarios[0].custo_usd == 0.0


def test_rota_metrics_valida_parametros(turnos_exemplo):
    from iai.app.main import app

    cliente = TestClient(app)

    corpo = cliente.get("/metrics?periodo=mes&usuarios=50&usuarios=5000").json()
    assert corpo["periodo"] == "mes"
    assert [c["usuarios"] for c in corpo["cenarios"]] == [50, 5000]
    assert cliente.get("/metrics?periodo=ano").status_code == 422
    assert cliente.get("/metrics?usuarios=0").status_code == 422


def test_cors_libera_origem_do_front(turnos_exemplo):
    from iai.app.main import app

    resposta = TestClient(app).get("/metrics", headers={"Origin": "http://localhost:3000"})
    assert resposta.headers["access-control-allow-origin"] == "http://localhost:3000"
