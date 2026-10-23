# IAI INVENTRA — contexto técnico e funcional para outras IAs

**Atualizado em:** 22/10/2026.  
**Base consultada:** código local, README, configurações e testes; HEAD `9448c08`, com alterações locais posteriores.  
**Alteração mais recente:** juiz JEV pré-execução; detalhes em [docs/JEV_GATE.md](docs/JEV_GATE.md).  
**Finalidade:** permitir que outra IA compreenda o projeto, encontre suas responsabilidades e preserve suas regras ao ajudar no desenvolvimento.

Este documento descreve o estado observado. Não substitui as solicitações do responsável pelo projeto nem os contratos oficiais do backend. Se o código mudar, as informações e os resultados abaixo devem ser revalidados.

## 1. O essencial para começar

A **IAI** é a camada de inteligência artificial do **INVENTRA**, sistema de gestão de estoque para cozinhas industriais. Este repositório contém uma API Python com FastAPI, agentes LangChain e orquestração LangGraph.

Ela atende três necessidades:

1. Explicar o INVENTRA a partir do FAQ real do projeto.
2. Consultar dados operacionais através de ferramentas tipadas e fontes autorizadas.
3. Preparar propostas de alteração, cuja execução depende de aprovação autenticada.

Os pontos que mais afetam qualquer trabalho neste projeto são:

- **Toda alteração de dados de negócio precisa de aprovação.** Criar uma proposta no Mongo não equivale a modificar o estoque.
- **Agentes live não executam tools sem autorização prévia JEV**, além das validações determinísticas.
- **O modelo não aprova nem executa operações de negócio.** As tools `propor_*` apenas registram propostas.
- **Consultas são diferentes de explicações documentais.** O FAQ não comprova o saldo atual de um produto.
- **Existem modos reais e simulados separados.** Uma resposta mockada deve continuar identificada como tal.
- **O modo demo não é uma IA com compreensão geral de linguagem natural.** É um intérprete determinístico limitado, separado do grafo live.
- **Identidade, cozinha e permissões vêm da autenticação**, não de campos escolhidos pelo modelo ou livremente enviados pelo usuário.
- **Não há exclusão física de registros de negócio pela IAI.** Utilizam-se desativação, cancelamento ou a operação lógica apropriada.
- **O PostgreSQL pertence à camada de negócio Spring.** Não há execução de SQL livre pelo modelo.
- **Código de integração existente não comprova integração externa funcionando.** O ambiente real não foi validado nesta sessão.

## 2. Estado comprovado e dependências

| Capacidade | Estado observado em 22/10/2026 |
|---|---|
| API FastAPI, documentação OpenAPI e autenticação de demonstração | Executadas localmente |
| FAQ e recuperação textual por pergunta | Implementados e cobertos por testes |
| Consultas, propostas, aprovação e execução demonstrativa | Fluxo HTTP exercitado com dados simulados |
| Servidor MCP por stdio | Inicialização, listagem de 20 tools de Supervisor e consulta mock exercitadas |
| Agentes e grafo live | Implementados e testados com modelos roteirizados; provedores reais não avaliados |
| JEV pré-tool | Implementado; gate e cliente HTTP cobertos por testes com respostas simuladas; API TypeSafe real não validada |
| API Spring e identidade JWT | Adaptadores presentes; serviço real e JWT de homologação não disponibilizados |
| MongoDB real | Caminho de persistência implementado; validação local usou mongomock |
| Transcrição de áudio Groq | Rota e implementação presentes; serviço real não validado |
| Página de aprovação no frontend | Não existe neste repositório |
| Delegação para executar com a identidade de outro solicitante | Dependência externa pendente |
| Remoção lógica real de item e edição real de contagem | Bloqueadas enquanto faltarem contratos externos adequados |

Nesta cópia, a preparação local criou `.venv` com Python 3.12.10 e `iai/.env` a partir do exemplo demonstrativo. Ambos ficam fora do Git. O comando global `python` da máquina apontava para Python 3.15; por isso os comandos locais usam explicitamente o executável da `.venv`.

Não havia chaves Gemini/Groq no ambiente nem serviços locais Spring/MongoDB disponíveis nos endereços padrão durante a verificação. A configuração local utilizada foi `mock/mock/mock/demo`.

## 3. Mapa do código

Os caminhos abaixo são relativos à raiz deste repositório.

