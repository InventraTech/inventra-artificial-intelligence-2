# IAI INVENTRA

FastAPI + LangChain + LangGraph para conhecimento documental, consultas operacionais e
pedidos de alteração (validados, **ainda não executados**: o human in the loop está por implementar).
Esta branch não altera Spring nem frontend.

Para compreender a arquitetura, as regras essenciais e os limites reais antes de trabalhar no código,
consulte [Contexto da IAI para outras IAs](CONTEXTO_IAI_PARA_IAS.md).

O chat **live** usa o [gate JEV pré-execução de tools](docs/JEV_GATE.md).
Cada chamada é registrada e julgada antes de executar; isso não substitui a confirmação
humana das alterações de negócio (ver [Human in the loop](#human-in-the-loop-a-implementar)). Configure `TYPESAFE_API_KEY` para esse fluxo.
O demo determinístico continua disponível sem JEV real.

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

Para persistir o histórico de conversas e as métricas (identidade e dados de negócio ainda simulados),
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
reutilizado pelo frontend ao repetir a mesma requisição; identifica a rodada.

```json
{
  "session_id": "cozinha-1-chat",
  "user_id": "demo-estoquista",
  "request_id": "mensagem-001",
  "pergunta": "Fiz um almoço usando 10 kg de arroz e 40 kg de carne."
}
```

No demo, esse exemplo gera dois pedidos validados e **não executados**; o saldo não muda.
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
| FAQ | sem tools: o nó `responder_faq` busca no FAQ em código e o modelo só redige (se falhar, devolve o texto literal do FAQ) |
| Estoquista | `consultar_inventra`, `buscar_historico`, `buscar_documentos`, `faq_retriever` + propor: criar lote, registrar entrada, registrar saída, ajustar estoque, registrar inventário, registrar contagem, alterar contagem |
| Comprador | as mesmas de consulta/histórico/documentos/FAQ + propor: criar requisição, adicionar/remover item, cancelar requisição, cadastrar/desativar fornecedor |
| Supervisor | todas as anteriores + propor: criar/editar/desativar produto |

`consultar_inventra` é uma única tool com o parâmetro `resource` (produtos, estoque, lotes,
validade, fornecedores, requisições, inventários, alertas, catálogos etc.). Nenhuma tool executa:
as `propor_*` só validam o pedido e respondem que nada foi executado.

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
| `propor_*` (estoquista, comprador, supervisor) | hoje só validam (nada é gravado nem executado). Quando o HITL existir, a execução chamará `POST/PUT/PATCH` em `/api/products`, `/api/suppliers`, `/api/stock-batches` (`restock`, `consume`, `adjust`), `/api/requisitions` (`items`, `cancel`) e `/api/inventories` (`counts`) |

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
subir a API. O servidor (`iai/app/extras/mcp_server.py`) usa transporte **stdio**: o cliente inicia o
processo e conversa por entrada/saída padrão (não há porta nem URL).

- **Identidade:** vem de `IAI_MCP_TOKEN` (JWT do Spring), validado como no `/chat`. Cada
  perfil vê só as tools do seu agente (estoquista 10, comprador 9, supervisor 19) e a regra
  `ROLE_OPERATIONS` continua valendo.
- **Nada executa:** as `propor_*` só validam o pedido (a confirmação humana ainda não existe).
- **Logs** vão para o stderr; o stdout é o canal do protocolo (nunca use `print`).

Configuração do Cursor (`.cursor/mcp.json`) ou do Cline (`cline_mcp_settings.json`), com
caminhos absolutos da **sua** máquina (`python -c "import sys; print(sys.executable)"`):

```json
{
  "mcpServers": {
    "assessor-inventra": {
      "command": "C:\\caminho\\para\\python.exe",
      "args": ["C:\\caminho\\para\\inventra-artificial-intelligence-2\\iai\\app\\extras\\mcp_server.py"],
      "env": { "IAI_MCP_TOKEN": "<JWT do Spring>" }
    }
  }
}
```

Para testar sem o Spring, acrescente em `env` os modos de demonstração
(`IAI_AUTH_MODE`, `IAI_BUSINESS_MODE`, `IAI_STORAGE_MODE` = `mock`, `IAI_LLM_MODE` = `demo`) e use
`"IAI_MCP_TOKEN": "demo-supervisor"`. No Cursor, deixe o chat em **Agent** (em Ask as tools MCP
não executam) e confira os logs em View → Output → MCP Logs. Não coloque o token no Git.

## Documentos como contexto (arquivos + Qdrant)

O usuário envia arquivos e os agentes passam a consultá-los com a tool `buscar_documentos`.

| Rota | O que faz |
|---|---|
| `POST /arquivos` | multipart: `arquivos` (um ou vários, até `ARQUIVOS_MAX_POR_ENVIO`) e `compartilhar` (opcional; só SUPERVISOR). Pasta = vários arquivos; o caminho relativo no nome do arquivo vira o campo `pasta`. Devolve o status de cada arquivo (`indexado`/`recusado`/`erro`) |
| `GET /arquivos` | documentos do usuário e os compartilhados pelo supervisor da cozinha |
| `DELETE /arquivos/{arquivo_id}` | dono remove o próprio; supervisor remove qualquer um da própria cozinha |

Aceitos: PDF (com texto), `.docx`, `.xlsx`, `.pptx`, `.csv`, `.txt`, `.md`, `.png`, `.jpg`, `.webp`.

**Segurança (nada é gravado em disco nem executado)**, em `iai/app/core/arquivos/`:

1. `seguranca.py`: lista de permitidos por extensão; recusa executáveis/scripts (`.exe`, `.dll`, `.bat`, `.ps1`, `.js`...),
   compactados (`.zip`, `.rar`, `.7z`...), macros (`.docm`, `.xlsm`...), HTML/SVG, Office antigo e dupla extensão
   (`fatura.exe.pdf`). Confere a **assinatura real** do conteúdo (um `.exe` renomeado para `.pdf` é barrado).
   PDF sem JavaScript/Launch/anexos/ações automáticas/senha; OOXML sem macro, objeto OLE/ActiveX, vínculo externo,
   DOCTYPE/ENTITY e zip bomb; imagem decodificada e **reencodada** (remove metadados e payloads escondidos).
2. `extracao.py`: texto oculto (`vanish`, abas ocultas, caracteres invisíveis/tag characters) é descartado.
3. `guardrail.py`: prompt injection indireta — regras determinísticas + classificador LLM (Gemini, com Groq de reserva),
   fail-closed (erro do classificador = arquivo recusado). Senhas/chaves são mascaradas antes de qualquer envio.
   Só depois de aprovado o arquivo vai para o Qdrant.
4. `descricao_imagem.py`: agente de visão (Gemini) descreve tudo da imagem; ordens escritas na imagem a recusam.
5. Na leitura, `buscar_documentos` filtra por cozinha/usuário no Qdrant, descarta trechos suspeitos e entrega o
   conteúdo ao modelo marcado como dado de terceiros.

Qdrant: uma coleção (`inventra_documentos`) com o campo `tipo` no payload (um índice para todos os formatos, filtrável
por `tipo`) e índices em `kitchen_id`, `user_id`, `arquivo_id`, `tipo`, `compartilhado` e `ordem`. Cada arquivo vira
trechos de ~1000 caracteres (150 de sobreposição; tabelas repetem o cabeçalho). Reenviar o mesmo arquivo substitui o anterior.

Limites conhecidos: não há antivírus por assinatura de malware (é lista de permitidos + validação estrutural);
PDF escaneado sem texto é recusado (envie como imagem); `.doc/.xls/.ppt` antigos e arquivos compactados são recusados.

## Human in the loop (a implementar)

O fluxo antigo de aprovação (coleção `approvals`, rotas `/approvals`, decisão, execução,
prazo de 7 dias) foi **removido**. Hoje as `propor_*` só validam o pedido e devolvem "não
executado". Quem for implementar o HITL parte de **um único ponto**:

`iai/app/core/inventra/acoes.py` → `solicitar_confirmacao(action, justification, ctx)`

- Já pronto e reaproveitável: `snapshot()` (confere perfil, IDs e estado dos registros),
  `domain.Action` / `ROLE_OPERATIONS` (operações e quem pode pedir cada uma) e
  `business.gateway().execute(...)` (executa no Spring ou no mock com o JWT do solicitante).
- Falta: guardar a ação pendente, pedir a confirmação a uma pessoa, revalidar e executar
  UMA vez, e devolver ao agente o estado real. O passo a passo está no docstring do arquivo.
- Textos ao usuário: `assistant/respostas.py` (`resposta_de_propostas`), prompts em
  `assistant/prompts.py` ("PEDIDOS DE AÇÃO") e FAQ em `iai/data/faq_inventra.jsonl`.

Quem pode **pedir** o quê (fora disso, 403): estoquista → lotes, entradas, saídas, ajustes,
inventários e contagens; comprador → requisições e fornecedores; supervisor → tudo, inclusive
produtos. O mapa fica em `ROLE_OPERATIONS` (`domain.py`) e aparece em
`GET /queries/capabilities` (`allowed_roles`). Não há exclusão física pela IAI.

## Modos reais e dependências

Por padrão, sem configuração demo, os modos são `spring/spring/mongo/live`.
Configure em ambiente ou `iai/.env`:

Por compatibilidade, `iai/app/.env` também é carregado para variáveis ainda não
definidas. A prioridade é: ambiente do processo, `iai/.env`, `iai/app/.env`.
Os dois arquivos locais permanecem ignorados pelo Git e pelo build Docker.

No Render, adicione `TYPESAFE_API_KEY` em **Environment**, junto às configurações
existentes. `IAI_LLM_MODE` é opcional: o padrão já é `live`; `demo` seleciona apenas
a demonstração offline. Não publique o arquivo `.env` local.

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
| TYPESAFE_API_KEY | TypeSafe/JEV para autorizar tools dos agentes live; falha bloqueia execução |
| JEV_ESCOPO | `escrita` (padrão): o JEV julga só `propor_*`; `todas`: julga qualquer tool |
| JEV_MODEL / JEV_APPROVAL_THRESHOLD / JEV_TIMEOUT | versão `jev-1.13.0`, limiar 0.70, timeout 10 s |
| QDRANT_URL / QDRANT_API_KEY | cluster do Qdrant Cloud para os documentos; sem eles `/arquivos` responde 503 e a IA segue normal |
| QDRANT_COLLECTION / EMBEDDING_MODEL / EMBEDDING_DIM | coleção (`inventra_documentos`), embedding Gemini (`gemini-embedding-001`) e dimensão (768); mudar a dimensão exige outra coleção |
| ARQUIVOS_MAX_BYTES / ARQUIVOS_MAX_POR_ENVIO / ARQUIVOS_MAX_POR_USUARIO / ARQUIVOS_MAX_CARACTERES | limites (10 MB, 10 arquivos por envio, 50 por usuário, 120 mil caracteres indexados por arquivo) |
| ARQUIVOS_GUARDRAIL_LLM | `on` (padrão) ou `off`: classificador LLM de prompt injection nos arquivos |
| CORS_ORIGINS | origens exatas do frontend, separadas por vírgula (padrão `http://localhost:3000,http://localhost:5173`) |
| METRICAS_REQ_USUARIO_SEMANA / METRICAS_VALOR_RESOLUCAO_USD | premissas do `/metrics` (padrão 10 e 1.50) |

API real não aceita identidade mock. JWT é validado por `GET /api/users/me`, sem
decodificação não verificada e sem segredo de assinatura compartilhado. Tokens permanecem
apenas no contexto HTTP; não são colocados em prompts, checkpoints ou histórico.

Quando o HITL existir, a execução real como outro solicitante dependerá de delegação ainda
ausente no Spring (não se usa o JWT de quem confirma no lugar do solicitante).
Edição de contagem e remoção lógica de item também têm dependências externas.
Não inventamos endpoints para essas capacidades.

Uma falha após iniciar envio pode ser `RESULTADO_INCERTO`. A IAI não repete a escrita.
Reconciliação/idempotência completa entre serviços depende do backend.

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
- `RUN_JEV_TESTS=1` e `TYPESAFE_API_KEY`: contrato real de decisões TypeSafe, sem executar tools de negócio.


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
- **Resolvida** = especialista terminou com execução autorizada de tool, sem parada do gate
  nem falha de execução (`jev:concluido`). Não comprova fidelidade de toda a redação final.
  O marcador `juiz:aprovado` continua reconhecido somente para compatibilidade com métricas legadas.
- **JEV:** decisões e eventos técnicos ficam nos documentos da coleção de métricas, sem
  argumentos ou conteúdo da conversa; não alteram o contrato público de `/metrics`.
  Seu custo ainda não está incluído na estimativa financeira LLM; o uso retornado é registrado.
- **Projeção** = usuários × `requisicoes_por_usuario_semana` × semanas do período (1 para semana,
  52/12 para mês) × custo médio por requisição **observado**. Sem tráfego registrado, vem zerada.
- **Premissas** (`config.py`): preços por modelo (conferir nas tabelas oficiais),
  `METRICAS_REQ_USUARIO_SEMANA` (padrão 10) e `METRICAS_VALOR_RESOLUCAO_USD` (padrão 1.50). Todos em
  USD. Não entram no custo o resumo de sessão (`/chat/encerrar`) nem a infra (Mongo, hospedagem).
  O custo é estimativa, não a fatura: o LangSmith aplica a tabela dele.
- **CORS**: origens liberadas em `CORS_ORIGINS` (padrão `http://localhost:3000` e `:5173`); ao
  publicar, definir no `.env` a URL real do front.
