# Inteligência Artificial do Inventra

IA multiagente para o app **Inventra**, exposta como serviço HTTP (FastAPI), seguindo a arquitetura:

```
User
  -> Guardrail entrada (palavrões / prompt injection / anonimiza PII)
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
│   └── app/
│       ├── main.py             # app FastAPI: monta rotas e expõe GET /health
│       ├── config.py           # lê o .env, expõe GEMINI_API_KEY/GROQ_API_KEY (SecretStr)
│       ├── llms.py             # instancia os modelos (Gemini via llm_especialista, Groq via llm_rapido)
│       ├── schemas.py          # Estado do grafo (Estado), ChatRequest/ChatResponse, ResultadoGuardrail
│       ├── guardrail.py        # guardrail_entrada, guardrail_saida, anonimizar_entrada (PII)
│       ├── prompts.py          # system prompts de cada agente/etapa
│       ├── graph.py            # monta o grafo (LangGraph) e expõe executar_fluxo_assessor
│       └── routes/
│           └── chat.py         # POST /chat, chama executar_fluxo_assessor
├── tests/
│   └── test_guardrail.py
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
   - `POST http://127.0.0.1:8000/chat` com corpo `{"session_id": "teste", "pergunta": "quantos tomates temos em estoque?"}` → resposta do fluxo de agentes

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
para os módulos importarem sem erro (nenhum teste hoje chama a LLM de verdade).

## Notas de design

- O **Guardrail de entrada** primeiro filtra termos proibidos por regex/keyword (bloqueio
  imediato) e, se passar, anonimiza PII (e-mail, CPF) antes de mandar pro LLM classificador
  (`guardrail_entrada`), que decide se bloqueia com um motivo estruturado.
- O **Guardrail de saída** restaura os dados originais (PII) na resposta final, já que o
  modelo só viu os tokens anonimizados durante o processamento.
- O agente **FAQ** não tem tools de banco de dados — só orienta sobre funcionalidades e
  contatos do app.
- `llm_rapido` (Groq) atende roteamento, orquestração, guardrail de entrada e o agente
  FAQ — chamadas rápidas e baratas. `llm_especialista` (Gemini) atende os agentes de
  cargo (Estoquista, Comprador, Supervisor), que fazem tool-calling mais complexo.
