# IAI INVENTRA

FastAPI + LangChain + LangGraph para conhecimento documental, consultas operacionais e
propostas de alteração aprovadas por Supervisor. Esta branch não altera Spring nem frontend.

## Executar localmente

Use Python 3.12 em um ambiente virtual. No Windows, o comando `python` pode
apontar para outra versão; por isso selecione `py -3.12` ao criar o ambiente.
Se essa versão não estiver instalada, use `py install 3.12` no gerenciador Python.
Copie `iai/.env.example` para `iai/.env` para habilitar **explicitamente a demonstração**:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path iai/.env)) { Copy-Item iai/.env.example iai/.env }
.\.venv\Scripts\python.exe -m uvicorn iai.app.main:app --host 127.0.0.1 --port 8000
```

Não sobrescreva um `.env` existente. A demonstração usa usuários fixos, estoque de
exemplo, Mongo simulado em memória e intérprete limitado sem provedores de IA.
Ela não é autenticação real, não consulta estoque real e perde dados ao reiniciar.

Use o executável de `.venv` também ao instalar dependências adicionais ou executar
testes. Não é necessário ativar o ambiente nem alterar a política do PowerShell.
Se aparecer `pg_config` ou uma exigência de compilador C++ durante a instalação,
verifique a versão do Python e o arquivo de requisitos: a IAI não utiliza `jq` nem
`psycopg2-binary`, e o acesso operacional ao PostgreSQL é feito pela API Spring.

Para persistir solicitações e histórico (identidade e dados de negócio ainda simulados),
use `IAI_STORAGE_MODE=mongo` e `MONGO_CONNECTION` apontando para um MongoDB.

### Testar no celular (mesma rede Wi-Fi)

1. Suba o servidor com `--host 0.0.0.0` em vez de `127.0.0.1`.
2. Descubra o IP do PC (PowerShell):
   ```powershell
   (Get-NetIPAddress -AddressFamily IPv4 | Where-Object { $_.InterfaceAlias -match "Wi-Fi|Ethernet" }).IPAddress
   ```
3. No celular, abra `http://<IP-do-PC>:8000/docs` e use **Authorize** com o token do perfil
   (`demo-estoquista`, `demo-comprador` ou `demo-supervisor`) para testar cada cargo.
4. Se não abrir, libere a porta (PowerShell como administrador):
   ```powershell
   New-NetFirewallRule -DisplayName "IAI 8000" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private
   ```
   Redes que isolam aparelhos (comum em Wi-Fi corporativo) bloqueiam o acesso: use o hotspot do
   celular ou um túnel (`ngrok http 8000`). Não exponha `0.0.0.0` em rede pública no modo demo.

Documentação interativa: `http://127.0.0.1:8000/docs`.
Estado/configuração: `GET /health`; modos também aparecem nos cabeçalhos `X-IAI-*`.

## Identidade demonstrativa

Use `Authorization: Bearer <identificador-demo>`:

| Identificador | Papel | Cozinha |
|---|---|---|
| demo-estoquista | Estoquista | 1 |
| demo-comprador | Comprador | 1 |
| demo-supervisor | Supervisor | 1 |
| demo-outra-cozinha | Supervisor | 2 |

Esses identificadores são **dados públicos de exemplo**, não credenciais de produção.
Não exponha os modos mock publicamente.

## Chat e consultas

`POST /chat` preserva `session_id`, `user_id`, `pergunta` e resposta `{"resposta": "..."}`.
O `user_id` deve corresponder à identidade autenticada. `request_id` opcional pode ser
reutilizado pelo frontend ao repetir a mesma requisição; é a chave da rodada para propostas.

```json
{
  "session_id": "cozinha-1-chat",
  "user_id": "demo-estoquista",
  "request_id": "mensagem-001",
  "pergunta": "Fiz um almoço usando 10 kg de arroz e 40 kg de carne."
}
```

No demo, esse exemplo gera duas propostas. O saldo permanece igual até aprovação.
Esse intérprete tem cobertura limitada; compreensão geral de linguagem natural usa os
agentes `IAI_LLM_MODE=live` e chaves Gemini/Groq. API de negócio e armazenamento podem
continuar simulados enquanto se valida o provedor real.

`POST /chat/audio` (multipart: `session_id`, `user_id`, `request_id` opcional e o arquivo
`audio` em webm, ogg, mp3, m4a, mp4 ou wav, até 10 MB) transcreve a fala com Whisper
(`whisper-large-v3-turbo`, Groq) e segue o mesmo fluxo e a mesma autenticação do `/chat`.
Responde `{"transcricao": "...", "resposta": "..."}`. Exige `GROQ_API_KEY`; o áudio não é
gravado, só o texto transcrito entra no histórico.

