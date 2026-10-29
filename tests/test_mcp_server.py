import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from iai.app.core.infra.storage import get_db
from iai.app.extras.mcp_server import contexto_do_ambiente, criar_servidor
from tests_helpers import context_for


def test_script_inicia_fora_do_diretorio_do_projeto(tmp_path):
    script = Path(__file__).resolve().parents[1] / "iai/app/extras/mcp_server.py"
    ambiente = os.environ.copy()
    ambiente.pop("PYTHONPATH", None)
    ambiente.pop("IAI_MCP_TOKEN", None)
    resultado = subprocess.run(
        [sys.executable, str(script)],
        check=False,
        cwd=tmp_path,
        env=ambiente,
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert resultado.returncode != 0
    assert "[assessor-inventra] Defina IAI_MCP_TOKEN" in resultado.stderr
    assert "ModuleNotFoundError" not in resultado.stderr
    assert resultado.stdout == ""


def nomes_das_tools(token):
    servidor = criar_servidor(context_for(token))
    return {tool.name for tool in asyncio.run(servidor.list_tools())}


def chamar(token, nome, argumentos):
    servidor = criar_servidor(context_for(token))
    resultado = asyncio.run(servidor.call_tool(nome, argumentos))
    return json.loads(resultado.content[0].text)


def test_cada_cargo_enxerga_so_as_tools_do_seu_agente():
    estoquista = nomes_das_tools("demo-estoquista")
    comprador = nomes_das_tools("demo-comprador")
    supervisor = nomes_das_tools("demo-supervisor")

    assert len(estoquista) == 12 and len(comprador) == 10 and len(supervisor) == 21
    assert "propor_registrar_saida" in estoquista - comprador
    assert "propor_criar_requisicao" in comprador - estoquista
    assert "propor_criar_produto" in supervisor - estoquista - comprador
    assert estoquista | comprador <= supervisor


def test_schema_da_tool_vem_do_contrato_do_dominio():
    servidor = criar_servidor(context_for("demo-supervisor"))
    tools = {tool.name: tool for tool in asyncio.run(servidor.list_tools())}

    saida = tools["propor_registrar_saida"].input_schema["properties"]
    assert {"batchId", "quantity", "justification"} <= set(saida)
    assert tools["consultar_inventra"].annotations.read_only_hint is True
    assert tools["propor_registrar_saida"].annotations.read_only_hint is False


def test_consulta_usa_a_identidade_do_ambiente():
    resposta = chamar("demo-estoquista", "consultar_inventra", {"resource": "lotes"})

    assert resposta["ok"] is True and resposta["mocked"] is True
    assert len(resposta["data"]) == 3


def test_proposta_pelo_mcp_nao_executa_e_cada_chamada_e_uma_rodada():
    argumentos = {"batchId": 1, "quantity": "2", "justification": "uso no almoço"}
    primeira = chamar("demo-estoquista", "propor_registrar_saida", argumentos)
    segunda = chamar("demo-estoquista", "propor_registrar_saida", argumentos)

    assert primeira["executed"] is False and primeira["code"] == "AGUARDANDO_APROVACAO"
    assert primeira["proposal"]["id"] != segunda["proposal"]["id"]
    assert get_db().approvals.count_documents({"requester_id": "demo-estoquista"}) == 2


def test_argumento_invalido_e_recusado_sem_criar_proposta():
    servidor = criar_servidor(context_for("demo-estoquista"))
    with pytest.raises(Exception):  # noqa: B017 - o SDK embrulha o erro de validação
        asyncio.run(
            servidor.call_tool("propor_registrar_saida", {"batchId": -1, "quantity": "2"})
        )
    assert get_db().approvals.count_documents({}) == 0


def test_token_ausente_ou_invalido_interrompe_o_servidor(monkeypatch):
    monkeypatch.delenv("IAI_MCP_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        contexto_do_ambiente()

    monkeypatch.setenv("IAI_MCP_TOKEN", "token-inexistente")
    with pytest.raises(SystemExit):
        contexto_do_ambiente()

    monkeypatch.setenv("IAI_MCP_TOKEN", "demo-comprador")
    assert contexto_do_ambiente().identity.role == "COMPRADOR"
