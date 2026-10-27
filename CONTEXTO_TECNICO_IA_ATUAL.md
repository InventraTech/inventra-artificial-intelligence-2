# Contexto técnico atual da IAI — Inventra

Revisão: 26/10/2026. Backend Python local, branch `fix/human-in-the-loop`, baseado no commit
`7aef497` (checkpoint de Artefatos/HITL/Paralelismo), consolidado com `main` em `ae7155c` (#22 e #23).
Este documento descreve código implementado; não certifica implantação ou serviços externos.
Caminhos partem da raiz do repositório.

## 1. Arquitetura e invariantes

FastAPI autentica Bearer e cria `RequestContext` por `ContextVar`. LangGraph executa
guardrails, roteador, um especialista e saída. Agentes LangChain chamam ferramentas;
O gate autoriza individualmente cada ferramenta live; o escopo define quais consultam o JEV. Negócio é consultado
pela API Spring ou por `MockGateway`; armazenamento usa MongoDB ou mongomock.

Há três evoluções implementadas: artefatos de apresentação, confirmação do próprio
solicitante e fan-out/fan-in seletivo de leituras. Não há frontend neste repositório.

Invariantes:

- Consultas não representam aprovação nem execução de alterações.
- `propor_*` apenas persiste a ação exata, com hash, snapshot e justificativa.
- Somente o próprio solicitante autenticado pode confirmar/rejeitar/executar sua ação.
  Supervisor não confirma ações alheias. As permissões de cargo continuam obrigatórias.
- Quando exigido pelo escopo, JEV é anterior à ferramenta e não substitui confirmação humana.
- Nenhum JWT é persistido para execução futura.
- Usuários, inclusive Supervisor, permanecem limitados à cozinha autenticada.
- Mock/demo são identificados; resultados demonstrativos não são dados de produção.

## 2. Arquivos centrais

| Arquivo/pasta | Função |
|---|---|
| `iai/app/main.py` | FastAPI, rotas, CORS, saúde, worker de expiração a cada 30 segundos |
| `iai/app/core/config.py` | Modos, ambiente, URLs, chaves, premissas financeiras e data operacional |
| `iai/app/core/schemas.py` | Chat, artefatos, ações para revisão, histórico, métricas e estado externo |
| `iai/app/core/routes/` | Chat/áudio/histórico, consultas, confirmação, execução e métricas |
| `iai/app/core/assistant/graph.py` | Agentes, conexões e execução completa; helper textual e resposta tipada |
| `iai/app/core/assistant/prompts.py` | Persona, regras, temporalidade, leituras independentes e seleção visual |
| `iai/app/core/assistant/llms.py` | Modelos e fallback |
| `iai/app/core/assistant/respostas.py` | Revisão determinística das propostas e diagnóstico seguro de falhas |
| `iai/app/core/assistant/guardrail.py` | Regex de injeção, anonimização, moderação, escopo e restauração de PII |
| `iai/app/core/assistant/tool_gate.py` | Candidatos, política de lotes, JEV, permissões de uso único e evidências |
| `iai/app/core/assistant/judge.py` | REST TypeSafe e política determinística sobre probabilidades |
| `iai/app/core/assistant/artifacts.py` | Projeção de dados operacionais, escolha tipada e montagem visual |
| `iai/app/core/assistant/memory.py` | Sessões, últimas mensagens, encerramento e resumos |
| `iai/app/core/tools/` | Consulta operacional, propostas, FAQ e histórico |
| `iai/app/core/inventra/domain.py` | `Query`, `Action`, 16 operações, schemas e `ROLE_OPERATIONS` |
| `iai/app/core/inventra/acoes.py` | Entrada compatível das tools, delega ao HITL durável |
| `iai/app/core/inventra/approvals.py` | Snapshot, hash, decisão, revalidação, expiração, auditoria e execução |
| `iai/app/core/inventra/business.py` | Interface `BusinessGateway`, adaptadores Spring/mock |
| `iai/app/core/infra/` | Identidade, Mongo, busca textual, remoção de segredos e métricas |
| `iai/app/extras/demo.py` | Interpretação limitada sem LLM, plano de leituras e ToolNode demonstrativo |
| `iai/app/core/arquivos/` | Validação de upload, extração de texto/visão, guardrails, chunks, embeddings e Qdrant |
| `iai/app/core/tools/documentos.py` | Tool de recuperação de documentos autorizados |
| `iai/app/core/routes/arquivos.py` | Upload, listagem e exclusão de arquivos por identidade/cozinha |
| `iai/app/extras/audio.py` | Transcrição Groq/Whisper |
| `iai/app/extras/mcp_server.py` | Ferramentas por perfil via stdio |
| `iai/data/faq_inventra.jsonl` | 24 pares documentais `p`/`r`; não é estoque atual |

## 3. Modelos e agentes

- Especialistas estoquista/comprador/supervisor: `gemini-2.5-flash`, temperatura 0,2,
  top_p 0,95; fallback Groq `openai/gpt-oss-20b`, temperatura 0.
- Roteador, FAQ, orquestrador, resumo e seleção de apresentação: Groq `openai/gpt-oss-20b`.
- Moderação: Groq `openai/gpt-oss-safeguard-20b`.
- JEV: TypeSafe REST `https://api.typesafe.ai/v1/systemone`, padrão `jev-1.13.0`.
- Transcrição: Groq `whisper-large-v3-turbo`, português.

Roteador tem `buscar_historico`; FAQ tem um subgrafo de busca determinística com `faq_retriever`, JEV e uma etapa de redação (fallback literal se o modelo falhar). Os três especialistas têm
`consultar_inventra`, `consultar_solicitacoes_iai`, histórico, FAQ, `buscar_documentos` e suas propostas.
Estoquista: 7 operações de lotes/estoque/inventário. Comprador: 6 de requisições/fornecedores.
Supervisor: todas as 16, incluindo criar/editar/desativar produto. Rota por intenção não
concede cargo: JEV e serviço de propostas aplicam `ROLE_OPERATIONS` da identidade.

Orquestrador formata texto sem ferramentas; há filtro determinístico contra novos tokens
numéricos. Ele não comprova fidelidade de cada frase. Não há novos especialistas ou A2A.
Data/hora de Brasília é reconstruída por chamada; auditoria usa UTC.

## 4. Rodada do chat

1. `POST /chat` valida campos e identidade; user_id divergente retorna 403.
2. Define contexto com sessão e turn_id (`request_id` ou UUID). Reidrata últimas 30 mensagens
   da sessão ativa por usuário/cozinha. Sem checkpointer ou MemorySaver.
3. Remove padrões de segredos, mascara e-mail/CPF/RG/telefone, aplica regex e moderação.
   Depois classifica escopo. Erros dos classificadores bloqueiam entrada.
4. Roteador responde saudação/histórico ou emite `ROUTE=estoquista|comprador|supervisor|faq`.
   Protocolo inválido cai em supervisor, sem ampliar permissões. Vendas e comparações entre
   cozinhas recebem explicação de indisponibilidade, sem inventar dados.
5. Especialista consulta fontes. Leituras independentes podem compartilhar um lote;
   chamadas dependentes continuam em rodadas sequenciais.
6. Gate registra candidato e avalia cada ferramenta. Fan-in do ToolNode entrega resultados
   ao especialista antes de sua próxima geração. Especialistas operacionais passam ao
   orquestrador; FAQ segue à saída. Saída restaura tokens de PII da rodada.
7. Função externa extrai propostas confirmadas e resultados operacionais desta rodada.
   Propostas substituem a redação livre por revisão determinística, sem execução.
8. Se há consulta elegível, seleção estruturada escolhe formato/fonte; backend monta os dados.
9. Retorna `resposta`, `artifacts` e `pending_actions`; salva pergunta anonimizada e resposta
   textual no histórico e registra métricas técnicas. Áudio acrescenta `transcricao`.

`executar_fluxo_chat()` retorna `ChatResponse`; `executar_fluxo_assessor()` permanece wrapper
textual para consumidores internos. Artefatos não são persistidos como visualizações no histórico.
Ausência de chaves generativas no live retorna 503; falhas inesperadas, 502 seguro.

## 5. Artefatos

`artifacts` é sempre lista de zero ou um elemento. União Pydantic discriminada por `type`:

- Tabela: `type=table`, `columns=[{key,label}]`, `rows=[{...}]`.
- Gráfico: `type=chart`, `chart_type=bar|pie`, `labels`, `datasets=[{label,data}]`, `unit`.
- Ambos têm `source` e `mocked`.

Fontes precisam ser ToolResults operacionais bem-sucedidos da rodada atual, associados à
chamada original. No live, precisam também de registro do gate `EXECUTED`. FAQ/histórico/documentos enviados não
são fontes numéricas para artefatos; saldos vêm das consultas operacionais. Projeção usa whitelist de campos dos contratos.

O modelo rápido escolhe somente `kind=none|table|bar|pie` e `source_id`; não recebe poder de
gerar arrays numéricos. Demo usa regras determinísticas. O código copia valores, valida
representação numérica, dimensões, categorias, campos essenciais e unidades comparáveis.
Recusa fonte incompleta; não fabrica totais ou valores ausentes. Barras representam quantidades
compatíveis ou contagem de requisições por status. Pizza conta registros por status/categoria.
Não soma kg e litros. Texto acompanhante explica finalidade/campos, sem narrar valores.

Perguntas escalares/conceituais/ações permanecem em texto. Fontes com poucos registros ou
campos insuficientes podem não gerar artefato, mesmo com pedido explícito. Falha de apresentação
preserva a consulta textual e informa a limitação. Não há HTML, renderização ou biblioteca gráfica.

## 6. JEV e concorrência

`after_model` captura candidatos PENDING_JEV com fingerprint, identidade, rodada e tentativa.
Verifica ferramenta, schema, perfil e segredos. Quando exigido pelo escopo, envia pedido/evidências sanitizados à TypeSafe.
Política exige positivos >= 0,70 e riscos <= 0,30 por padrão. Decisões: APPROVE,
RETRY_SPECIALIST (até três tentativas de reparo), ASK_USER ou REJECT. Falha não autoriza ferramenta.
`protect_tool` exige permissão de uso único no escopo live, vinculada a nome/argumentos.

Concorrência máxima de quatro. Somente lotes com múltiplas leituras da whitelist
`consultar_inventra`, `faq_retriever`, `buscar_historico`, `buscar_documentos` liberam o bloqueio abrangente.
Cada chamada conserva autorização própria no gate. Captura evidências anteriores ao lote; atualizações
compartilhadas usam lock curto. ToolNode propaga ContextVar e agrega mensagens/Commands.
Lotes com proposta conservam apenas a primeira etapa para o modelo continuar sequencialmente.
`JEV_ESCOPO=escrita` é o padrão: propostas `propor_*` exigem JEV; leituras mantêm validações locais, sem TypeSafe.
`JEV_ESCOPO=todas` exige JEV individual também nas leituras. A dispensa é registrada como `jev_dispensado=leitura`.

Fontes com erro não fornecem dados. Se uma leitura funciona e outra fica indisponível,
permite síntese parcial sem executar a rejeitada. Rejeições de segurança ou pedido de
esclarecimento mantêm parada segura. A relevância e independência das fontes são orientadas
pelos prompts e, quando o escopo exige JEV, pela avaliação individual de necessidade/fundamentação.

REST direto, MCP e demo não são turnos live e não usam TypeSafe; schemas, identidade e
confirmação de escrita continuam aplicados. O demo usa grafo compilado com ToolNode para
leituras concorrentes reais, mas interpretação por regras continua limitada.

## 7. HITL e persistência

`pending_actions` fornece ID, hash, operação, parâmetros, justificativa, estado, prazo e
snapshot dos alvos. Dados vêm da proposta persistida, não da redação do modelo.

Fluxo: AGUARDANDO_APROVACAO -> RECUSADA/EXPIRADA ou APROVADA_PENDENTE_INTEGRACAO
-> EM_EXECUCAO -> CONCLUIDA/FALHOU/RESULTADO_INCERTO.

`POST /approvals/{id}/decision`: decision approve/reject, proposal_hash e motivo opcional;
usa Bearer fresco e exige o próprio solicitante, mesma cozinha e operação permitida ao perfil
atual. Aprovar tenta executar imediatamente. `/execute` retoma aprovação válida quando possível.
Decisão legada feita por terceiro não pode executar sob a política nova.

Reaproveita snapshot, hash, auditoria, prazo de sete dias e compare-and-set Mongo.
Snapshot é comparado novamente antes do envio; alvo alterado exige nova proposta.
Chave idempotente de ferramenta deriva de rodada+ação; request_id estável ajuda retries,
mas não cacheia resposta inteira. Operações em propostas distintas não são atômicas.
Escrita incerta nunca é repetida automaticamente. Sem reconciliação/idempotência distribuída.

Inbox é pessoal. Supervisor conserva consulta administrativa da própria cozinha, sem decidir
pelos demais. Edição de contagem e remoção lógica de item seguem bloqueadas no Spring;
mock demonstra essas operações. Confirmação não contorna permissões Spring.

Banco `dbIAI`: sessions, approvals, metrics; mock usa business_mock/execution_mock/mock_counters.
Mongo real persiste entre processos/reinícios; mongomock é memória por processo e perde dados.
Índices existentes bastam; não há TTL removendo auditoria. Worker expira pendências e marca
EM_EXECUCAO parado por dez minutos como RESULTADO_INCERTO. Não há notificações externas.

## 8. Integrações e interfaces

SpringGateway usa JWT atual e endpoints já mapeados de produtos, fornecedores, estoque,
lotes, requisições, inventários, alertas e catálogos. Não acessa PostgreSQL diretamente.
Estoque geral usa `/api/kitchens/{id}/stock`; produtos é catálogo global, e a ferramenta exige
nome/ID para essa consulta. `/queries` chama gateway sem essa restrição específica da tool.
Query oferece filtros tipados, paginação e mine para requisições/inventários. ToolResult informa
ok/code/data/source/mocked/incomplete. Não existe paginação automática geral.

Rotas: /chat, /chat/audio, /chat/encerrar, /chat/historico/{user_id}, /chat/mensagens/{doc_id},
/queries, /queries/capabilities, /approvals, /approvals/inbox, /approvals/{id},
/approvals/{id}/decision, /approvals/{id}/execute, /metrics, /health e /docs.
Requisições nascem em DRAFT; itens só mudam nesse estado, cancelamento também admite UNDER_REVIEW/APPROVED.
Capabilities indica confirmation_actor=requester; a listagem de agentes ainda não enumera
roteador/FAQ nem faq_retriever dos especialistas: graph.py é referência das tools conectadas.

Áudio multipart até 10 MB; Groq/Whisper obrigatório inclusive no demo. Não grava arquivo;
retorna transcrição original mais resposta estruturada. MCP usa stdio, IAI_MCP_TOKEN e
11/10/20 tools por perfil; cada chamada é rodada nova e pode gerar proposta distinta.
A confirmação é HTTP pelo solicitante. Logs MCP vão para stderr.

Memória: últimos 30 textos ativos; encerramento gera resumo via modelo rápido ou resumo
demo limitado. Histórico busca até 200 resumos recentes e devolve até cinco pela ferramenta.
FAQ retorna até quatro entradas por ranking textual, sem embeddings. Documentos enviados usam embeddings e Qdrant; isso não altera a memória textual de conversa. Não há treinamento de modelo próprio.
PII restaurada na resposta pode ser persistida; métricas não guardam texto, mas tracing
LangSmith é canal externo separado e não deve ser presumido anônimo.

## 9. Configuração e limites

Padrões sem configuração: auth=spring, business=spring, storage=mongo, llm=live.
`.env.example` seleciona mock/mock/mock/demo. Prioridade: ambiente > iai/.env > iai/app/.env.
Alterações exigem reinício. Negócio Spring exige autenticação Spring; HTTPS fora de localhost.
Identidade vem de GET /api/users/me; não se confia em cargo/cozinha enviados pelo modelo.

Variáveis: IAI_AUTH_MODE, IAI_BUSINESS_MODE, IAI_STORAGE_MODE, IAI_LLM_MODE,
INVENTRA_API_URL (localhost:8080), INVENTRA_HTTP_TIMEOUT (10s), MONGO_CONNECTION,
GEMINI_API_KEY, GROQ_API_KEY, TYPESAFE_API_KEY, JEV_MODEL, JEV_APPROVAL_THRESHOLD,
JEV_TIMEOUT (10s), CORS_ORIGINS, IAI_MCP_TOKEN e premissas METRICAS_*.
Preços são tabela estática; IAI_MODEL_PRICES_JSON não é lida. /health só testa armazenamento.

Python 3.12; LangChain 1.4.0, core 1.6.2, LangGraph 1.2.11, google-genai 4.2.1,
langchain-groq 1.1.2, Pydantic 2, FastAPI, PyMongo, httpx, Groq, MCP e python-multipart.
As três evoluções não exigiram dependências próprias; a integração de documentos acrescenta as dependências da main listadas abaixo.

Dependências incorporadas da main: qdrant-client>=1.15,<2.0, pypdf>=5,<7 e pillow>=11,<14.

Métricas: callback inclui seleção de apresentação, tokens e tempos; preços desconhecidos
ficam zero na estimativa, sem significar gratuidade. Custo TypeSafe, transcrição, resumo e
infraestrutura não integram o custo generativo do chat. ROI é premissa. Resolução usa marcador
jev:concluido de ferramenta, não prova execução de negócio nem verdade integral da redação.

Fora do escopo: múltiplas cozinhas, vendas, frontend, OCR, e-mail, WhatsApp, novos especialistas,
A2A, busca web, streaming, renderização e novas operações de negócio. O índice vetorial de documentos já foi incorporado da main.

## 10. Validação e continuidade

A suíte cobre contratos visuais, origem dos valores, ausência de preenchimento fictício,
confirmação pelo solicitante, perfil atual, hash, snapshot, repetição/concorrência, decisões
legadas, sobreposição de JEV/tools com barreiras, fan-in, falha parcial e isolamento de contexto.
O teste Mongo opt-in prepara proposta em um processo e confirma em outro, com banco temporário.
Validação local: **394 testes aprovados, 6 ignorados, cobertura 91,56%**; Ruff e mypy passaram.
Os serviços externos e o teste de retomada com Mongo real ficaram desativados; não houve
validação de integração em produção. O baseline anterior à consolidação era 91,42%; a base original tinha 90,34%.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest --cov=iai --cov-report=term --cov-fail-under=91.42 -q
.\.venv\Scripts\python.exe -m ruff check iai
.\.venv\Scripts\python.exe -m mypy iai --follow-imports=skip
```

Serviços externos são opt-in: RUN_LLM_TESTS, RUN_MONGO_TESTS + MONGO_TEST_CONNECTION,
RUN_SPRING_TESTS + SPRING_TEST_TOKEN, RUN_JEV_TESTS. Mongo de teste usa iai_test_<uuid>.
Suíte simulada não comprova qualidade dos modelos reais, contrato Spring implantado ou
persistência real sem executar o opt-in. Dockerfile e .dockerignore existem; build não validado.

Ao adicionar operação, revisar domain -> perfil -> ferramenta -> prompt -> JEV -> snapshot
-> confirmação -> gateway -> testes. Para artefatos, preservar origem e cópia determinística;
para paralelismo, não ampliar whitelist sem revisar efeitos, dependências e segurança.


## Documentos incorporados da main

POST /arquivos ingere PDF, DOCX, XLSX, PPTX, CSV, TXT e imagens; GET /arquivos lista os
visíveis; DELETE /arquivos/{id} remove documentos autorizados. Produtos/fornecedores
continuam sem exclusão física: exclusão de documentos é um recurso separado.
Validação estrutural e guardrails antecedem indexação. Arquivos são divididos em trechos,
embeddings usam Gemini e a persistência documental usa Qdrant com isolamento por cozinha/usuário.
Supervisor pode compartilhar um documento com a cozinha. Imagens recebem descrição de visão;
PDF escaneado sem texto não ganha OCR automaticamente e pode ser recusado.
Sem Qdrant configurado, o recurso fica indisponível e o restante da IAI segue funcionando.

Variáveis: QDRANT_URL, QDRANT_API_KEY, QDRANT_COLLECTION (inventra_documentos),
EMBEDDING_MODEL (gemini-embedding-001), EMBEDDING_DIM (768), ARQUIVOS_MAX_BYTES (10 MB),
ARQUIVOS_MAX_POR_ENVIO (10), ARQUIVOS_MAX_POR_USUARIO (50), ARQUIVOS_MAX_CARACTERES (120000),
ARQUIVOS_GUARDRAIL_LLM (on). Esses guardrails de ingestão são separados do gate JEV das tools.
O demo também reconhece referências simples a documentos para consulta; continua sendo intérprete limitado.

Os testes usam Qdrant em memória e EmbedderLocal; isso não comprova integração com Qdrant Cloud
ou embeddings Gemini reais. Fontes documentais não são autorização de escrita, confirmação humana
ou saldo operacional atual. Nunca usar números de uma planilha enviada para preencher um gráfico
operacional sem uma consulta autorizada que os confirme.
