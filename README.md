# A Ponte: Central de Salas (agente A2A com MCP por dentro)

Dois processos Python, sem LLM:

| Processo | Porta | Endpoints | Papel |
|---|---|---|---|
| `servidor-mcp/servidor.py` | 7301 | `POST /mcp` (Streamable HTTP, stateless) | servidor MCP: 3 tools, o resource `politica://uso` e o ciclo de MRTR em `reservar_sala` |
| `agente/agente.py` | 7300 | `GET /.well-known/agent-card.json`, `POST /a2a` (JSON-RPC 2.0) | servidor A2A v1.0 por fora, host MCP por dentro |

Stack: Python 3.10+, SDK oficial `mcp==2.3.0` (revisão 2026-07-28) nos dois lados, Starlette e uvicorn. As versões ficam travadas em [servidor-mcp/pyproject.toml](servidor-mcp/pyproject.toml), [agente/pyproject.toml](agente/pyproject.toml) e [testes/pyproject.toml](testes/pyproject.toml). Nenhum SDK de provedor de LLM.

## Como rodar

A partir de um clone limpo, na raiz do repositório. Só é preciso ter `python3` (3.10 ou superior):

```bash
python3 -m venv .venv
.venv/bin/pip install ./servidor-mcp ./agente
```

O servidor MCP exige a chave de integridade do `requestState` em `REQUEST_STATE_SECRET`, com no mínimo 32 bytes de aleatoriedade. Gere a sua e exporte no terminal do servidor. Nunca faça commit dela:

```bash
export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
```

Para testar o retry depois de reiniciar o servidor, reinicie no **mesmo** terminal, para manter a mesma chave. Com outra chave, o estado é rejeitado com `-32602`, como deve ser.

**Terminal 1, servidor MCP** (o stderr dele é o log de requests):

```bash
.venv/bin/python servidor-mcp/servidor.py
```

**Terminal 2, agente:**

```bash
.venv/bin/python agente/agente.py
```

**Terminal 3, validador** (com os dois processos recém-iniciados):

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

**Testes da entrega** (opcional): são 79 testes próprios, além das 36 verificações do validador. Eles sobem os dois processos em portas livres, então não conflitam com o que estiver rodando nas portas 7300 e 7301:

```bash
.venv/bin/pip install ./testes
.venv/bin/python -m pytest testes
```

### Variáveis de ambiente

| Variável | Processo | Padrão | Para quê |
|---|---|---|---|
| `REQUEST_STATE_SECRET` | servidor-mcp | obrigatória | chave de integridade do `requestState`, com ≥ 32 bytes |
| `REQUEST_STATE_TTL` | servidor-mcp | `600` | validade do `requestState` em segundos, entre 300 e 1800 (fora disso, o processo não sobe) |
| `MCP_PORT` / `MCP_HOST` | servidor-mcp | `7301` / `127.0.0.1` | onde o servidor atende |
| `MCP_URL` | agente | `http://127.0.0.1:7301/mcp` | onde o agente encontra o servidor MCP |
| `AGENT_PORT` / `AGENT_HOST` | agente | `7300` / `127.0.0.1` | onde o agente atende |
| `AGENT_PUBLIC_URL` | agente | `http://127.0.0.1:7300` | base da URL anunciada no Agent Card |

### O que procurar no stderr

O servidor MCP imprime, na subida, a versão da política e a validade do `requestState`. Depois, uma linha por request: método, id, alvo, se é retry, status HTTP, `traceparent` e as capabilities declaradas. Os requests que o SDK recusa também aparecem. Trecho real de um `SendMessage` do validador que passou pela pausa:

```
mcp central-de-salas em http://127.0.0.1:7301/mcp (politica 2026-11-01, requestState valido por 600s)
...
mcp method=tools/list id=4 alvo=- retry=nao http=200 traceparent=00-99ad0d57086b6beb6803e4406666134d-... capabilities={"elicitation":{"form":{},"url":{}}}
mcp method=resources/read id=5 alvo=politica://uso retry=nao http=200 traceparent=00-99ad0d57086b6beb6803e4406666134d-...
mcp method=tools/call id=6 alvo=reservar_sala retry=nao http=200 traceparent=00-99ad0d57086b6beb6803e4406666134d-b39e7e51ddf5937b-01 ...
mcp method=tools/call id=7 alvo=reservar_sala retry=sim http=200 traceparent=00-99ad0d57086b6beb6803e4406666134d-fc95f6ace795e2ed-01 ...
```

- `tools/list` vem antes do primeiro `tools/call` de cada Task. O agente descobre a tool em runtime, sem lista fixa.
- O trace-id impresso na primeira linha do validador aparece no `traceparent` de todos os requests MCP da Task. O span-id muda a cada request.
- Na pausa, o par de `tools/call` tem ids diferentes (6 e 7), e o segundo aparece com `retry=sim`.

O agente registra no stderr cada chamada A2A e cada mudança de estado da Task (`a2a task=… estado=TASK_STATE_WORKING -> TASK_STATE_INPUT_REQUIRED`).