```text
iai/
  app/
    main.py                  API, CORS, saúde e tarefa de expiração
    core/
      config.py              Variáveis, modos, caminhos e premissas de métricas
      schemas.py             Contratos HTTP, estado do grafo e saídas estruturadas
      assistant/
        graph.py             Grafo live e execução de uma rodada de chat
        prompts.py           Instruções dos agentes e guardrails
        llms.py              Instâncias dos modelos e fallback
        guardrail.py         Filtros, anonimização e avaliação de escopo
        judge.py             Cliente TypeSafe e política de decisão pré-tool
        tool_gate.py         Captura, gate JEV e permissão de uso único
        memory.py            Sessões, mensagens, resumos e histórico
      inventra/
        domain.py            Consultas, 16 operações e permissões para propor
        business.py          BusinessGateway, SpringGateway e MockGateway
        approvals.py         Propostas, decisão, execução e estados
      infra/
        security.py          Identidade, Bearer e contexto da requisição
        storage.py           MongoDB/mongomock e índices
        retrieval.py         Ranking textual usado no FAQ e nos resumos
        privacy.py           Remoção de segredos reconhecidos por padrões
        metrics.py           Coleta e agregação de métricas
      routes/                chat, queries, approvals e metrics
      tools/                 Tools de FAQ, histórico e operações
    extras/
      demo.py                Grafo determinístico sem LLM
      audio.py               Transcrição com Groq
      mcp_server.py          Exposição das tools por MCP stdio
  data/faq_inventra.jsonl     Base documental versionada
  .env.example               Configuração pública de demonstração
tests/                       Testes automatizados
conftest.py                  Isolamento do ambiente de testes
tests_helpers.py             Contextos e auxiliares de teste
```

O ponto de entrada continua sendo `iai.app.main:app`. Os módulos internos foram reorganizados para `core/` e `extras/`; referências antigas como `iai.app.graph` ou `iai.app.tools` não representam a estrutura atual.

## 4. Agentes, ferramentas e permissões

### Responsabilidades

| Componente | Responsabilidade e acesso |
|---|---|
| Roteador | Interpreta a intenção e escolhe um especialista; pode buscar histórico |
| Estoquista | Consultas e propostas relacionadas a lotes, movimentação, inventário e contagens |
| Comprador | Consultas e propostas relacionadas a fornecedores e requisições |
| Supervisor | Consultas e todas as propostas, incluindo manutenção de produtos |
| FAQ | Conhecimento documental através de `faq_retriever` |
| Juiz JEV | Decide se uma candidata a tool call pode executar; não gera a chamada nem julga a redação final |
| Orquestrador | Organiza a resposta dos especialistas operacionais; não executa alterações |

Roteamento por intenção não concede permissão ao usuário. A verificação do perfil para criar uma proposta acontece em `create_proposal`, usando `ROLE_OPERATIONS` de `domain.py`, inclusive quando a proposta vem diretamente do REST ou MCP.

### Tools principais

- `consultar_inventra`: recebe o contrato `Query` e retorna dados operacionais estruturados.
- `consultar_solicitacoes_iai`: consulta situação e resultado das propostas no escopo autorizado.
- `buscar_historico`: recupera resumos do mesmo usuário e cozinha.
- `faq_retriever`: recupera entradas documentais relacionadas à pergunta.
- `propor_<operacao>`: tool gerada a partir do schema tipado da operação, acrescido de `justification`.

Estoquista, Comprador e Supervisor recebem suas tools operacionais, histórico e FAQ no grafo. As listas concretas estão em `tools/operational.py` e `assistant/graph.py`. No MCP, os perfis expõem respectivamente **11, 10 e 20 tools**. Não existe tool para aprovar, recusar ou executar.

### Operações permitidas

| Perfil que pode propor | Valores de `Operation` |
|---|---|
| Estoquista e Supervisor | `criar_lote`, `registrar_entrada`, `registrar_saida`, `ajustar_estoque`, `registrar_inventario`, `registrar_contagem`, `alterar_contagem` |
| Comprador e Supervisor | `cadastrar_fornecedor`, `desativar_fornecedor`, `criar_requisicao`, `adicionar_item_requisicao`, `remover_item_requisicao`, `cancelar_requisicao` |
| Somente Supervisor | `criar_produto`, `editar_produto`, `desativar_produto` |

