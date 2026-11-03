import asyncio

import httpx
import openai
import pytest
from google.genai.errors import ClientError
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, SecretStr

from iai.app.core.assistant import llms, rodizio
from iai.app.core.assistant.rodizio import (
    ComRodizio,
    Registro,
    Vigia,
    classificar_falha,
    montar,
    registro,
    tentar_em_rodizio,
)


@pytest.fixture(autouse=True)
def registro_limpo():
    registro.limpar()
    yield
    registro.limpar()


def resposta_http(status, texto="", cabecalhos=None):
    return httpx.Response(
        status,
        headers=cabecalhos,
        json={"error": {"message": texto}},
        request=httpx.Request("POST", "https://provedor.exemplo/v1"),
    )


def erro_openai(status, texto="falhou", cabecalhos=None):
    classe = {
        401: openai.AuthenticationError,
        429: openai.RateLimitError,
        500: openai.InternalServerError,
    }.get(status, openai.APIStatusError)
    return classe(texto, response=resposta_http(status, texto, cabecalhos), body=None)


class ModeloFalso(BaseChatModel):
    """Responde `resposta` ou levanta `erro`; guarda cada chamada em `chamadas`."""

    resposta: str = "ok"
    erro: Exception | None = None
    chamadas: list = Field(default_factory=list)

    @property
    def _llm_type(self) -> str:
        return "falso"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.chamadas.append(kwargs)
        if self.erro:
            raise self.erro
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.resposta))]
        )

    def bind_tools(self, tools, **kwargs) -> Runnable:
        # O LangChain só reaplica o método em todos os candidatos se ele anunciar que devolve um Runnable.
        return self.bind(tools=tools, **kwargs)


def candidato(rotulo, **campos):
    return rotulo, ModeloFalso(callbacks=[Vigia(rotulo)], **campos)


def texto(saida):
    return saida.content


# --- classificação das falhas -----------------------------------------------------------------
def test_429_usa_o_retry_after_do_cabecalho():
    falha = classificar_falha(erro_openai(429, cabecalhos={"retry-after": "12"}))
    assert (falha.motivo, falha.status, falha.espera) == ("limite", 429, 12.0)


def test_429_sem_dica_espera_um_minuto():
    assert classificar_falha(erro_openai(429)).espera == rodizio.ESPERA_LIMITE


def test_429_do_gemini_le_o_tempo_da_mensagem():
    corpo = {
        "error": {
            "code": 429,
            "status": "RESOURCE_EXHAUSTED",
            "message": "You exceeded your current quota. requests per minute limit exceeded on Free Tier.\n"
            "Please retry in 36.5s.",
        }
    }
    falha = classificar_falha(ClientError(429, corpo))
    assert falha.motivo == "limite"
    assert falha.espera == pytest.approx(36.5)


def test_429_da_groq_le_minutos_e_segundos_da_mensagem():
    erro = erro_openai(
        429, "Rate limit reached for tokens per day (TPD). Please try again in 7m12.5s."
    )
    falha = classificar_falha(erro)
    assert falha.motivo == "limite"
    # o limite é diário: a espera sugerida (432 s) sobe para o piso do limite diário
    assert falha.espera == rodizio.ESPERA_LIMITE_DIARIO


def test_429_com_espera_longa_sugerida_respeita_o_teto():
    erro = erro_openai(429, "Please try again in 5h.")
    assert classificar_falha(erro).espera == rodizio.ESPERA_MAXIMA


def test_429_por_falta_de_credito_na_openai_nao_e_limite_de_minuto():
    erro = erro_openai(429, "{'type': 'insufficient_quota', 'code': 'insufficient_quota'}")
    falha = classificar_falha(erro)
    assert (falha.motivo, falha.espera) == ("credito", rodizio.ESPERA_CREDITO)


def test_402_do_openrouter_e_credito():
    falha = classificar_falha(erro_openai(402, "Insufficient credits"))
    assert (falha.motivo, falha.espera) == ("credito", rodizio.ESPERA_CREDITO)


@pytest.mark.parametrize("status", [401, 403, 404])
def test_chave_ou_modelo_recusado_fica_em_espera_longa(status):
    falha = classificar_falha(erro_openai(status))
    assert (falha.motivo, falha.espera) == ("acesso", rodizio.ESPERA_ACESSO)


