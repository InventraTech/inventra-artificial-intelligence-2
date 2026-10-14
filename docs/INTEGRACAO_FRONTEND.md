# Contrato para frontend de aprovação e autenticação

Data: 14/10/2026. A página pertence ao frontend externo; não foi implementada nesta branch.

## Arquitetura de identidade

Fluxo recomendado: frontend autenticado no INVENTRA → HTTPS/gateway → IAI →
`GET /api/users/me` no Spring usando o JWT recebido. O Spring valida assinatura,
expiração, conta ativa, perfil e cozinha; a IAI usa a identidade retornada e não confia
em IDs/papéis/cozinhas enviados no corpo. O gateway deve limitar exposição, rate limit e
tamanho de payloads. Não registrar o header Authorization. JWT nunca deve entrar em
prompts, traces de modelos, sessões ou documentos de aprovação.

Refresh/login/logout continuam no INVENTRA; a IAI não guarda refresh tokens.
CORS usa origens explícitas e Authorization; não implica autenticação.
Use HTTPS fora de localhost. Quando possível, prefira sessão/BFF com cookies seguros
HttpOnly/SameSite e proteção CSRF na camada correspondente; esse BFF não está nesta branch.

## Endpoints da IAI

Todos, exceto saúde e documentação OpenAPI, exigem Bearer. Mock usa identificadores demo
fixos. API não aceita arbitrariamente perfil ou cozinha.

| Método e rota | Contrato |
|---|---|
| POST /chat | session_id, user_id, pergunta; request_id opcional; retorna resposta |
| POST /chat/encerrar | session_id; encerra somente sessão do usuário/cozinha |
| GET /chat/historico/{user_id} | somente próprio usuário; busca textual e limite 1–100 |
| GET /chat/mensagens/{doc_id} | somente proprietário; não expõe avaliações internas do juiz |
| POST /queries | Query tipada, apenas leitura |
| GET /queries/capabilities | schemas dos parâmetros e limitações por operação |
| POST /approvals | ProposalInput; requer idempotency_key estável |
| GET /approvals | solicitante vê próprias; Supervisor vê cozinha; status/offset/limit |
| GET /approvals/inbox | somente Supervisor; pendentes da própria cozinha |
| GET /approvals/{id} | detalhes conforme propriedade/papel/cozinha |
| POST /approvals/{id}/decision | Supervisor da cozinha, decisão vinculada ao hash |
| POST /approvals/{id}/execute | somente proposta aprovada; identidade permitida para execução |
| GET /metrics | métricas próprias ou da cozinha para Supervisor |
| GET /health | modos e condição do armazenamento, não verifica disponibilidade dos modelos |

`/openapi.json` e `/docs` são os schemas executáveis. Não inserir tokens em URL.
Respostas incluem cabeçalhos `X-IAI-Auth-Mode`, `X-IAI-Business-Mode`,
`X-IAI-Storage-Mode`, `X-IAI-LLM-Mode`. A página deve mostrar aviso em modo mock.

### Criar proposta

```json
{
  "action": {
    "operation": "registrar_saida",
    "parameters": {"batchId": 1, "quantity": "10.000"}
  },
  "justification": "Consumo de arroz no almoço",
  "idempotency_key": "solicitacao-cliente-001"
}
```

Os campos de operação não aceitam user_id, kitchenId ou token. Kitchen e requester são
atribuídos pelo servidor. Parâmetros adicionais são recusados. A resposta inclui:

- id e proposal_hash;
- requester_id, requester (identidade), kitchen_id;
- action.operation e action.parameters;
- before (retrato de cada referência autorizada);
- justification, created_at, expires_at, status;
- decision, decided_by, decided_at, decision_reason;
- technical_executor, result, execution_error, audit;
- business_mode e notification.channel=supervisor_inbox.

### Aprovar ou recusar

```json
{
  "decision": "approve",
  "proposal_hash": "<hash recebido no detalhe da proposta>",
  "reason": "Conferido"
}
```

