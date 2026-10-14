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

Para MongoDB local real e persistente, com identidade/dados ainda simulados:
`docker compose up --build`. O volume `iai-mongo` preserva solicitações e histórico.
Essa alternativa exige Docker; o build não foi validado neste ambiente.

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

`POST /queries` oferece as mesmas consultas tipadas sem depender de LLM.
Exemplo: `{"resource":"validade","days":7}`.
`GET /queries/capabilities` informa schemas, operações e limitações.

## Aprovação

1. Proposta criada por ferramenta do agente ou `POST /approvals`.
2. Supervisor vê `GET /approvals/inbox`, restrito à sua cozinha.
3. Envia `POST /approvals/{id}/decision` com decisão e hash da proposta.
4. Execução única quando disponível. Resultado mock continua explicitamente identificado.
5. Solicitante acompanha `GET /approvals/{id}` ou listagem de suas solicitações.

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
| CORS_ORIGINS | origens exatas do frontend, separadas por vírgula |
| IAI_MODEL_PRICES_JSON | tabela opcional por modelo, input/output em USD por milhão |

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

Veja [inventário atualizado](docs/INVENTARIO_TECNICO_FUNCIONAL_IAI.md) e
[contrato do frontend e autenticação](docs/INTEGRACAO_FRONTEND.md).