São **16 operações**, todas sujeitas à aprovação. Os campos obrigatórios e as validações estão em `PARAMETERS`, `Action` e nos modelos de `domain.py`; não devem ser inferidos a partir do nome da operação. Quantidades e valores usam `Decimal` com limites de precisão.

## 5. Fluxo de uma conversa

### Modo live

1. A rota autentica o usuário e verifica a correspondência do `user_id`.
2. O contexto seguro mantém a identidade, a cozinha e o JWT fora do estado enviado aos modelos.
3. Até 30 mensagens visíveis anteriores da sessão são reidratadas, mantendo o isolamento.
4. Os guardrails de entrada continuam tratando anonimização, abuso, injeção e escopo.
5. O roteador escolhe um especialista. Todos os cinco agentes têm middleware JEV.
6. Quando o modelo emite uma tool call, `after_model` registra a candidata como `PENDING_JEV` no estado.
7. `wrap_tool_call` verifica integridade, binding, perfil e schema. O cliente TypeSafe recebe somente contexto mínimo sanitizado e perguntas Noul/Choice.
8. A política Python autoriza uma execução, devolve correção ao agente, pede informação ao usuário ou encerra a etapa. O JEV não escreve JSON corrigido nem mensagens livres.
9. A resposta operacional segue para o orquestrador e para o guardrail de saída; FAQ segue diretamente para a saída. Uma parada do gate conserva a mensagem de esclarecimento/indisponibilidade.
10. A aplicação renderiza propostas a partir dos registros confirmados, identifica mocks e persiste mensagens e métricas.

O limiar padrão é **0,70** para critérios positivos e no máximo **0,30** para riscos. Há até **3 tentativas por sequência de correção**; nenhuma rejeitada executa, inclusive a terceira. Falha ou chave ausente é fail-closed. Aprovação JEV de `propor_*` apenas permite criar a proposta para aprovação humana.

```mermaid
flowchart TD
    A[Autenticação e guardrails] --> B[Roteador ou especialista]
    B --> C[Intenção de tool no estado: PENDING_JEV]
    C --> D[Validaç
    -> B
    D -->|corrigir, dentro do limite| B
    D -->|falta dado ou rejeita| F[Pergunta ou parada segura]
    B -->|resposta operacional| G[Orquestrador]
    B -->|resposta FAQ ou direta| H[Guardrail de saída]
    F --> H
    G --> H
    H --> I[Usuário e persistência]
```

O juiz generativo posterior foi removido. O orquestrador mantém uma checagem determinística simples para não introduzir números diferentes dos retornados pelo especialista. O gate não garante fidelidade de toda a redação final nem substitui autorização do backend. O desenho, a política e as limitações estão em [JEV_GATE.md](docs/JEV_GATE.md).

### Modo demo

`extras/demo.py` compila outro grafo, com um intérprete determinístico. Reconhece algumas consultas e frases de consumo em kg, inclusive múltiplos itens. Exemplo demonstrável:

> Fiz um almoço usando 10 kg de arroz e 40 kg de carne.

Se produtos e lotes puderem ser identificados sem ambiguidade, são criadas propostas independentes. O saldo só muda depois da aprovação. Se faltarem dados críticos, o fluxo pede confirmação.

Esse modo usa padrões e palavras específicas; não executa a mesma cadeia de modelos/juiz do live. Não se deve apresentar o demo como comprovação de compreensão geral de linguagem natural ou de todos os guardrails do live. As demais operações podem ser exercitadas por contratos estruturados sem depender do intérprete demo.

### Modelos configurados

| Uso | Configuração no código |
|---|---|
| Especialistas operacionais | Gemini `gemini-2.5-flash`, com fallback Groq |
| Roteador, FAQ, orquestrador e resumo | Groq `openai/gpt-oss-20b` |
| Juiz de tool calls | TypeSafe `jev-1.13.0`, configurável; modelo de decisão, não gerador |
| Moderação de abuso | Groq `openai/gpt-oss-safeguard-20b` |
| Transcrição | Groq `whisper-large-v3-turbo` |

Os nomes acima descrevem `llms.py`, não confirmam disponibilidade atual no provedor. O chat live exige `GEMINI_API_KEY` e `GROQ_API_KEY` configuradas; para executar tools também precisa de `TYPESAFE_API_KEY` e autorização JEV válida.

## 6. Consultas e origem dos dados

`BusinessGateway` separa o contrato de negócio de suas implementações:

