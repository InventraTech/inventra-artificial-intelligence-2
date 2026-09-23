# Inteligência Artificial do Inventra

IA multiagente para o app **Inventra**, exposta como serviço HTTP (FastAPI), seguindo a arquitetura:

```
User
  -> Guardrail Insulto (termos proibidos / prompt injection / anonimiza PII / modelo de moderação)
       -> [bloqueado] Parar resposta
       -> [ok] -> Guardrail Escopo (pergunta pertence ao Inventra?)
                    -> [bloqueado] Parar resposta
                    -> [ok] -> Roteador -> Agente do cargo:
                                             Estoquista
                                             Comprador
                                             Supervisor
                                             FAQ           -> Orquestrador -> Guardrail Saída -> User
```

## Estrutura de pastas

```
inventra-ai-2/
├── iai/
│   ├── .env                    # variáveis de ambiente locais (não versionado)
│   ├── data/
│   │   └── faq_inventra.jsonl  # base de conhecimento do agente FAQ (pares pergunta/resposta)
│   └── app/
│       ├── main.py             # app FastAPI: monta rotas e expõe GET /health
│       ├── config.py           # lê o .env, expõe GEMINI_API_KEY/GROQ_API_KEY/MONGO_CONNECTION (SecretStr) e FAQ_PATH
│       ├── llms.py             # instancia os modelos (Gemini via llm_especialista, Groq via llm_rapido e llm_guardrail)
│       ├── schemas.py          # Estado do grafo (Estado), ChatRequest/ChatResponse, ResultadoGuardrail, schemas de sessão/histórico
│       ├── guardrail.py        # guardrail_insulto, guardrail_escopo, guardrail_saida, anonimizar_entrada (PII)
│       ├── prompts.py          # system prompts de cada agente/etapa
│       ├── graph.py            # monta o grafo (LangGraph) e expõe executar_fluxo_assessor
│       ├── memory.py           # persiste chats no Mongo: salvar_mensagem, encerrar_sessao (gera resumo via LLM), recuperar_historico/recuperar_mensagem
│       ├── tools/
│       │   ├── faq.py          # tool faq_retriever: lê faq_inventra.jsonl via JSONLoader
│       │   └── memoria.py      # tool buscar_historico: consulta resumos de sessões anteriores do mesmo user_id
│       └── routes/
│           └── chat.py         # POST /chat, POST /chat/encerrar, GET /chat/historico/{user_id}, GET /chat/mensagens/{doc_id}
├── tests/
│   ├── test_guardrail.py
│   ├── test_graph.py
│   └── test_faq.py
├── conftest.py                 # env vars dummy para rodar os testes sem credenciais reais
├── pytest.ini                  # pythonpath=. (necessário pois não há __init__.py em iai/)
├── requirements.txt
├── requirements-dev.txt        # ruff, mypy, pytest, pytest-cov, pip-audit
├── mypy.ini
└── Dockerfile
```

Cada cargo é atendido por um agente próprio (persona e tools isoladas), montado com
`create_agent` (LangChain); o Roteador decide qual agente chamar a partir da última
mensagem do usuário, e o Orquestrador formata a resposta final antes do guardrail de saída.

## Como rodar

1. Crie o arquivo `iai/.env` com as chaves necessárias:
   ```
   GEMINI_API_KEY=...
   GROQ_API_KEY=...
   MONGO_CONNECTION=mongodb+srv://usuario:senha@cluster.mongodb.net/
   ```
   (repare que o `.env` fica dentro de `iai/`, não na raiz do projeto — é onde `config.py` procura.)
2. Instale as dependências:
   ```
   pip install -r requirements.txt
   ```
3. Suba o servidor:
   ```
   python -m uvicorn iai.app.main:app --reload
   ```
4. Com o servidor no ar:
   - `GET http://127.0.0.1:8000/health` → `{"status": "ok"}`
   - `POST http://127.0.0.1:8000/chat` com corpo `{"session_id": "teste", "user_id": "user1", "pergunta": "quantos tomates temos em estoque?"}` → resposta do fluxo de agentes (a mensagem e a resposta já ficam salvas no Mongo)
   - `POST http://127.0.0.1:8000/chat/encerrar` com `{"session_id": "teste"}` → encerra a sessão e gera um resumo da conversa via LLM
   - `GET http://127.0.0.1:8000/chat/historico/{user_id}` → lista as sessões já encerradas desse usuário (resumo + data)
   - `GET http://127.0.0.1:8000/chat/mensagens/{doc_id}` → mensagens completas de uma sessão (o `doc_id` vem do histórico acima)

## Docker

```bash
docker build -t inventra-ai .
docker run -p 8000:8000 --env-file iai/.env inventra-ai
```

## Testes

```bash
pip install -r requirements-dev.txt
pytest --cov=. --cov-report=term-missing --cov-fail-under=70
```