def test_gemini_chave_invalida_vem_como_400_mas_e_acesso():
    corpo = {"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}
    assert classificar_falha(ClientError(400, corpo)).motivo == "acesso"


@pytest.mark.parametrize("erro", [erro_openai(500), erro_openai(503)])
def test_erro_do_provedor_espera_pouco(erro):
    falha = classificar_falha(erro)
    assert (falha.motivo, falha.espera) == ("indisponivel", rodizio.ESPERA_INDISPONIVEL)


def test_timeout_e_indisponibilidade():
    erro = openai.APITimeoutError(httpx.Request("POST", "https://provedor.exemplo"))
    assert classificar_falha(erro).motivo == "indisponivel"


def test_pedido_ruim_nao_deixa_o_candidato_em_espera():
    falha = classificar_falha(erro_openai(400, "schema inválido"))
    assert (falha.motivo, falha.espera) == ("pedido", 0.0)
    assert classificar_falha(ValueError("qualquer outra")).espera == 0.0


def test_status_e_lido_da_causa_do_erro():
    try:
        try:
            raise erro_openai(429, cabecalhos={"retry-after": "9"})
        except openai.RateLimitError as causa:
            raise RuntimeError("encapsulado") from causa
    except RuntimeError as exc:
        assert classificar_falha(exc).espera == 9.0


# --- registro: espera e ordem -------------------------------------------------------------------
class Relogio:
    def __init__(self):
        self.agora = 1000.0

    def __call__(self):
        return self.agora


def test_candidato_volta_quando_a_espera_termina():
    relogio = Relogio()
    reg = Registro(relogio)
    reg.esfriar("a#1", 30)
    assert reg.restante("a#1") == 30
    assert reg.ordem(["a#1", "b#1"]) == [1, 0]
    relogio.agora += 31
    assert reg.restante("a#1") == 0
    assert reg.ordem(["a#1", "b#1"]) == [0, 1]


def test_espera_maior_nao_e_encurtada_por_uma_menor():
    reg = Registro(Relogio())
    reg.esfriar("a#1", 100)
    reg.esfriar("a#1", 10)
    assert reg.restante("a#1") == 100


def test_todos_em_espera_continuam_na_lista_do_que_volta_antes_ao_que_volta_depois():
    reg = Registro(Relogio())
    reg.esfriar("a#1", 300)
    reg.esfriar("b#1", 20)
    reg.esfriar("c#1", 60)
    assert reg.ordem(["a#1", "b#1", "c#1"]) == [1, 2, 0]


def test_chaves_do_mesmo_grupo_se_revezam_e_a_preferencia_entre_grupos_fica():
    reg = Registro(Relogio())
    rotulos = ["luna", "g#1", "g#2", "g#3", "z"]
    primeiros = [reg.ordem(rotulos) for _ in range(4)]
    assert primeiros == [
        [0, 1, 2, 3, 4],
        [0, 2, 3, 1, 4],
        [0, 3, 1, 2, 4],
        [0, 1, 2, 3, 4],
    ]


def test_revezamento_pula_a_chave_em_espera():
    reg = Registro(Relogio())
    reg.esfriar("g#2", 60)
    assert [reg.ordem(["g#1", "g#2", "g#3"]) for _ in range(2)] == [
        [0, 2, 1],
        [2, 0, 1],
    ]


# --- vigia -------------------------------------------------------------------------------------
def test_vigia_poe_em_espera_e_loga_sem_segredo(caplog):
    erro = erro_openai(429, "chave sk-abcdefghijklmnopqrstuvwxyz1234 estourou")
    with caplog.at_level("WARNING"):
        Vigia("groq#1").on_llm_error(erro)
    assert registro.restante("groq#1") > 0
    assert "groq#1" in caplog.text and "limite" in caplog.text
    assert "sk-abcdefghijklmnop" not in caplog.text


def test_vigia_nao_esfria_por_erro_do_pedido():
    Vigia("groq#1").on_llm_error(ValueError("formato"))
    assert registro.restante("groq#1") == 0


# --- cadeia com rodízio -----------------------------------------------------------------------
def test_429_passa_para_o_proximo_sem_erro_para_quem_chamou():
    primeiro = candidato("pri#1", erro=erro_openai(429))
    segundo = candidato("seg#1", resposta="do segundo")
    cadeia = montar([primeiro, segundo])
    assert texto(cadeia.invoke("oi")) == "do segundo"
    assert len(primeiro[1].chamadas) == 1


def test_candidato_em_espera_e_pulado_sem_ida_a_rede():
    primeiro = candidato("pri#1", erro=erro_openai(429))
    segundo = candidato("seg#1", resposta="do segundo")
    cadeia = montar([primeiro, segundo])
    cadeia.invoke("a")
    cadeia.invoke("b")
    cadeia.invoke("c")
    assert len(primeiro[1].chamadas) == 1
    assert len(segundo[1].chamadas) == 3


def test_candidato_volta_a_ser_usado_depois_da_espera(monkeypatch):
    relogio = Relogio()
    novo = Registro(relogio)
    monkeypatch.setattr(rodizio, "registro", novo)
    primeiro = candidato("pri#1", erro=erro_openai(429, cabecalhos={"retry-after": "30"}))
    segundo = candidato("seg#1", resposta="do segundo")
    cadeia = montar([primeiro, segundo])
    cadeia.invoke("a")
    assert novo.restante("pri#1") == 30
    relogio.agora += 31
    primeiro[1].erro = None
    primeiro[1].resposta = "do primeiro de novo"
    assert texto(cadeia.invoke("b")) == "do primeiro de novo"


def test_pedido_ruim_tenta_o_proximo_mas_nao_esfria_ninguem():
    primeiro = candidato("pri#1", erro=erro_openai(400, "schema"))
    segundo = candidato("seg#1", resposta="ok do segundo")
    cadeia = montar([primeiro, segundo])
    assert texto(cadeia.invoke("a")) == "ok do segundo"
    assert registro.restante("pri#1") == 0
    cadeia.invoke("b")
    assert len(primeiro[1].chamadas) == 2


def test_todos_falham_sobe_o_primeiro_erro():
    primeiro_erro = erro_openai(429, "primeiro")
    cadeia = montar(
        [candidato("a#1", erro=primeiro_erro), candidato("b#1", erro=erro_openai(500))]
    )
    with pytest.raises(openai.RateLimitError) as aberto:
        cadeia.invoke("oi")
    assert aberto.value is primeiro_erro


def test_todos_em_espera_ainda_sao_tentados_como_ultimo_recurso():
    a = candidato("a#1", resposta="a voltou")
    b = candidato("b#1", resposta="b voltou")
    registro.esfriar("a#1", 600)
    registro.esfriar("b#1", 100)
    cadeia = montar([a, b])
    assert texto(cadeia.invoke("oi")) == "b voltou"


def test_bind_tools_mantem_o_rodizio_e_a_ordem_dos_rotulos():
    primeiro = candidato("pri#1", erro=erro_openai(429))
    segundo = candidato("seg#1", resposta="do segundo")
    cadeia = montar([primeiro, segundo]).bind_tools([{"name": "ferramenta"}])
    assert isinstance(cadeia, ComRodizio)
    assert cadeia.rotulos == ["pri#1", "seg#1"]
    assert texto(cadeia.invoke("oi")) == "do segundo"
    assert segundo[1].chamadas[0]["tools"] == [{"name": "ferramenta"}]
    cadeia.invoke("de novo")
    assert len(primeiro[1].chamadas) == 1


def test_stream_troca_de_candidato_antes_do_primeiro_pedaco():
    primeiro = candidato("pri#1", erro=erro_openai(429))
    segundo = candidato("seg#1", resposta="stream do segundo")
    cadeia = montar([primeiro, segundo])
    pedacos = list(cadeia.stream("oi"))
    assert "".join(p.content for p in pedacos) == "stream do segundo"


def test_ainvoke_tambem_passa_para_o_proximo():
    primeiro = candidato("pri#1", erro=erro_openai(429))
    segundo = candidato("seg#1", resposta="async do segundo")
    cadeia = montar([primeiro, segundo])
    assert texto(asyncio.run(cadeia.ainvoke("oi"))) == "async do segundo"
    assert registro.restante("pri#1") > 0


def test_um_unico_candidato_funciona_como_antes():
    cadeia = montar([candidato("unico#1", resposta="só eu")])
    assert texto(cadeia.invoke("oi")) == "só eu"


# --- chamadas diretas (áudio, embeddings) -------------------------------------------------------
def test_tentar_em_rodizio_devolve_o_primeiro_que_responde():
    chamados = []

    def chamar(valor):
        chamados.append(valor)
        if valor == "ruim":
            raise erro_openai(429)
        return f"resultado de {valor}"

    saida = tentar_em_rodizio([("x#1", "ruim"), ("x#2", "bom")], chamar)
    assert saida == "resultado de bom"
    assert registro.restante("x#1") > 0
    tentar_em_rodizio([("x#1", "ruim"), ("x#2", "bom")], chamar)
    assert chamados == ["ruim", "bom", "bom"]


def test_tentar_em_rodizio_sobe_o_primeiro_erro_quando_todos_falham():
    primeiro = erro_openai(429, "um")

    def chamar(valor):
        raise primeiro if valor == 1 else erro_openai(500)

    with pytest.raises(openai.RateLimitError):
        tentar_em_rodizio([("y#1", 1), ("y#2", 2)], chamar)


# --- montagem dos candidatos em llms.py -------------------------------------------------------
def test_luna_so_entra_com_chave_e_a_ordem_e_openrouter_depois_openai(monkeypatch):
    monkeypatch.setattr(llms, "OPENROUTER_API_KEY", SecretStr(""))
    monkeypatch.setattr(llms, "OPENAI_API_KEY", SecretStr(""))
    assert llms._luna(10) == []
    monkeypatch.setattr(llms, "OPENAI_API_KEY", SecretStr("sk-openai"))
    assert [r for r, _ in llms._luna(10)] == ["openai/luna"]
    monkeypatch.setattr(llms, "OPENROUTER_API_KEY", SecretStr("sk-or"))
    luna = llms._luna(10)
    assert [r for r, _ in luna] == ["openrouter/luna", "openai/luna"]
    assert "openrouter.ai" in str(luna[0][1].openai_api_base)
    assert all(m.max_retries == 0 for _, m in luna)


def test_uma_entrada_por_chave_de_gemini_e_groq(monkeypatch):
    monkeypatch.setattr(
        llms, "GEMINI_API_KEYS", (SecretStr("g1"), SecretStr("g2"), SecretStr("g3"))
    )
    monkeypatch.setattr(llms, "GROQ_API_KEYS", (SecretStr("q1"), SecretStr("q2")))
    assert [r for r, _ in llms._gemini()] == ["gemini#1", "gemini#2", "gemini#3"]
    grupo = llms._groq("openai/gpt-oss-20b", temperature=0.0)
    assert [r for r, _ in grupo] == ["groq/gpt-oss-20b#1", "groq/gpt-oss-20b#2"]
    assert all(m.max_retries == 0 for _, m in grupo)


def test_sem_chave_nenhuma_o_modelo_ainda_e_construido(monkeypatch):
    monkeypatch.setattr(llms, "GEMINI_API_KEYS", ())
    monkeypatch.setattr(llms, "GROQ_API_KEYS", ())
    assert [r for r, _ in llms._gemini()] == ["gemini#1"]
    assert [r for r, _ in llms._groq("openai/gpt-oss-20b")] == ["groq/gpt-oss-20b#1"]


def test_papeis_expoem_o_que_o_resto_do_codigo_usa():
    for papel in (llms.llm_especialista, llms.llm_rapido, llms.llm_guardrail, llms.llm_gemini):
        assert isinstance(papel, ComRodizio)
        assert len(papel.rotulos) == 1 + len(papel.fallbacks)
    assert llms.llm_especialista.rotulos[0].startswith("gemini")
    assert llms.llm_rapido.rotulos[0].startswith("groq/gpt-oss-20b")
    assert all(r.startswith("groq/gpt-oss-safeguard") for r in llms.llm_guardrail.rotulos)


def test_maior_preferencia_do_especialista_e_a_luna_depois_gemini_depois_groq(monkeypatch):
    monkeypatch.setattr(llms, "OPENROUTER_API_KEY", SecretStr("sk-or"))
    monkeypatch.setattr(llms, "OPENAI_API_KEY", SecretStr("sk-openai"))
    cadeia = llms.montar(
        [
            *llms._luna(10),
            *llms._gemini(pensante=True),
            *llms._groq("openai/gpt-oss-20b"),
        ]
    )
    assert [r.split("#")[0] for r in cadeia.rotulos] == [
        "openrouter/luna",
        "openai/luna",
        "gemini",
        "groq/gpt-oss-20b",
    ]


def test_ferramentas_chegam_a_todos_os_provedores_reais_da_cadeia(monkeypatch):
    """Se `bind_tools` ficasse só no primeiro candidato, a troca de provedor perderia as tools sem avisar."""
    monkeypatch.setattr(llms, "OPENROUTER_API_KEY", SecretStr("sk-or"))
    monkeypatch.setattr(llms, "OPENAI_API_KEY", SecretStr("sk-openai"))
    monkeypatch.setattr(llms, "GEMINI_API_KEYS", (SecretStr("g1"), SecretStr("g2")))
    monkeypatch.setattr(llms, "GROQ_API_KEYS", (SecretStr("q1"), SecretStr("q2")))
    cadeia = llms.montar(
        [*llms._luna(10), *llms._gemini(pensante=True), *llms._groq("openai/gpt-oss-20b")]
    )

    def consultar(produto: str) -> str:
        """Consulta o estoque de um produto."""
        return produto

    ligada = cadeia.bind_tools([consultar])
    assert isinstance(ligada, ComRodizio)
    assert ligada.rotulos == cadeia.rotulos
    candidatos = [ligada.runnable, *ligada.fallbacks]
    assert len(candidatos) == 6
    assert all("tools" in c.kwargs for c in candidatos)

    estruturada = cadeia.with_structured_output(Entrada)
    assert isinstance(estruturada, ComRodizio)
    assert len(estruturada.fallbacks) == 5


# --- schema aceito pela OpenAI ---------------------------------------------------------------------
class Entrada(BaseModel):
    quantidade: float
    codigo: str = Field(pattern=r"^[A-Z]{3}$")


def test_openai_ve_o_schema_das_tools_sem_pattern_com_lookaround():
    # O Pydantic já gerou isso para Decimal (versões antigas); o pattern é escrito à mão para não depender dela.
    class ComLookaround(BaseModel):
        # json_schema_extra: o validador do Pydantic não aceita lookaround, mas o schema enviado à OpenAI traz.
        quantidade: str = Field(
            json_schema_extra={"pattern": r"^(?!^[-+.]*$)[+-]?0*\d*\.?\d*$"}
        )
        codigo: str = Field(pattern=r"^[A-Z]{3}$")

    ferramenta = convert_to_openai_tool(ComLookaround)
    assert "(?!" in str(ferramenta)

    modelo = llms.ChatOpenAISemLookaround(model="gpt-6-luna", api_key="sk-teste")
    carga = modelo._get_request_payload(
        [HumanMessage(content="oi")], tools=[ferramenta]
    )
    assert "(?" not in str(carga["tools"])
    # só o pattern com lookaround sai; os outros continuam valendo
    assert "^[A-Z]{3}$" in str(carga["tools"])


def test_schema_sem_lookaround_nao_e_alterado():
    ferramenta = convert_to_openai_tool(Entrada)
    assert llms._sem_lookaround(ferramenta) == ferramenta


# --- chaves, preços e segredos --------------------------------------------------------------------
def test_chaves_em_ordem_numerica_sem_vazias_nem_repetidas(monkeypatch):
    from iai.app.core import config

    monkeypatch.setenv("ZZ_KEY", "primeira")
    monkeypatch.setenv("ZZ_KEY_10", "decima")
    monkeypatch.setenv("ZZ_KEY_2", "segunda")
    monkeypatch.setenv("ZZ_KEY_3", "   ")
    monkeypatch.setenv("ZZ_KEY_4", "segunda")
    monkeypatch.setenv("ZZ_KEY_X", "ignorada")
    monkeypatch.setenv("ZZ_KEYS_2", "ignorada")
    assert [c.get_secret_value() for c in config._chaves("ZZ_KEY")] == [
        "primeira",
        "segunda",
        "decima",
    ]


def test_sem_a_primeira_chave_a_numerada_assume(monkeypatch):
    from iai.app.core import config

    monkeypatch.delenv("YY_KEY", raising=False)
    monkeypatch.setenv("YY_KEY_2", "so-a-segunda")
    assert [c.get_secret_value() for c in config._chaves("YY_KEY")] == ["so-a-segunda"]


def test_preco_de_modelo_com_data_ou_provedor_no_nome():
    from iai.app.core.infra import metrics

    base = metrics.preco_do_modelo("gpt-6-luna")
    assert base == (0.10, 0.50)
    assert metrics.preco_do_modelo("gpt-6-luna-2026-09-22") == base
    assert metrics.preco_do_modelo("openai/gpt-6-luna-20260922") == base
    assert metrics.preco_do_modelo("openai/gpt-oss-20b") == (0.075, 0.30)
    assert metrics.preco_do_modelo("modelo-que-nao-existe") == (0.0, 0.0)


def test_chaves_novas_sao_removidas_de_logs_e_argumentos():
    from iai.app.core.infra.privacy import remove_secrets

    suja = "a sk-or-v1-0123456789abcdef0123 b AQ.Ab8RN6J0123456789abcdef c sk-proj-abcdefghijklmnop1234"
    assert "0123456789abcdef" not in remove_secrets(suja)
    assert remove_secrets("task-force-assessment-longer-than-sixteen") == (
        "task-force-assessment-longer-than-sixteen"
    )