- `SpringGateway`: HTTP com JWT para a API Spring; não acessa PostgreSQL diretamente.
- `MockGateway`: simulação explícita, com dados de exemplo segregados por cozinha no armazenamento da IAI.

O contrato `Query` aceita os recursos:

```text
produtos, estoque, lotes, validade, estoque_minimo,
fornecedores, fornecedores_recorrentes, fornecedores_produto,
parametros_produto, requisicoes, itens_requisicao,
inventarios, contagens, alertas, categorias, unidades, cozinha
```

Há filtros como `product_id`, `record_id`, `name`, `status`, `days`, `page`, `size`, `unread` e `mine`, sujeitos às validações e ao comportamento de cada recurso.

Detalhes que evitam interpretações incorretas:

- `estoque` sem `product_id` consulta o saldo geral da cozinha; com o ID, consulta o produto específico.
- `itens_requisicao` e `contagens` exigem o identificador do recurso pai em `record_id`.
- `fornecedores_produto` e `parametros_produto` exigem `product_id`.
- `mine` aplica-se a requisições e inventários e usa o usuário autenticado.
- Requisições usam os estados `UNDER_REVIEW`, `APPROVED`, `REJECTED` e `CANCELLED`.
- Fornecedores recorrentes são medidos pelo **número de lotes recebidos**, não pela frequência de pedidos de compra.
- `ToolResult` diferencia `ok`, `code`, `message`, `data`, `source`, `mocked`, `request_id` e `incomplete`.
- Um resultado vazio, uma página incompleta e uma falha de autorização são situações diferentes.

O adaptador contém rotas para produtos, fornecedores, lotes, estoque da cozinha, requisições, inventários, contagens, alertas e catálogos. Os caminhos exatos estão em `business.py`; sua existência no adaptador não comprova que a revisão implantada do Spring aceite os mesmos contratos.

## 7. Aprovação, execução e auditoria

### Regras centrais

- Qualquer **Supervisor da mesma cozinha** pode aprovar ou recusar.
- O próprio solicitante pode aprovar se for Supervisor autorizado daquela cozinha.
- A solicitação vale **7 dias**.
- A operação pertence ao **solicitante original**; o aprovador e o executor técnico são registrados separadamente.
- A proposta aprovada é imutável. Alteração nos dados exige nova proposta e nova aprovação.
- Não há transação de grupo para vários itens: cada proposta tem seu próprio resultado.

### Sequência implementada

1. Validar perfil, schema, referências e condições do negócio.
2. Capturar um snapshot dos registros relacionados.
3. Persistir proposta com ID, chave idempotente, hash, solicitante, cozinha, justificativa, datas, estado e auditoria.
4. Disponibilizá-la na inbox do Supervisor.
5. Receber decisão autenticada com o `proposal_hash` esperado.
6. Registrar a decisão usando atualização condicional para evitar decisões concorrentes.
7. Revalidar a proposta e o snapshot antes de executar.
8. Adquirir o estado `EM_EXECUCAO` por atualização condicional.
9. Chamar o gateway e registrar o resultado efetivamente retornado.

A notificação atual é a **inbox consultável**, com canal `supervisor_inbox` registrado na proposta. Não há serviço implementado de push, e-mail ou WhatsApp para esse fluxo. O frontend deve consultar as rotas de aprovação; sua página não está nesta cópia.

### Estados persistidos

| Estado | Significado |
|---|---|
| `AGUARDANDO_APROVACAO` | Proposta criada, sem decisão válida |
| `APROVADA_PENDENTE_INTEGRACAO` | Aprovação registrada; execução ainda depende de contexto, integração ou validação |
| `RECUSADA` | Supervisor recusou |
| `EM_EXECUCAO` | Uma execução adquiriu a solicitação |
| `CONCLUIDA` | Gateway confirmou sucesso; verificar `result.mocked` para distinguir simulação |
| `FALHOU` | Validação ou execução retornou falha confirmada |
| `EXPIRADA` | Prazo encerrado sem execução válida |
| `RESULTADO_INCERTO` | Pode ter ocorrido efeito externo; exige reconciliação e não deve ser repetida automaticamente |

A expiração é aplicada nas consultas/decisões e por tarefa periódica de aproximadamente 30 segundos enquanto a API está ativa. Execuções paradas por 10 minutos são marcadas como incertas. O prazo também é verificado na aquisição da execução. Não há TTL apagando o histórico de propostas.

### Idempotência e limites reais

