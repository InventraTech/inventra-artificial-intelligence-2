from datetime import UTC, datetime

from iai.app.core.assistant import prompts


def test_contexto_temporal_usa_horario_de_brasilia_em_portugues():
    agora = datetime(2026, 10, 22, 2, 30, tzinfo=UTC)  # 23:30 do dia 21 em Brasília

    texto = prompts.contexto_temporal(agora)

    assert "21/10/2026 23:30:00" in texto


def test_contexto_temporal_muda_a_cada_chamada():
    antes = prompts.contexto_temporal(datetime(2026, 1, 5, 12, 0, tzinfo=UTC))
    depois = prompts.contexto_temporal(datetime(2026, 12, 25, 12, 0, tzinfo=UTC))

    assert "05/01/2026 09:00:00" in antes
    assert "25/12/2026 09:00:00" in depois


def test_prompt_com_contexto_mantem_base_e_anexa_data():
    resultado = prompts.com_contexto_temporal(prompts.FAQ_SYSTEM_PROMPT)

    assert resultado.startswith(prompts.FAQ_SYSTEM_PROMPT.rstrip())
    assert "### CONTEXTO TEMPORAL" in resultado


def test_protocolo_do_roteador_e_regras_de_escopo_permanecem():
    assert (
        "ROUTE=[estoquista|comprador|supervisor|faq]" in prompts.ROTEADOR_SYSTEM_PROMPT
    )
    assert "resource=estoque" in prompts.COMMON_OPERATIONAL_PROMPT
    assert "NUNCA peça ID" in prompts.COMMON_OPERATIONAL_PROMPT
