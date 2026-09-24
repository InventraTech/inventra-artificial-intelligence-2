# Tools reais do Inventra

## Banco inspecionado

Em 23/09/2026, `defaultdb` tinha tabelas em português, sem views ou dados de negócio.
A estrutura inglesa descrita no pedido está em `inventra_db_test`, no mesmo servidor.
Configure `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER`, `DB_PASSWORD` e `DB_SSLMODE`.
O código usa esses campos, não `DATABASE_URL`, evitando precedência ambígua.
Carrega primeiro variáveis exportadas, depois `iai/.env`, depois `.env` da raiz.
Nenhuma migration, tabela, view ou trigger é criada/alterada pela aplicação.

## Fluxo e responsabilidades

FastAPI → identidade do servidor → usuário/perfil/cozinha no PostgreSQL →
guardrails → roteador Groq → especialista Gemini com fallback Groq →
tool tipada → service → repository → PostgreSQL → apresentação determinística.
LangGraph mantém `MemorySaver`, com chave separada por usuário e sessão.
MongoDB continua armazenando conversas e também o estado de confirmação da sessão.
O fallback envolve somente a inferência do modelo e não repete uma execução de tool.

`domain.py` contém schemas; `database.py`, conexões/transações; `repository.py`,
SQL parametrizado; `services.py`, regras; `security.py`, identidade e autorização;
`confirmation.py`, propostas e confirmação; `tools/inventory.py`, interface LangChain.
O LLM não recebe ferramenta de SQL nem ferramenta de confirmação.

Os perfis observados em `tb_profile.access_type` são famílias `ESTOQUISTA_*`,
`COMPRADOR_*` e `SUPERVISOR_*`. O código usa o prefixo exato, com separador `_`.
Perfis desconhecidos, usuários/cozinhas inativos e usuários sem cozinha são recusados.
Estoque/catálogo são leituras compartilhadas. Requisições: Comprador e Supervisor.
Indicadores analíticos: Supervisor. Baixa: Estoquista. Criar compra: Comprador.
O Supervisor não herda permissões de escrita de outros cargos.
Produtos/fornecedores são cadastros globais no schema; dados operacionais sempre
recebem a cozinha do usuário, sem aceitar cozinha/cargo nos argumentos do LLM.

## Integração de identidade pendente — único mock permitido

O middleware confiável do backend deverá atribuir `request.state.authenticated_user_id`
antes da dependência `identity_from_backend`. Não há login paralelo, leitura de senha,
nem aceitação de cargo ou identidade por texto do usuário ou header não verificado.
O campo legado `user_id` de `/chat`, se enviado, deve coincidir com essa identidade.

Para desenvolvimento, configure `IDENTITY_MODE=mock` e `MOCK_USER_ID` com um UUID real
de `tb_user`. Apenas a origem desse UUID é mock; perfil, vínculo, status e permissão
são consultados no PostgreSQL. O modo mock é bloqueado com `APP_ENV=production`.
Não há usuário privilegiado padrão ou UUID escolhido automaticamente.

## Consultas

`consultar_dados` oferece recursos enumerados, filtros tipados, limite máximo 100,
ordem estável e sinalização de resultados adicionais. Nomes não são únicos; consultas
retornam os IDs candidatos. Valores são parametrizados, inclusive filtros por nome.
Views de lote, posição, vencimentos, urgência, catálogo, requisições, alertas,
movimentações, inventário, valor e indicadores são reutilizadas.
`pre_lista` significa somente lotes com status `ACTIVE`, conforme decisão do usuário.

`vw_category_stock_balance` e `vw_category_monthly_requisition_trend` agregam todas
as cozinhas e não expõem `id_kitchen`. Não podem servir usuários de uma única cozinha:
nesses dois recursos, consultas parametrizadas com isolamento substituem a visão global.
O saldo por categoria usa a view de posição; a tendência usa requisições reais.
`vw_product_stock_position` só inclui produtos com parâmetros por cozinha; para todos
os lotes, inclusive produtos sem parametrização, use `lotes`.
Somatórios de views entre unidades distintas devem ser interpretados conforme a view;
não equivalem automaticamente a kg ou unidades físicas homogêneas.