REST usa `idempotency_key`; as tools derivam uma chave da rodada e da ação. No chat, reutilizar o mesmo `request_id` ajuda a deduplicar a mesma proposta; não equivale a cache integral da resposta nem a garantia de que um modelo sempre repetirá a mesma intenção.

Clique duplicado e nova decisão equivalente não devem executar novamente uma proposta já processada. A proteção local usa IDs duráveis, índices e atualizações condicionais. A garantia entre IAI e Spring ainda depende de idempotência/reconciliação também no backend. Comparar snapshots na IAI não substitui controle concorrente transacional no serviço de negócio.

Em modo Spring:

- Uma aprovação de outro usuário não autoriza reutilizar o JWT do Supervisor para agir como o solicitante.
- Sem delegação segura disponível, a proposta permanece pendente. O solicitante pode apresentar JWT fresco em `POST /approvals/{id}/execute`.
- `alterar_contagem` e `remover_item_requisicao` permanecem bloqueadas para execução real enquanto não houver contrato adequado de edição/remoção lógica.
- Uma proposta criada em modo mock não pode ser executada como real apenas trocando a configuração.
- Timeout ou resposta inesperada após uma escrita não podem ser transformados em sucesso ou justificar repetição automática.

A situação atual deve ser consultada em `GET /approvals/{id}` ou `consultar_solicitacoes_iai`, considerando o estado persistido e o resultado, não apenas o texto produzido pelo modelo.

## 8. Identidade, segurança e memória

### Autenticação

`security.py` define `Identity`, `RequestContext` e um `ContextVar` por requisição.

- No modo `spring`, o Bearer é validado online em `GET /api/users/me`; usuário ativo, cozinha e perfil são conferidos.
- O backend de negócio continua responsável por suas permissões e regras reais.
- No modo `mock`, só são aceitas identidades fixas de demonstração: `demo-estoquista`, `demo-comprador`, `demo-supervisor` e `demo-outra-cozinha`.
- Os três primeiros pertencem à cozinha 1; o último é Supervisor da cozinha 2.
- O `user_id` no corpo do chat existe por compatibilidade, mas deve corresponder à identidade autenticada.
- O JWT fica no contexto HTTP e não deve ser incluído em prompts, mensagens, parâmetros de tools ou documentos persistidos.
- A configuração exige HTTPS para a URL Spring fora de localhost. API real com identidade mock é rejeitada.

Os identificadores demo são exemplos públicos, não autenticação de produção. Guardrails, juiz e prompts ajudam a controlar o conteúdo; a autorização depende das verificações determinísticas e dos serviços responsáveis.

### Proteção de conteúdo

Existem filtros de prompt injection, classificação de abuso e escopo, anonimização de padrões de e-mail/CPF/RG/telefone e remoção de segredos reconhecidos por expressão regular. PII pode ter um mapa temporário de restauração; segredos reconhecidos são removidos sem esse mapa. Esses mecanismos não detectam toda forma possível de dado sensível.

### Histórico e persistência

- Banco da IAI: `dbIAI`.
- Coleções principais: `sessions`, `approvals` e `metrics`.
- A simulação de negócio usa estruturas adicionais, como `business_mock` e `execution_mock`.
- `STORAGE_MODE=mock` usa mongomock em memória. **Reiniciar o processo perde esses dados.** Processos separados, como API e MCP, também não compartilham esse Mongo simulado.
- `STORAGE_MODE=mongo` usa PyMongo e índices criados na inicialização; a persistência real precisa ser validada no ambiente de destino.
- Sessões e mensagens são isoladas por usuário e cozinha; conhecer um `session_id` ou `doc_id` não concede acesso a outra identidade.
- O histórico recuperável reúne resumos de sessões encerradas. `busca` faz ranking textual sobre até 200 resumos recentes.
- Mensagens internas `judge` ficam fora do histórico devolvido ao usuário e do resumo.
- O grafo live não depende de `MemorySaver` para manter a conversa; reidrata o histórico do armazenamento a cada rodada.

O armazenamento ter índice para uma coleção não comprova a existência de um serviço que a utilize. Por exemplo, a inbox atual consulta propostas; não há um worker externo de notificações demonstrado pelo índice `notifications`.

## 9. FAQ e conhecimento do INVENTRA

O arquivo oficial desta cópia é [iai/data/faq_inventra.jsonl](iai/data/faq_inventra.jsonl), com **24 entradas** na revisão consultada. Cada linha é um objeto JSON com pergunta `p` e resposta `r`.