`POST /queries` oferece as mesmas consultas tipadas sem depender de LLM.
Exemplo: `{"resource":"validade","days":7}`.
`GET /queries/capabilities` informa schemas, operações e limitações.

## Tools por agente

O roteador escolhe o agente pela intenção da mensagem; as tools de cada agente e quem pode
propor cada operação (`ROLE_OPERATIONS`) estão em `GET /queries/capabilities`
(`agents` e `operations[].allowed_roles`).

| Agente | Tools |
|---|---|
| Roteador | `buscar_historico` |
| FAQ | `faq_retriever` |
| Estoquista | `consultar_inventra`, `consultar_solicitacoes_iai`, `buscar_historico`, `faq_retriever` + propor: criar lote, registrar entrada, registrar saída, ajustar estoque, registrar inventário, registrar contagem, alterar contagem |
| Comprador | as mesmas de consulta/histórico/FAQ + propor: criar requisição, adicionar/remover item, cancelar requisição, cadastrar/desativar fornecedor |
| Supervisor | todas as anteriores + propor: criar/editar/desativar produto |

`consultar_inventra` é uma única tool com o parâmetro `resource` (produtos, estoque, lotes,
validade, fornecedores, requisições, inventários, alertas, catálogos etc.). Nenhuma tool aprova
ou executa: as `propor_*` só criam propostas.

## Integração com a API Spring e com agentes externos (MCP)

**Sistema integrado:** API REST do Inventra (Spring), em `INVENTRA_API_URL`. Os agentes não
acessam o PostgreSQL; leem e propõem alterações pelas tools, que chamam a API
(`SpringGateway`, em `business.py`).

**Autenticação:** o JWT do usuário vai em `Authorization: Bearer` em toda chamada. A identidade
(usuário, cozinha, cargo) é validada em `GET /api/users/me` e nunca vem do cliente ou do modelo.
O token fica só no contexto da requisição (não entra em prompt, histórico ou log).

| Tool (agente) | Endpoints consumidos |
|---|---|
| `consultar_inventra` (todos) | `GET /api/products`, `/api/suppliers`, `/api/stock-batches` (+ `/expiring-soon`, `/low-stock`), `/api/products/{id}/kitchen-parameters`, `/api/products/{id}/suppliers`, `/api/requisitions` (+ `/{id}/items`), `/api/inventories` (+ `/{id}/counts`), `/api/alerts`, `/api/categories`, `/api/units`, `/api/kitchens` |
| `propor_*` (estoquista, comprador, supervisor) | só criam proposta no Mongo da IAI; depois da aprovação, a execução chama `POST/PUT/PATCH` em `/api/products`, `/api/suppliers`, `/api/stock-batches` (`restock`, `consume`, `adjust`), `/api/requisitions` (`items`, `cancel`) e `/api/inventories` (`counts`) |

**Erros:** a integração nunca derruba o agente. API fora do ar, timeout, status inesperado ou
resposta inválida viram um resultado com código (`SERVICO_INDISPONIVEL`, `RESPOSTA_INVALIDA`,
`SEM_PERMISSAO`, `NAO_ENCONTRADO`), que o agente explica ao usuário. Escritas com resultado
incerto (`RESULTADO_INCERTO`) não são repetidas automaticamente. Cobertos em
`tests/test_business.py` com falhas HTTP simuladas.

**Como validar contra a API real:** `RUN_SPRING_TESTS=1` e `SPRING_TEST_TOKEN` (veja Testes), ou
`POST /queries` com `{"resource": "produtos"}` e um JWT no **Authorize** do `/docs`.

**Agentes externos:** o servidor MCP abaixo expõe as mesmas tools a qualquer cliente MCP.
Não há implementação de A2A.

## Servidor MCP

As mesmas tools dos agentes podem ser usadas por Cursor, Cline ou qualquer cliente MCP, sem
subir a API. O servidor (`iai/app/mcp_server.py`) usa transporte **stdio**: o cliente inicia o
processo e conversa por entrada/saída padrão (não há porta nem URL).

- **Identidade:** vem de `IAI_MCP_TOKEN` (JWT do Spring), validado como no `/chat`. Cada
  perfil vê só as tools do seu agente (estoquista 11, comprador 10, supervisor 20) e a regra
  `ROLE_OPERATIONS` continua valendo.
- **Nada executa:** as `propor_*` só criam propostas; a aprovação continua em `/approvals`.
  Cada chamada de tool é uma rodada nova, então repetir a mesma proposta cria outra proposta.