Datas relativas usam `CURRENT_DATE` da conexão, com timezone do banco (GMT na inspeção).
`esta_semana` vai de segunda a domingo. `proximos_dias` inclui hoje e exige `days`.
Classificações de vencimento vêm da view: vencido, crítico até 3 dias, atenção até 7.
O indicador de desperdício é explicitamente um **proxy**, não descarte medido.

## Preparação e confirmação

`preparar_baixa`/`preparar_requisicao` validam e salvam uma proposta na sessão Mongo.
A resposta exibe um resumo completo e um `confirmation_id`, válido por dez minutos.
Nada é escrito no PostgreSQL nessa etapa. Uma nova proposta substitui a pendente.

Confirme por `POST /chat/confirmar`:

```json
{"session_id":"sessao-1","confirmation_id":"token recebido no resumo","approved":true}
```

Use `approved:false` para cancelar. No chat, também são aceitos os comandos exatos
`confirmar TOKEN` e `cancelar TOKEN`. Um simples “sim” ou instrução ao LLM não executa.
O token é vinculado ao usuário e à sessão, consumido por compare-and-set no Mongo.
Repetir uma confirmação concluída retorna o resultado anterior, sem nova escrita.
Não é possível enviar quantidade ou produto diferente no endpoint de confirmação.

Na execução, perfil, cozinha, produto, unidade, fornecedor, status, validade e saldo
são revalidados. Lotes são bloqueados com `FOR UPDATE`; dados alterados desde o resumo
exigem nova proposta. Não há conversão automática de unidades nem seleção arbitrária
entre lotes. Quantidades usam Decimal e a precisão real `numeric(12,3)`.
Consumo de lote vencido é recusado. Requisições são `PURCHASE`, origem `SYSTEM`,
com status padrão `UNDER_REVIEW`; cabeçalho e itens são gravados na mesma transação.
Campos opcionais ausentes ficam nulos; não se inventa preço ou fornecedor.

Os triggers existentes registram auditoria em `tb_log_stock_batch` e
`tb_log_requisition`. O trigger de lote marca saldo zero como `WRITTEN_OFF`.
A auditoria existente registra a conta SQL em `db_user`, não o usuário do chat;
não se afirma uma autoria que o banco não registra nem se cria auditoria duplicada.

Não há transação distribuída entre Mongo e PostgreSQL. Se houver queda durante o
commit ou antes de salvar o resultado, a proposta permanece em execução/indeterminada
e não é repetida automaticamente. É necessário reconciliar com os dados/histórico.
O sistema não promete “exactly once” para novas propostas distintas após uma falha.

## Respostas, FAQ e validação

Resultados factuais e resumos são formatados pelo código a partir das tools, sem
aceitar texto livre do modelo como prova de sucesso. O FAQ usa o JSONL existente:
o modelo escolhe um índice e o código apresenta o texto oficial correspondente.
Os guardrails de entrada/escopo/PII continuam ativos. A memória não substitui uma
consulta nova quando a pergunta requer o estado atual do banco.
O resumo de sessão é extrativo: reproduz a última resposta registrada, identificando
truncamento quando necessário. Não transforma alegações do usuário em ações concluídas.
O Docker instala dependências acessíveis ao usuário sem privilégios e exclui arquivos
de ambiente do contexto da imagem; forneça as variáveis no ambiente de execução.

Instale `requirements.txt` e `requirements-dev.txt`; execute `pytest --cov=iai/app
--cov-report=term-missing`, `ruff check iai tests conftest.py` e `mypy iai`.
Os testes padrão não carregam o `.env` real nem chamam LLMs/ bancos de produção.
Testes de integração PostgreSQL devem usar somente leitura no banco existente;
escritas são cobertas por fixtures transacionais isoladas, sem modificar dados reais.