`faq_retriever(question)` normaliza o texto, considera stopwords e alguns sinônimos e calcula um ranking por termos. Retorna até **4 entradas** e referências de linha. Quando não encontra conteúdo relevante, ou a base está inválida, informa a limitação.

Isso é **recuperação textual**, sem embeddings e sem banco vetorial. Não deve ser descrito como busca semântica implementada. O mesmo mecanismo de ranking é reutilizado para resumos de histórico.

O FAQ descreve funcionalidades; dados atuais de estoque vêm do gateway. Não se deve inventar telas, botões ou fluxos de frontend com base apenas nos nomes das operações da IAI.

## 10. Contratos HTTP, MCP e áudio

### Rotas principais

| Método e rota | Finalidade |
|---|---|
| `GET /health` | Saúde do armazenamento e modos configurados |
| `GET /docs`, `GET /openapi.json` | Documentação e contratos FastAPI |
| `POST /chat` | Rodada de conversa autenticada |
| `POST /chat/audio` | Transcrição e processamento pelo fluxo do chat |
| `POST /chat/encerrar` | Encerramento e resumo de sessão |
| `GET /chat/historico/{user_id}` | Resumos do próprio usuário |
| `GET /chat/mensagens/{doc_id}` | Mensagens autorizadas da sessão |
| `POST /queries` | Consulta operacional tipada, independente de LLM |
| `GET /queries/capabilities` | Schemas, operações, perfis e listas de tools |
| `POST /approvals`, `GET /approvals` | Criar e listar propostas no escopo autorizado |
| `GET /approvals/inbox` | Propostas aguardando decisão do Supervisor |
| `GET /approvals/{id}` | Estado, auditoria e resultado da proposta |
| `POST /approvals/{id}/decision` | Aprovar ou recusar com hash da proposta |
| `POST /approvals/{id}/execute` | Tentar executar aprovação válida com contexto apropriado |
| `GET /metrics` | Métricas limitadas ao usuário/cozinha conforme perfil |

`/health` não comprova disponibilidade de Gemini, Groq ou Spring: seu `status` depende do armazenamento. As respostas também identificam os modos nos cabeçalhos `X-IAI-*`.

Exemplo de chat **para o modo demonstrativo**:

```http
POST /chat
Authorization: Bearer demo-estoquista
Content-Type: application/json
```

```json
{
  "session_id": "demo-cozinha-1",
  "user_id": "demo-estoquista",
  "request_id": "mensagem-001",
  "pergunta": "Usei 10 kg de arroz."
}
```

A resposta mantém o contrato `{"resposta": "..."}`. Campos adicionais, contratos de decisão e schemas completos devem ser consultados no OpenAPI e em `schemas.py`/`domain.py`.

### MCP

O servidor `extras/mcp_server.py` é iniciado pelo cliente MCP via **stdio**, sem porta própria e sem exigir que a API HTTP esteja rodando. Usa `IAI_MCP_TOKEN` para identificar o usuário, expõe tools por perfil e só permite propor alterações. A decisão continua dependendo da API de aprovação.

Não há A2A implementado. Cada chamada MCP recebe uma nova rodada; repetir uma tool de proposta pode criar outra proposta. Com Mongo real configurado de forma comum, API e MCP podem consultar as mesmas propostas; com mongomock, os processos têm memórias independentes.

O stdout pertence ao protocolo. Logs devem ir para stderr. A inicialização por caminho absoluto foi corrigida para localizar a raiz do projeto mesmo quando o cliente usa outro diretório de trabalho.

### Áudio

`POST /chat/audio` recebe multipart com `session_id`, `user_id`, `audio` e `request_id` opcional. O arquivo tem limite de 10 MB e validação de extensão. A Groq transcreve a fala, e o texto segue o mesmo fluxo autenticado do chat. A resposta contém `transcricao` e `resposta`.

**Mesmo com `IAI_LLM_MODE=demo`, a transcrição exige `GROQ_API_KEY` e o serviço externo.** Não existe transcritor offline mockado nessa rota. O código não grava o arquivo de áudio; o texto pode entrar no histórico após o tratamento do fluxo.

## 11. Métricas e observabilidade

