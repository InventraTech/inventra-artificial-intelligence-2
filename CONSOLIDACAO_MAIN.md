# Consolidação de fix/human-in-the-loop com main

Data: 26/10/2026. Pasta: cópia diretamente em Interdisciplinar/inventra-artificial-intelligence-2.
A branch existente foi consolidada por merge, sem reset, rebase, descarte de trabalho ou push.

## Base preservada e commits incorporados

- Checkpoint `7aef497`: registra integralmente o trabalho de Artefatos, HITL e Paralelismo que estava sem commit.
- `d9b3023` (#22): correções no FAQ/JEV, mensagens/diagnósticos e regras de requisições.
- `ae7155c` (#23): arquivos como contexto, extração, visão, guardrails, Qdrant e dependências.

## Mantido da branch

- Chat/áudio tipados com resposta, zero ou um artefato (tabela/barra/pizza) e revisão estruturada de ações.
- Dados dos artefatos copiados de resultados operacionais autorizados; documentos não viram saldo atual.
- HITL Mongo durável: hash, snapshot, prazo, auditoria, idempotência, compare-and-set, revalidação e execução única.
- Somente o próprio solicitante confirma/rejeita/executa, com Bearer fresco e permissões atuais.
- Fan-out seletivo e fan-in; leituras independentes, ContextVar isolado, evidência anterior ao lote e locks curtos.
- Cada tool live continua exigindo JEV individual; propostas e suas dependências continuam sequenciais.
- Modo demo identificado, testes e isolamento por cozinha preservados.

## Incorporado/adaptado da main

- Upload/listagem/exclusão de documentos; PDF, DOCX, XLSX, PPTX, CSV, texto e imagens descritas.
- Validação estrutural, mascaramento, detecção de instruções em arquivos e isolamento Qdrant por identidade/cozinha.
- buscar_documentos nos especialistas e em consultas demonstrativas reconhecidas; leitura também elegível ao fan-out.
- Embeddings/Qdrant opcionais; qdrant-client, pypdf e Pillow mantidos nos requisitos.
- FAQ com busca determinística e fallback literal se a geração falhar; a busca foi adaptada ao gate obrigatório.
- Mensagens legíveis de propostas, classificação de erros e logs com motivos JEV sem argumentos/conversa.
- Requisições nascem em DRAFT; itens só mudam em DRAFT; cancelamento admite DRAFT/UNDER_REVIEW/APPROVED.
- CORS DELETE para remoção documental, logs e métricas compatíveis com a nova rota FAQ.

## Conflitos e decisões

O Git encontrou 13 conflitos explícitos (conteúdo ou remoção/modificação). Também foram revisadas
mudanças que o merge aplicou automaticamente, pois ausência de conflito textual não garante compatibilidade.

| Área | Conflito | Resolução |
|---|---|---|
| Propostas/rotas/testes | A main removia o fluxo e só devolvia ação não executada | Preservados serviço, contratos, rotas, índices e testes do HITL atual |
| acoes.py | Ponto novo da main ainda dizia que não havia HITL | Adaptado como entrada para o serviço durável existente, sem duplicar snapshot/execução |
| JEV | Main dispensava leituras por padrão | Preservada exigência individual; JEV_ESCOPO=todas é obrigatório; escrita é recusado |
| Concorrência | Gate da main serializava todas as chamadas | Mantidos lotes de leitura independentes e locks curtos; documentos foram incluídos após revisão |
| FAQ | Main buscava fora do agente/gate | Busca determinística agora gera candidato, passa pelo JEV e só então permite redação/fallback |
| Falhas FAQ | Erro/solicitação de reparo poderia ser confundido com evidência | Nenhuma geração sem busca autorizada; falha/reparo interrompe; ausência de trechos não conta como resolução |
| Chat | Novo texto da main e saída estruturada da branch | Preservados contratos, dados exatos da revisão e montagem dos artefatos, com texto legível |
| Requisições | Regras antigas de UNDER_REVIEW versus DRAFT | Atualizados schema, mock, snapshot, FAQ e testes; sem adicionar operações |
| Documentação/testes | Main descrevia HITL ausente e testes esperavam ausência de persistência/JEV | Atualizados para política consolidada, preservando cenários e ampliando asserts de segurança |

## Configuração e dependências

- JEV_ESCOPO=todas; nenhuma dispensa probabilística de leituras live.
- QDRANT_URL/QDRANT_API_KEY habilitam documentos; coleção inventra_documentos, embeddings gemini-embedding-001, dimensão 768.
- ARQUIVOS_* preserva limites/guardrails da main. Sem Qdrant configurado, o restante continua disponível.
- Nenhum arquivo .env com credenciais foi versionado ou sobrescrito. As novas dependências vêm da main.

## Validação

- 394 testes aprovados, 6 ignorados; cobertura 91,56% (antes da integração: 91,42%).
- Ruff e mypy: sem erros; pip check: dependências consistentes.
- Casos integrados: estoque + documento concorrentes com barreira e JEV por chamada; números de documento
  não substituem estoque no artefato; FAQ sem JEV/reparo não busca nem gera; confirmação durável sobrevive
  à indisponibilidade anterior ao envio; DRAFT funciona com confirmação e revalidação.
- Qdrant de testes em memória, embedder local e adaptadores de modelo simulados. Seis testes externos
  dependem de serviços/chaves explícitos e não foram executados: não certifica integração em produção.
- Aviso esperado do Qdrant local: índices de payload de servidor não têm efeito completo no modo em memória.

## Arquivos alterados/adicionados

Lista completa do estado consolidado comparado à base original `001ff2b` (inclui o trabalho preservado
no checkpoint e as alterações incorporadas/adaptadas da main). Caminhos relativos à raiz do repositório.

| Arquivo | Situação |
|---|---|
| `CONSOLIDACAO_MAIN.md` | Adicionado |
| `CONTEXTO_IAI_PARA_IAS.md` | Alterado |
| `CONTEXTO_TECNICO_IA_ATUAL.md` | Adicionado |
| `README.md` | Alterado |
| `conftest.py` | Alterado |
| `iai/.env.example` | Alterado |
| `iai/app/core/arquivos/descricao_imagem.py` | Adicionado |
| `iai/app/core/arquivos/extracao.py` | Adicionado |
| `iai/app/core/arquivos/guardrail.py` | Adicionado |
| `iai/app/core/arquivos/indice.py` | Adicionado |
| `iai/app/core/arquivos/seguranca.py` | Adicionado |
| `iai/app/core/arquivos/servico.py` | Adicionado |
| `iai/app/core/assistant/artifacts.py` | Adicionado |
| `iai/app/core/assistant/graph.py` | Alterado |
| `iai/app/core/assistant/judge.py` | Alterado |
| `iai/app/core/assistant/prompts.py` | Alterado |
| `iai/app/core/assistant/respostas.py` | Adicionado |
| `iai/app/core/assistant/tool_gate.py` | Alterado |
| `iai/app/core/config.py` | Alterado |
| `iai/app/core/infra/metrics.py` | Alterado |
| `iai/app/core/infra/security.py` | Alterado |
| `iai/app/core/inventra/acoes.py` | Adicionado |
| `iai/app/core/inventra/approvals.py` | Alterado |
| `iai/app/core/inventra/business.py` | Alterado |
| `iai/app/core/inventra/domain.py` | Alterado |
| `iai/app/core/routes/approvals.py` | Alterado |
| `iai/app/core/routes/arquivos.py` | Adicionado |
| `iai/app/core/routes/chat.py` | Alterado |
| `iai/app/core/routes/queries.py` | Alterado |
| `iai/app/core/schemas.py` | Alterado |
| `iai/app/core/tools/documentos.py` | Adicionado |
| `iai/app/core/tools/faq.py` | Alterado |
| `iai/app/core/tools/operational.py` | Alterado |
| `iai/app/extras/demo.py` | Alterado |
| `iai/app/extras/mcp_server.py` | Alterado |
| `iai/app/main.py` | Alterado |
| `iai/data/faq_inventra.jsonl` | Alterado |
| `requirements.txt` | Alterado |
| `tests/test_api.py` | Alterado |
| `tests/test_approvals.py` | Alterado |
| `tests/test_arquivos.py` | Adicionado |
| `tests/test_artifacts.py` | Adicionado |
| `tests/test_audio.py` | Alterado |
| `tests/test_external_services.py` | Alterado |
| `tests/test_fanout.py` | Adicionado |
| `tests/test_faq.py` | Alterado |
| `tests/test_graph.py` | Alterado |
| `tests/test_jev.py` | Alterado |
| `tests/test_live_graph.py` | Alterado |
| `tests/test_operations.py` | Alterado |
| `tests/test_respostas.py` | Adicionado |
| `tests/test_tool_gate.py` | Alterado |
| `tests/test_tools_and_security.py` | Alterado |
| `tests_helpers.py` | Alterado |
