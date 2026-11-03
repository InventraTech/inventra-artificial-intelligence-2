# Provedores de LLM e fallback

O IAI nunca depende de uma única chave. Cada papel do fluxo tem uma lista de candidatos em ordem de
preferência (uma entrada por chave de cada provedor). Se uma chamada falha, **a mesma requisição segue para o
próximo candidato** e o usuário não percebe nada: o erro só aparece no log do servidor. O app mobile não muda.

Código: [`rodizio.py`](../iai/app/core/assistant/rodizio.py) (a regra) e [`llms.py`](../iai/app/core/assistant/llms.py)
(as listas).

## Ordem de cada papel

| Papel | Onde é usado | Ordem de tentativa |
|---|---|---|
| `llm_especialista` | Estoquista, Comprador, Supervisor, FAQ | OpenRouter Luna → OpenAI Luna → Gemini (todas as chaves) → Groq `gpt-oss-20b` |
| `llm_rapido` | Roteador, orquestrador, guardrail de escopo, resumos, escolha de gráfico | Groq `gpt-oss-20b` (todas as chaves) → OpenRouter Luna → OpenAI Luna → Gemini |
| `llm_guardrail` | Guardrail de insulto | Groq `gpt-oss-safeguard-20b` (todas as chaves); se todas falham, o código já cai no `llm_rapido` |
| `llm_gemini` | Guardrail de arquivos, descrição de imagem | Gemini (todas as chaves) → OpenRouter Luna → OpenAI Luna |

- Provedor sem chave configurada simplesmente não entra na lista.
- Chaves do mesmo grupo (as seis da Groq, as cinco do Gemini) **se revezam** a cada chamada, para dividir o limite
  de cada uma em vez de esgotar a primeira. A preferência entre provedores (Luna antes do Gemini) é mantida.
- O mesmo vale para áudio (Whisper, chaves Groq) e embeddings de documentos (chaves Gemini).

## O que faz o fallback disparar

| Falha | Exemplo | O que o rodízio faz |
|---|---|---|
| Limite (429) | Gemini grátis (5 req/min), Groq TPM/RPD | Passa para o próximo. A chave fica em espera pelo `Retry-After` ou pelo tempo da mensagem ("retry in 36s"); sem dica, 60 s. Limite diário: 30 min |
| Sem crédito | OpenRouter 402, OpenAI `insufficient_quota` | Passa para o próximo; espera 5 min e tenta de novo |
| Chave ou modelo recusado | 401, 403, 404, "API key not valid" | Passa para o próximo; espera 30 min |
| Provedor fora | 5xx, timeout, conexão | Passa para o próximo; espera 20 s |
| Pedido recusado | 400 (schema, formato) | Passa para o próximo, sem pôr ninguém em espera (o problema é o pedido) |

Enquanto uma chave está em espera, as chamadas seguintes **pulam** essa chave sem ir à rede. Se todas
estiverem em espera, o rodízio ainda tenta todas (a estimativa de espera pode estar errada). Só se nenhuma
responder o erro chega ao fluxo, que já responde com a mensagem de falha de sempre.

Os SDKs não repetem sozinhos (`max_retries=0`): repetir a mesma chave só atrasa. O tempo máximo por tentativa é
`LLM_TIMEOUT` (especialistas, 40 s) ou `LLM_TIMEOUT_LEVE` (modelos leves, 20 s).

## O que aparece no terminal

```
LLM groq/gpt-oss-20b#1 falhou (limite, HTTP 429); em espera por 5s: RateLimitError ...
LLM openrouter/luna falhou (credito, HTTP 402); em espera por 300s: ...
```

O rótulo (`groq/gpt-oss-20b#1`, `gemini#3`, `openai/luna`) identifica **qual** chave falhou, sem mostrar a chave.
Chaves e tokens são removidos de qualquer log.

## Variáveis (`iai/.env`)

| Variável | Uso |
|---|---|
| `GROQ_API_KEY`, `GROQ_API_KEY_2`, ... | Modelos leves (roteador, guardrails) e Whisper |
| `GEMINI_API_KEY`, `GEMINI_API_KEY_2`, ... | Gemini: reserva dos especialistas, arquivos, embeddings |
| `OPEN_ROUTER_API_KEY` | Luna via OpenRouter (1ª opção do especialista). Precisa de créditos comprados |
| `OPEN_AI_KEY` | Luna direto na OpenAI (2ª opção) |
| `LUNA_MODEL_OPENROUTER` / `LUNA_MODEL_OPENAI` | `openai/gpt-6-luna` / `gpt-6-luna` |
| `LUNA_REASONING_EFFORT` | `low` (padrão). Mais alto = respostas melhores e mais lentas |
| `GEMINI_MODEL` | `gemini-3.5-flash` (funciona em todas as chaves; o 2.5 já não aceita projetos novos) |
| `LLM_TIMEOUT` / `LLM_TIMEOUT_LEVE` | 40 s / 20 s |

Também são aceitos os nomes `OPENAI_API_KEY` e `OPENROUTER_API_KEY`. Em produção (k3s), as variáveis novas
precisam ir para o secret do deploy, senão só as chaves antigas entram no rodízio.

## Limites que importam

- **Groq:** o limite é por organização. Chaves de uma mesma conta dividem o limite; chaves de contas diferentes
  somam (confirmado: os contadores das seis chaves atuais são independentes).
- **Gemini grátis:** o limite é por **projeto** e por modelo (5 requisições por minuto no `gemini-3.5-flash`).
  Chaves do mesmo projeto do Google Cloud dividem esse limite. É a reserva mais fraca da lista.
- **OpenRouter:** o limite de gasto da chave (US$ 6) não substitui crédito. Sem créditos comprados o pedido
  volta 402 e o rodízio passa para a OpenAI.
- **OpenAI:** recusa `pattern` com lookaround no schema das tools (o Pydantic gera isso para `Decimal`).
  `ChatOpenAISemLookaround` remove esses trechos antes de enviar.

## Limitações conhecidas

- Se um provedor cair **no meio** de uma resposta já enviada em partes, o erro sobe (só dá para trocar de
  provedor antes do primeiro pedaço).
- O resumo do raciocínio (`pensamento` no `/chat/stream`) só aparece quando o Gemini responde; a Luna não envia.
- O JEV (TypeSafe) não faz parte do rodízio: indisponível, a escrita é cancelada (falha fechada), como antes.
- A espera das chaves fica na memória do processo: reiniciar o servidor zera, e cada réplica aprende sozinha.

## Conferir depois de um deploy

`GET /health` mostra quantas chaves cada provedor carregou (só a contagem, nunca a chave):

```json
"llm_chaves": {"groq": 6, "gemini": 5, "openrouter": true, "openai": true}
```

Se um número vier menor que o esperado, a variável não chegou ao servidor (ver o secret do deploy). Confira
também o log do pod: cada falha aparece como `LLM <rótulo> falhou (...)`.