`infra/metrics.py` coleta tempos de nós, chamadas de modelos, tokens informados pelos provedores e erros. A persistência técnica não inclui o texto dos prompts. A rota `/metrics` oferece períodos `semana` e `mes`, projeções por quantidade de usuários e indicadores de custo, resolução e ROI.

As principais limitações para interpretar os números são:

- Custos são **estimativas**, calculadas com a tabela estática `PRECOS_MODELOS_USD_POR_MILHAO` em `config.py`, não a fatura real.
- Modelos sem preço na tabela usam custo zero no cálculo atual; isso não comprova custo real zero.
- ROI depende de premissas configuradas e não representa retorno financeiro observado.
- Sem tráfego, projeções podem estar zeradas; isso não demonstra gratuidade do serviço.
- O custo do resumo de sessão e da infraestrutura não está incluído nessa conta.
- A métrica atual de resolução usa `jev:concluido`: um especialista terminou com tool autorizada executada, sem parada do gate ou falha de execução. É um indicador do fluxo, não uma verificação da verdade de cada frase. O marcador antigo `juiz:aprovado` é lido apenas por compatibilidade histórica.
- Decisões e eventos técnicos JEV são registrados em métricas, sem argumentos ou conversa. O uso TypeSafe é guardado, mas seu custo não foi incluído na estimativa financeira LLM.
- O arquivo `.env.example` ainda contém um comentário sobre `IAI_MODEL_PRICES_JSON`, mas essa variável **não é lida pelo `config.py` atual**. Não a tratar como configuração funcional.

Há suporte ao tracing opcional LangSmith via configuração LangChain. Isso é separado da coleção técnica de métricas: não se deve assumir que traces externos têm a mesma política de conteúdo. Antes de habilitá-los em ambiente real, é necessário revisar dados enviados e acesso aos traces.

## 12. Execução e validação

O roteiro completo está no [README](README.md). Para uma instalação nova no Windows:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
if (-not (Test-Path iai/.env)) { Copy-Item iai/.env.example iai/.env }
.\.venv\Scripts\python.exe -m uvicorn iai.app.main:app --host 127.0.0.1 --port 8000
```

O exemplo habilita demonstração explicitamente. Sem essa configuração, os padrões do código são:

| Variável | Padrão | Alternativas relevantes |
|---|---|---|
| `IAI_AUTH_MODE` | `spring` | `mock` para identidade demonstrativa |
| `IAI_BUSINESS_MODE` | `spring` | `mock` para dados de exemplo |
| `IAI_STORAGE_MODE` | `mongo` | `mock` para memória simulada |
| `IAI_LLM_MODE` | `live` | `demo` para intérprete limitado |
| `JEV_MODEL` | `jev-1.13.0` | Versão estável fixada; alterar exige recalibrar |
| `JEV_APPROVAL_THRESHOLD` | `0.70` | Limiar dos critérios positivos; riscos usam o complemento |
| `JEV_TIMEOUT` | `10` | Timeout HTTP em segundos |
| `INVENTRA_API_URL` | `http://localhost:8080` | URL do Spring autorizado |
| `MONGO_CONNECTION` | `mongodb://localhost:27017` | MongoDB disponibilizado para a IAI |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Lista explícita das origens permitidas |

Para uso real também são necessárias chaves Gemini/Groq/TypeSafe, JWT válido, serviços disponíveis e validação dos contratos implantados. Configuração por ambiente tem precedência sobre valores carregados do arquivo `.env`.

### Evidência de testes em 22/10/2026

- Instalação completa de requisitos e `pip check` sem dependências quebradas.
- Baseline anterior ao JEV nesta tarefa: **171 testes aprovados e 4 ignorados**.
- Após a implementação JEV: **204 testes aprovados, 5 ignorados e cobertura de 90,34%**. O cliente TypeSafe é validado com HTTP simulado e o gate com agentes/modelos roteirizados; a integração externa real não foi executada.
- HTTP: saúde, docs, OpenAPI, consulta, chat, aprovação, repetição da decisão e métricas exercitados com mocks.
- Requisição sem autenticação rejeitada; CORS local aceitou as duas origens previstas e rejeitou origem não autorizada.
- MCP: handshake stdio, listagem de tools e consulta mock por processo externo exercitados.
- Ruff passou nos arquivos alterados na correção; isso não equivale a revisão estática completa do repositório.

Os testes ignorados dependem de serviços reais; o JEV também possui teste externo opt-in. Os testes comuns usam dados/identidades/Mongo simulados; os testes do grafo usam modelos roteirizados e decisões JEV simuladas. Eles não comprovam qualidade linguística nem integração em produção.