Os testes não precisam de `GEMINI_API_KEY`/`GROQ_API_KEY` reais: o `conftest.py` na raiz
preenche valores dummy quando essas variáveis não existem no ambiente, só o suficiente
para os módulos importarem sem erro. A maioria dos testes é assim — determinística, sem
chamar a LLM de verdade.

`tests/test_faq.py` tem duas exceções que rodam o fluxo completo (roteador + agente FAQ)
contra o Gemini/Groq de verdade, para validar que o agente escolhe a resposta certa da
base de conhecimento mesmo quando a pergunta não é idêntica ao FAQ. Elas ficam `skipped`
por padrão (inclusive no CI, que não tem essas chaves como secret) e só rodam com
`RUN_LLM_TESTS=1` e as chaves reais exportadas no ambiente antes do pytest iniciar:

```bash
set -a && source iai/.env && set +a && RUN_LLM_TESTS=1 pytest tests/test_faq.py
```

## Notas de design

- A entrada passa por **dois guardrails em sequência**, cada um como node próprio do grafo:
  1. **`guardrail_insulto`**: primeiro filtra tentativas clássicas de jailbreak/prompt
     injection (PT e EN) por regex sobre o texto normalizado (sem acento, minúsculo —
     `normalizar_texto`), bloqueio imediato e sem custo de LLM. Se passar, anonimiza PII
     antes de mandar pro modelo de moderação `openai/gpt-oss-safeguard-20b`
     (`llm_guardrail`), que identifica insultos/abuso.
  2. **`guardrail_escopo`**: só roda se o primeiro aprovar; usa `llm_rapido` para classificar
     se a mensagem (já anonimizada) pertence ao escopo do Inventra.
  Bloqueio em qualquer um dos dois guardrails encerra o fluxo direto pro usuário, sem passar
  pelo roteador.
- A anonimização de PII (`anonimizar_entrada`, chamada dentro do `guardrail_insulto`) cobre
  e-mail, CPF, RG e telefone; os padrões regex ficam centralizados em `PADROES_PII` no topo
  de `guardrail.py`. O **Guardrail de saída** restaura os dados originais na resposta final,
  já que o modelo só viu os tokens anonimizados durante o processamento.
- O agente **FAQ** não tem tools de banco de dados — só orienta sobre funcionalidades e
  contatos do app, usando a tool `faq_retriever` (`iai/app/tools/faq.py`), que carrega a
  base de perguntas e respostas de `iai/data/faq_inventra.jsonl` via `JSONLoader` e deixa
  o próprio LLM escolher a entrada que melhor responde à pergunta do usuário.
- `llm_rapido` (Groq) atende roteamento, orquestração, guardrail de escopo e o agente
  FAQ — chamadas rápidas e baratas. `llm_guardrail` (Groq, `gpt-oss-safeguard-20b`) atende
  só o guardrail de insulto/moderação. `llm_especialista` (Gemini) atende os agentes de
  cargo (Estoquista, Comprador, Supervisor), que fazem tool-calling mais complexo.
- **Persistência de chat** (`memory.py`): toda mensagem trocada em `/chat` é salva na
  coleção `dbIAI.sessions` do Mongo, vinculada a `session_id`/`user_id`. Uma sessão fica
  "ativa" (sem `summary`) até alguém chamar `/chat/encerrar`, que lê as mensagens salvas e
  pede pro `llm_rapido` gerar um resumo curto da conversa. É esse fechamento explícito —
  chamado pelo app quando o usuário sai do chat — que separa uma sessão da próxima com o
  mesmo `session_id`; não há expiração automática por tempo. Importante: isso é o *log* da
  conversa, não o estado usado pelo LangGraph pra responder — esse continua no `MemorySaver`
  (`graph.py`), que é em memória e some a cada restart do processo.
- **Memória de longo prazo** (`tools/memoria.py`): a tool `buscar_historico` consulta, via
  `recuperar_historico`, os resumos já gerados (sessões encerradas) do **mesmo `user_id`** —
  não do `session_id` atual, então uma sessão nova do mesmo usuário enxerga o que ele
  perguntou em sessões passadas, mas um `user_id` diferente nunca vê o histórico de outro.
  O Roteador e os três agentes de cargo (Estoquista, Comprador, Supervisor) recebem essa
  tool; o `ROTEADOR_SYSTEM_PROMPT` instrui explicitamente quando usá-la (perguntas tipo "o
  que eu perguntei da última vez?"), e o `GUARDRAIL_ENTRADA_SYSTEM_PROMPT` permite esse tipo
  de pergunta passar mesmo sem citar estoque/compras. Pra tool conseguir identificar o
  usuário, `executar_fluxo_assessor` propaga `user_id` (além de `thread_id`) no
  `configurable` do grafo — sem isso a tool não sabe de quem buscar o histórico. Hoje o
  `user_id` do `ChatRequest` é informado livremente pelo chamador da API, sem autenticação;
  a garantia de isolamento por usuário depende de quem estiver na frente do IAI (app/gateway)
  sempre mandar o `user_id` correto.