- **Logs** vão para o stderr; o stdout é o canal do protocolo (nunca use `print`).

Configuração do Cursor (`.cursor/mcp.json`) ou do Cline (`cline_mcp_settings.json`), com
caminhos absolutos da **sua** máquina (`python -c "import sys; print(sys.executable)"`):

```json
{
  "mcpServers": {
    "assessor-inventra": {
      "command": "C:\\caminho\\para\\python.exe",
      "args": ["C:\\caminho\\para\\inventra-artificial-intelligence-2\\iai\\app\\mcp_server.py"],
      "env": { "IAI_MCP_TOKEN": "<JWT do Spring>" }
    }
  }
}
```

Para testar sem o Spring, acrescente em `env` os modos de demonstração
(`IAI_AUTH_MODE`, `IAI_BUSINESS_MODE`, `IAI_STORAGE_MODE` = `mock`, `IAI_LLM_MODE` = `demo`) e use
`"IAI_MCP_TOKEN": "demo-supervisor"`. No Cursor, deixe o chat em **Agent** (em Ask as tools MCP
não executam) e confira os logs em View → Output → MCP Logs. Não coloque o token no Git.

## Aprovação

1. Proposta criada por ferramenta do agente ou `POST /approvals`.
2. Supervisor vê `GET /approvals/inbox`, restrito à sua cozinha.
3. Envia `POST /approvals/{id}/decision` com decisão e hash da proposta.
4. Execução única quando disponível. Resultado mock continua explicitamente identificado.
5. Solicitante acompanha `GET /approvals/{id}` ou listagem de suas solicitações.

Quem pode **propor** o quê (chat ou `POST /approvals`; fora disso, 403):
estoquista → lotes, entradas, saídas, ajustes, inventários e contagens; comprador →
requisições e fornecedores; supervisor → tudo, inclusive produtos. O mapa fica em
`ROLE_OPERATIONS` (`domain.py`) e aparece em `GET /queries/capabilities` (`allowed_roles`).
A API Spring continua aplicando as próprias permissões na execução.

Prazo: 7 dias. Autoaprovação de Supervisor da mesma cozinha é permitida.
Nenhuma tool do modelo aprova ou executa. Não há exclusão física pela IAI.
Operações em itens/lotes distintos são propostas independentes, sem atomicidade de grupo.

## Modos reais e dependências

Por padrão, sem configuração demo, os modos são `spring/spring/mongo/live`.
Configure em ambiente ou `iai/.env`:

| Variável | Uso |
|---|---|
| IAI_AUTH_MODE | mock ou spring |
| IAI_BUSINESS_MODE | mock ou spring |
| IAI_STORAGE_MODE | mock ou mongo |
| IAI_LLM_MODE | demo ou live |
| INVENTRA_API_URL | base Spring, localhost em HTTP ou endereço remoto HTTPS |
| INVENTRA_HTTP_TIMEOUT | segundos; padrão 10 |
| MONGO_CONNECTION | MongoDB; não inclua credenciais no Git |
| GEMINI_API_KEY / GROQ_API_KEY | provedores para agentes live |
| CORS_ORIGINS | origens exatas do frontend, separadas por vírgula (padrão `http://localhost:3000,http://localhost:5173`) |
| METRICAS_REQ_USUARIO_SEMANA / METRICAS_VALOR_RESOLUCAO_USD | premissas do `/metrics` (padrão 10 e 1.50) |

API real não aceita identidade mock. JWT é validado por `GET /api/users/me`, sem
decodificação não verificada e sem segredo de assinatura compartilhado. Tokens permanecem
apenas no contexto HTTP; não são colocados em prompts, checkpoints ou histórico.

A execução real como outro solicitante depende de delegação ainda ausente no Spring.
A aprovação permanece `APROVADA_PENDENTE_INTEGRACAO`. O solicitante pode apresentar
JWT fresco em `POST /approvals/{id}/execute`; a API volta a aplicar suas permissões.
Edição de contagem e remoção lógica de item também têm dependências externas.
Não inventamos endpoints para essas capacidades.

Uma falha após iniciar envio pode ser `RESULTADO_INCERTO`. A IAI não repete a escrita.
Reconciliação/idempotência completa entre serviços depende do backend.
A página de aprovação está em outro repositório e não foi criada aqui.