Comandos para repetir a suíte:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-dev.txt
.\.venv\Scripts\python.exe -m pytest --cov=iai --cov-report=term --cov-fail-under=70 -q
```

Testes externos são opt-in: `RUN_LLM_TESTS`, `RUN_MONGO_TESTS` com `MONGO_TEST_CONNECTION`, `RUN_SPRING_TESTS` com `SPRING_TEST_TOKEN`, e `RUN_JEV_TESTS` com `TYPESAFE_API_KEY`. Conferir as condições nos testes antes de executá-los. O teste Mongo usa uma base temporária exclusiva, não o banco de negócio.

O Dockerfile existe, mas seu build não foi validado. Nesta cópia não há `.dockerignore`; como o Dockerfile usa `COPY . .`, arquivos locais como `.env` e `.venv` precisam de exclusões adequadas antes de produzir uma imagem para distribuição. O `.gitignore` não substitui essas exclusões.

## 13. Orientação para a próxima IA que trabalhar aqui

1. Começar pelo README, por este documento e pelo código da funcionalidade afetada; verificar o estado do Git e eventuais instruções locais vigentes.
2. Tratar `domain.py`, os contratos Pydantic, as rotas e o adaptador de negócio como referências verificáveis; não inventar endpoints externos, campos ou estados.
3. Preservar a separação entre leitura, preparação de proposta, decisão e execução. Aprovação nunca deve ser reduzida a uma frase no chat.
4. Ao adicionar uma operação, conferir schema, perfil autorizado, tool, agente, prompt, gate JEV, aprovação humana, gateway e testes. Tools compartilhadas devem adotar `protect_tool` para que não sejam um bypass durante turnos live.
5. Não concluir que uma ferramenta existe apenas porque aparece num prompt ou numa dependência. Conferir sua conexão efetiva ao agente.
6. Usar `ToolResult`, estado persistido e retorno do serviço para responder sobre sucesso. Distinguir dados vazios, resultado parcial, falta de permissão e indisponibilidade.
7. Não introduzir SQL livre, confiança em `user_id` arbitrário, armazenamento de JWT ou execução direta pelo modelo.
8. Não contornar dependências do Spring utilizando JWT do aprovador como se fosse do solicitante, exclusão física ou endpoints presumidos.
9. Manter mocks identificados, sem apresentar dados demonstrativos como dados atuais do INVENTRA.
10. Preservar mudanças locais do responsável e o escopo deste repositório. Alterar Spring ou frontend externo depende de autorização específica.
11. Repetir os testes pertinentes quando houver alteração funcional; registrar o que foi simulado e o que foi realmente integrado.
12. Atualizar este documento quando arquitetura, contratos, permissões, modos ou evidências de funcionamento mudarem.

### Leitura dirigida por tarefa

| Necessidade | Arquivos por onde começar |
|---|---|
| Entender ou corrigir o chat | `core/routes/chat.py`, `core/assistant/graph.py`, `core/assistant/prompts.py` |
| Adicionar consulta ou adaptar Spring | `core/inventra/domain.py`, `core/inventra/business.py`, `core/tools/operational.py` |
| Alterar o gate JEV | `core/assistant/tool_gate.py`, `core/assistant/judge.py`, `tests/test_tool_gate.py`, `tests/test_jev.py` |
| Alterar aprovação ou idempotência | `core/inventra/approvals.py`, `core/routes/approvals.py`, `core/infra/storage.py` |
| Tratar identidade e isolamento | `core/infra/security.py`, `core/assistant/memory.py`, rotas autenticadas |
| Revisar conhecimento documental | `iai/data/faq_inventra.jsonl`, `core/tools/faq.py`, `core/infra/retrieval.py` |
| Integrar outra IA via MCP | `extras/mcp_server.py`, `tests/test_mcp_server.py`, seção MCP do README |
| Trabalhar com áudio | `extras/audio.py`, `core/routes/chat.py`, `tests/test_audio.py` |
| Entender custos e observabilidade | `core/infra/metrics.py`, `core/routes/metrics.py`, `core/config.py` |

Na tabela acima, os caminhos `core/` e `extras/` partem de `iai/app/`. As evidências de testes representam a revisão local verificada; este texto não certifica disponibilidade futura dos serviços nem implantação em produção.