## Onde a ponte acontece

A costura fica toda em [agente/ponte.py](agente/ponte.py). Em [`Ponte._aplicar`](agente/ponte.py#L138-L147), o `resultType: input_required` que volta do MCP vira `TASK_STATE_INPUT_REQUIRED`. Ele chega já normalizado como `PrecisaInput` por [`HostMCP.chamar_tool`](agente/host_mcp.py#L169-L199), que chama o SDK com `allow_input_required=True` para receber o resultado cru, em vez de deixar um callback de elicitation responder sozinho. Ali o agente guarda a chave do `inputRequests` e o `requestState` numa `Pendencia` ligada ao id da Task ([tarefas.py](agente/tarefas.py#L110-L138), num dicionário separado que nunca é serializado) e devolve só a linha `alternativas: …` na ordem do enum.

O caminho de volta está em [`Ponte.continuar`](agente/ponte.py#L90-L136). O `SendMessage` com `taskId` e `escolha=<id>` (ou `escolha=recusar`, que vira `action: decline`) faz o agente repetir o `tools/call` original com os mesmos argumentos, `inputResponses` com a mesma chave e o `requestState` ecoado byte a byte ([linhas 119-125](agente/ponte.py#L119-L125)). O SDK gera um id JSON-RPC novo nesse retry.

Do lado do servidor, [`reservar_sala`](servidor-mcp/servidor.py#L234-L266) devolve o `InputRequiredResult` montado em [`_perguntar`](servidor-mcp/servidor.py#L182-L196). O servidor não faz callback ao cliente: ele encerra a resposta pedindo informação, e é o cliente quem volta com um request novo.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do próprio SDK: `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=…)` passado ao `MCPServer` ([servidor.py:79](servidor-mcp/servidor.py#L79)). Com isso o SDK instala o `RequestStateBoundary`, que faz o seguinte:
- **AES-256-GCM** com chave derivada por HKDF-SHA256, que dá integridade e confidencialidade. Trocar um caractere falha na tag GCM e devolve `-32602 Invalid or expired requestState`.
- Sela num envelope com `iat`/`exp`, o método, o nome da tool, um **digest dos argumentos** e a audiência (nome do servidor).
- No retry, argumentos diferentes dos selados não chegam a ter efeito: o estado é rejeitado com `-32602`, que é um dos dois caminhos aceitos.
- A chave vem só do ambiente. O processo não sobe sem ela, nem com menos de 32 bytes, e nada fica hardcoded.

**Validade.** São **10 minutos** por padrão, configuráveis por `REQUEST_STATE_TTL`, sempre dentro da faixa de 5 a 30 minutos do enunciado: fora dela, o servidor recusa subir. Um teste prova que um token autêntico, porém vencido, é recusado com `-32602` e não cria reserva.

**Servidor sem memória entre as rodadas.** O `requestState` carrega tudo o que a segunda rodada precisa: o pedido original e as alternativas que foram oferecidas (`{"v":1,"pedido":{…},"alternativas":[…]}`, cifrado pelo boundary). No retry, o servidor reconstrói o pedido a partir do estado selado, confere que a escolha está entre as alternativas seladas, verifica que a sala escolhida continua livre e conclui. Por isso o retry funciona mesmo depois de reiniciar o processo, desde que com a mesma chave. Se a sala escolhida tiver sido ocupada entre as rodadas, o servidor pergunta de novo com as alternativas daquele momento.

A primeira versão usava o resolver `Resolve`/`Elicit` do SDK, que reexecuta a pergunta no retry e só aceita a resposta se a pergunta recalculada for idêntica à selada. O fluxo do avaliador expôs o problema: no passo 8 o mirante é reservado, e no passo 11 o estado sela a pergunta com `[sala-fusca]`. O restart do passo 12 zera as reservas, a pergunta recalculada vira `[sala-fusca, sala-mirante]` e o SDK descarta a resposta, devolvendo `input_required` de novo em vez de concluir. Montar o `InputRequiredResult` na tool, com o pedido e as alternativas dentro do estado, cumpre o requisito de reconstruir o pedido a partir do `requestState`. O teste `test_retry_apos_restart_conclui_mesmo_que_as_alternativas_tenham_mudado` reproduz esse cenário.

**A escolha é validada no servidor.** O `content` do `accept` precisa estar entre as alternativas seladas no `requestState`. Uma resposta fora delas, seja de uma sala inexistente ou de uma sala de capacidade menor, termina em `isError` e não cria reserva, mesmo vinda de um cliente MCP que não seja o nosso agente.

**`-32021` e erros de protocolo.** Antes de perguntar, o servidor lê a capability do `_meta` deste request (nunca de um anterior). Se o cliente não declarou elicitation em form mode, responde `-32021` com `data.requiredCapabilities = {"elicitation":{"form":{}}}` e HTTP 400 ([`_perguntar`](servidor-mcp/servidor.py#L182-L196)), com a mesma regra do SDK: `elicitation: {}` sem modos conta como form, e só url não conta. `_meta` incompleto, header divergente do corpo (`-32020`), `requestState` adulterado, vencido ou com argumentos divergentes, e URI de resource inexistente saem do próprio SDK, com HTTP 400 onde a spec exige.

**Estado das Tasks.** Fica em memória no agente ([agente/tarefas.py](agente/tarefas.py)), com três dicionários indexados pelo id da Task: a Task (estado, histórico e artifacts), o pedido original (argumentos, versão da política e trace-id) e a pendência de MRTR. Cada Task tem o próprio lock, e duas Tasks pausadas ao mesmo tempo nunca compartilham `requestState`. Estado terminal é definitivo: um `SendMessage` para uma Task COMPLETED, CANCELED ou FAILED recebe o erro `-32004` (UnsupportedOperation), e uma Task desconhecida recebe `-32001` (TaskNotFound).

**Queda do servidor MCP.** O agente distingue dois casos:
- **O servidor respondeu com erro** (por exemplo, `requestState` vencido): a Task termina em FAILED.
- **O servidor não chegou a responder** (falha de rede): se a Task estava na primeira chamada, ela termina em FAILED. Se estava na retomada, continua em `TASK_STATE_INPUT_REQUIRED` com a mesma pendência, porque o `requestState` continua válido. Quando o servidor volta, a mesma escolha conclui a reserva.

**O agente só traduz protocolo.** Conflito, política e alternativas são decididos no servidor MCP. O agente faz o parse do formato fixo por expressão regular, confere a escolha contra o enum que o próprio servidor mandou e copia a mensagem de erro da tool para a Task FAILED.

**Reservas** ficam em memória no servidor e são perdidas a cada restart, como o enunciado permite.

### Limitações do SDK

O enunciado pede que limitações reais do SDK sejam documentadas, com evidência, em vez de contornadas reescrevendo o protocolo.

**1. Capability de elicitation do agente.** O enunciado pede `{"elicitation": {"form": {}}}`. O `Client` do `mcp==2.3.0` não oferece um parâmetro público para escolher os modos de elicitation: quando há um `elicitation_callback`, ele sempre declara form **e** url (`mcp/client/session.py`, linhas 634-638):

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

Por isso os requests do agente saem com `{"elicitation":{"form":{},"url":{}}}`. Isso **declara elicitation em form mode**, que é o que o servidor verifica, e o log acima comprova. O callback existe só para a capability ser declarada e nunca é chamado ([host_mcp.py](agente/host_mcp.py#L115-L121)), porque todas as chamadas usam `allow_input_required=True`.

**2. Uma queda de conexão inutiliza o `Client`.** No `mcp==2.3.0`, um `httpx2.ConnectError` dentro do transporte Streamable HTTP derruba o grupo de tarefas do `Client` (`mcp/client/streamable_http.py`, `_run_request_post`). A partir daí, toda chamada falha com `MCPError(-32000, 'Connection closed')`, mesmo depois que o servidor volta. Reproduzi: com o agente no ar, derrubei o servidor MCP, fiz uma chamada e subi o servidor de novo, e até Tasks novas falhavam. Recriar o `Client` resolveria a queda, mas reiniciaria o contador de ids, o que ameaça a garantia de que o retry sai com um id diferente do request inicial. A solução fica na camada HTTP, abaixo do protocolo: o agente passa ao SDK um `httpx2.AsyncClient` com um [`TransporteResiliente`](agente/host_mcp.py#L48-L76). Ele converte a falha de rede numa resposta 503 com um erro JSON-RPC marcado como de transporte, que o SDK entrega como erro comum. O `Client` sobrevive e o contador segue (no teste, o retry depois da queda saiu com id 5, depois do 3 inicial e do 4 que falhou). Nada do protocolo MCP foi reescrito.

### Testes

Ficam em [testes/](testes/): 79 testes, todos sobre processos reais e HTTP de verdade, sem mocks do protocolo.
- **Unidade:** regras de sala e de política, a ordem das alternativas, a máquina de estados da Task e o parse do formato fixo.
- **Servidor MCP:** recusa de subida sem chave ou com TTL fora da faixa, `requestState` vencido ou de outra chave, retry depois de reiniciar o processo, resposta fora do enum, `decline`/`cancel`, capability lida a cada request e registro no stderr.
- **Agente:** um proxy grava o que o agente envia ao servidor MCP e confere headers espelhados, `_meta`, ordem `tools/list` → `resources/read` → `tools/call`, trace-id propagado com span novo, id novo no retry, a mesma chave e o `requestState` ecoado intacto. Também cobre a queda do servidor durante a pausa, os erros `-32001`/`-32004` e o card.
- **Entrega:** o validador do starter rodado de ponta a ponta, conferindo que o trace-id dele aparece no stderr do servidor; `dados/`, `validador/` e `exemplos/` intocados; nenhum segredo versionado; versões travadas; as quatro seções deste README.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: 99ad0d57086b6beb6803e4406666134d
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```

Exit code: `0`.