## Testes

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest --cov=iai --cov-report=term-missing --cov-fail-under=70
.\.venv\Scripts\python.exe -m ruff check iai
.\.venv\Scripts\python.exe -m mypy iai --follow-imports=skip
```

Os testes comuns usam Mongo/dados/identidades simulados. O grafo também é exercitado com
agentes LangChain e um modelo roteirizado; isso não mede a qualidade de Gemini/Groq.
Testes opcionais, somente com serviços explicitamente disponibilizados:

- `RUN_LLM_TESTS=1` e chaves reais: FAQ pelos provedores, negócio/Mongo continuam mockados.
- `RUN_MONGO_TESTS=1` e `MONGO_TEST_CONNECTION`: persistência e decisão atômica em base
  temporária exclusiva `iai_test_<uuid>`; não usa `dbIAI`.
- `RUN_SPRING_TESTS=1` e `SPRING_TEST_TOKEN`: leitura de identidade/catálogo real, sem escrita.


## Observabilidade e métricas SRE

O backend só expõe os números; a tela é feita no front-end (React) por outra pessoa, consumindo
`GET /metrics` (exige `Authorization: Bearer`; só considera a cozinha do usuário e, para quem não é
SUPERVISOR, apenas as próprias requisições). O contrato completo (campos, tipos e descrições) aparece em `/docs` (Swagger).

```
GET /metrics?periodo=semana            # padrão: histórico dos últimos 7 dias, projeção semanal
GET /metrics?periodo=mes               # histórico dos últimos 30 dias, projeção mensal
GET /metrics?periodo=mes&usuarios=50&usuarios=5000   # cenários customizados (padrão: 100 e 1000)
```

| Demanda | Campo da resposta |
|---|---|
| Custo estimado para 100 e 1000 usuários semanais | `cenarios[]` (`usuarios`, `requisicoes`, `custo_usd`, `retorno_usd`, `roi`) |
| Latência interagentes | `latencia.por_no[]` (média e p95 de cada nó do grafo) |
| Tempo total de resposta | `latencia.total_media_ms`, `latencia.total_p95_ms` |
| Índice de erros | `erros.indice_erros`, `erros.indice_erros_componentes`, `erros.bloqueios` |
| Custo / retorno (ROI) | `roi.retorno_usd`, `roi.roi` |
| Custo por resolução | `resolucao.custo_por_resolucao_usd` (+ `resolucao.taxa_resolucao`) |
| Custo observado | `custo.total_usd`, `custo.medio_por_requisicao_usd`, `custo.por_modelo[]` |
| Premissas usadas | `premissas` (para o front exibir junto dos números) |

Fluxo dos dados:

1. `metrics.py` → `ColetorMetricas` é um callback passado no `config` de cada `invoke` do grafo. Ele
   anota a latência de cada nó, cada chamada de LLM (modelo, tokens, custo, latência) e os erros de
   LLM/ferramenta. O texto do prompt **não** é guardado.
2. `registrar_requisicao` grava um documento por requisição em `dbIAI.metrics` (Mongo), com `user_id` e `kitchen_id`, sem conteúdo da conversa. Falha ao
   gravar nunca derruba o chat.
3. `GET /metrics` agrega esses documentos na janela do `periodo`.
4. Com `LANGCHAIN_TRACING_V2=true`, cada `/chat` também vira um trace `iai_chat` no LangSmith (tag
   `iai`, metadata `session_id`/`user_id`), útil para investigar um caso específico.

Definições:

- **Índice de erros** = requisições que terminaram em exceção ÷ requisições. Bloqueio do guardrail
  de entrada é o sistema funcionando e **não** conta (aparece em `erros.bloqueios`).
  `indice_erros_componentes` também conta erros de LLM/ferramenta que o fallback recuperou.
- **Resolvida** = um especialista respondeu e o juiz aprovou (`juiz:aprovado`). Bloqueio, resposta
  direta do roteador e reprovação final do juiz não contam.
- **Projeção** = usuários × `requisicoes_por_usuario_semana` × semanas do período (1 para semana,
  52/12 para mês) × custo médio por requisição **observado**. Sem tráfego registrado, vem zerada.
- **Premissas** (`config.py`): preços por modelo (conferir nas tabelas oficiais),
  `METRICAS_REQ_USUARIO_SEMANA` (padrão 10) e `METRICAS_VALOR_RESOLUCAO_USD` (padrão 1.50). Todos em
  USD. Não entram no custo o resumo de sessão (`/chat/encerrar`) nem a infra (Mongo, hospedagem).
  O custo é estimativa, não a fatura: o LangSmith aplica a tabela dele.
- **CORS**: origens liberadas em `CORS_ORIGINS` (padrão `http://localhost:3000` e `:5173`); ao
  publicar, definir no `.env` a URL real do front.