Use `reject` para recusar. Não envie uma proposta editada no endpoint de decisão.
Para corrigir dados, crie nova proposta, com nova chave e nova aprovação.
Solicitações idênticas com mesma chave retornam o mesmo ID; reutilizar a chave com dados
diferentes retorna 409. Clique repetido na mesma decisão não dispara nova execução.
Após decidir, consulte o estado retornado. Não interpretar HTTP 200 como execução:
`APROVADA_PENDENTE_INTEGRACAO` significa aprovação registrada, sem alteração de negócio.

## Página necessária

Mostrar solicitante, cozinha, operação, parâmetros, dados atuais registrados em before,
justificativa, datas, estado e indicação do modo. Renderizar dados como texto seguro, sem
executar HTML ou instruções recebidas em nomes/justificativas.
Botões aprovar/recusar somente para Supervisor, com decisão conferida novamente no servidor.
Autoaprovação permitida. Exibir recusa/erro/incerteza; bloquear cliques durante envio.
Atualizar lista após decisão e apresentar o resultado da execução com fonte/mocked.

A caixa de entrada é a notificação persistente disponível nesta branch: listar/polling.
Não há push, e-mail, WhatsApp ou página construída. O frontend deverá consumir a inbox.
O solicitante acompanha o resultado por listagem/detalhe; não há envio assíncrono externo.

## Estados e execução

`AGUARDANDO_APROVACAO → RECUSADA | EXPIRADA | APROVADA_PENDENTE_INTEGRACAO`.

`APROVADA_PENDENTE_INTEGRACAO → EM_EXECUCAO → CONCLUIDA | FALHOU | RESULTADO_INCERTO`.
Pendentes de integração também expiram ao completar sete dias.
Execuções em andamento sem conclusão por dez minutos ficam incertas e não são repetidas.
Expiração é materializada pelo trabalhador a cada 30 segundos e ao consultar/decidir.

A execução usa operação imutável e revalida o retrato do alvo. Se o registro mudou,
exige nova proposta; ainda é necessária validação transacional no backend para impedir
mudança entre essa rechecagem e a gravação externa.
Mongo não consegue garantir uma transação única com a API HTTP.

No mock, execução usa solicitante original e registra aprovador e executor técnico.
No real, não usar o JWT do gestor em nome do solicitante. Sem delegação, manter pendente.
O próprio solicitante pode retomar usando JWT novo; Spring decide suas permissões.
Não persistir JWT aguardando decisão. Tokens demonstrativos nunca vão à API real.
Não há endpoint inventado de delegação ou idempotência.

A API deverá futuramente oferecer execução delegada confiável, idempotência vinculada ao
ID da proposta e consulta de estado para reconciliação. O executor autorizado precisa
identificar o solicitante original, a cozinha e a decisão específica. Não aceitar
headers arbitrários como delegação sem autenticação/validação no Spring.

Edição de contagem não tem endpoint documentado. DELETE de item de requisição não possui
remoção lógica comprovada. Essas integrações ficam pendentes; não remover/recriar contagem
nem apagar registros para contornar os contratos.

## Erros e recuperação

401 identidade inválida; 403 falta de permissão; 404 recurso fora do escopo/inexistente;
409 conflito, expiração ou proposta alterada; 422 parâmetros inválidos; 502 fluxo/modelo indisponível; 503 persistência/
identidade indisponível. Resultados das consultas possuem ok/code/data/source/mocked/incomplete.
Erros de negócio/executores ficam no documento de aprovação; mostrar o estado.

Em timeout depois de enviar uma escrita, consultar o ID da solicitação. Nunca criar nova
aprovação ou repetir execução automaticamente para tentar eliminar a incerteza.
Reconciliação externa futura deve conferir a operação original e registrar o resultado;
esta branch não oferece uma ação administrativa de “forçar repetição”.
