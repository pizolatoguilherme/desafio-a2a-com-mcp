# A Ponte: Central de Salas (agente A2A com MCP por dentro)

Dois processos Python, sem LLM:

| Processo | Porta | Endpoints | Papel |
|---|---|---|---|
| `servidor-mcp/servidor.py` | 7301 | `POST /mcp` (Streamable HTTP, stateless) | servidor MCP: 3 tools, o resource `politica://uso` e o ciclo de MRTR em `reservar_sala` |
| `agente/agente.py` | 7300 | `GET /.well-known/agent-card.json`, `POST /a2a` (JSON-RPC 2.0) | servidor A2A v1.0 por fora, host MCP por dentro |

Stack: Python 3.10+, SDK oficial `mcp==2.3.0` (revisão 2026-07-28) nos dois lados, Starlette e uvicorn. As versões ficam travadas em [servidor-mcp/pyproject.toml](servidor-mcp/pyproject.toml) e [agente/pyproject.toml](agente/pyproject.toml). Nenhum SDK de provedor de LLM.

## Como rodar

A partir de um clone limpo, na raiz do repositório:

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

Variáveis opcionais, todas com os padrões que o validador espera: `MCP_PORT` (7301), `AGENT_PORT` (7300), `MCP_URL` (`http://127.0.0.1:7301/mcp`) e `AGENT_PUBLIC_URL` (`http://127.0.0.1:7300`, usada na URL do card).

### O que procurar no stderr do servidor MCP

Cada request gera uma linha com método, id, alvo, se é retry, status HTTP, `traceparent` e as capabilities declaradas. Os requests que o SDK recusa também aparecem. Trecho real de um `SendMessage` do validador:

```
mcp method=tools/list id=1 alvo=- retry=nao http=200 traceparent=00-c798adcfbc38ece0755adae7e9232d8b-8c6bfe9b1ec4a4b8-01 capabilities={"elicitation":{"form":{},"url":{}}}
mcp method=resources/read id=2 alvo=politica://uso retry=nao http=200 traceparent=00-c798adcfbc38ece0755adae7e9232d8b-4b7f52c3477bdece-01 ...
mcp method=tools/call id=3 alvo=reservar_sala retry=nao http=200 traceparent=00-c798adcfbc38ece0755adae7e9232d8b-ab203e61cc80c6fa-01 ...
```

- `tools/list` vem antes do primeiro `tools/call` de cada Task. O agente descobre a tool em runtime, sem lista fixa.
- O trace-id impresso na primeira linha do validador aparece no `traceparent` de todos os requests MCP da Task. O span-id muda a cada request.
- Numa reserva que passou pela pausa, o par de `tools/call` tem ids diferentes, e o segundo aparece com `retry=sim`.

## Onde a ponte acontece

A costura fica toda em [agente/ponte.py](agente/ponte.py). Em [`Ponte._aplicar`](agente/ponte.py#L134-L143), o `resultType: input_required` que volta do MCP (já normalizado como `PrecisaInput` por [host_mcp.py](agente/host_mcp.py#L108-L130), que chama o SDK com `allow_input_required=True` para receber o resultado cru, em vez de deixar um callback de elicitation responder sozinho) vira `TASK_STATE_INPUT_REQUIRED`. Ali o agente guarda a chave do `inputRequests` e o `requestState` numa `Pendencia` ligada ao id da Task ([tarefas.py](agente/tarefas.py#L104-L131), num dicionário separado que nunca é serializado) e devolve só a linha `alternativas: …` na ordem do enum. O caminho de volta está em [`Ponte.continuar`](agente/ponte.py#L90-L132): o `SendMessage` com `taskId` e `escolha=<id>` (ou `escolha=recusar`, que vira `action: decline`) faz o agente repetir o `tools/call` original com os mesmos argumentos, `inputResponses` com a mesma chave e o `requestState` ecoado byte a byte ([linhas 120-126](agente/ponte.py#L120-L126)). O SDK gera um id JSON-RPC novo nesse retry. Do lado do servidor, o resolver [`escolha_de_sala`](servidor-mcp/servidor.py#L132-L145) é o que transforma conflito em `Elicit`. O servidor não faz callback ao cliente: ele encerra a resposta pedindo informação.

## Decisões técnicas

**Proteção do `requestState`.** Usei o utilitário do próprio SDK: `RequestStateSecurity(keys=[REQUEST_STATE_SECRET], ttl=600)` passado ao `MCPServer` ([servidor.py:49](servidor-mcp/servidor.py#L49)). Com isso o SDK instala o `RequestStateBoundary`, que faz o seguinte:
- **AES-256-GCM** com chave derivada por HKDF-SHA256, que dá integridade e confidencialidade. Trocar um caractere falha na tag GCM e devolve `-32602 Invalid or expired requestState`.
- Sela num envelope com `iat`/`exp` (**validade de 10 minutos**), o método, o nome da tool, um **digest dos argumentos** e a audiência (nome do servidor).
- No retry, argumentos diferentes dos selados não chegam a ter efeito: o estado é rejeitado com `-32602`, que é um dos dois caminhos aceitos.
- A chave vem só do ambiente. O processo não sobe sem ela, nem com menos de 32 bytes, e nada fica hardcoded.

**Servidor sem memória entre as rodadas.** O `requestState` carrega a resposta já perguntada e o digest da pergunta. No retry, o resolver roda de novo, recalcula as alternativas a partir dos dados e o SDK casa a resposta com a mesma pergunta. Por isso um retry funciona mesmo depois de reiniciar o processo, desde que com a mesma chave. Se as alternativas tiverem mudado entre as rodadas, a pergunta muda e o servidor pergunta de novo, em vez de aceitar uma resposta velha.

**`-32021` e erros de protocolo.** O SDK lança `-32021` com `data.requiredCapabilities = {"elicitation":{"form":{}}}` quando o resolver precisa perguntar e o cliente não declarou elicitation em form mode. `_meta` incompleto, header divergente do corpo (`-32020`) e URI de resource inexistente também saem do SDK, com HTTP 400 onde a spec exige. Não reimplementei nenhuma dessas checagens.

**Estado das Tasks.** Fica em memória no agente ([agente/tarefas.py](agente/tarefas.py)), com três dicionários indexados pelo id da Task: a Task (estado, histórico e artifacts), o pedido original (argumentos, versão da política e trace-id) e a pendência de MRTR. Cada Task tem o próprio lock, e duas Tasks pausadas ao mesmo tempo nunca compartilham `requestState`. Estado terminal é definitivo: um `SendMessage` para uma Task COMPLETED, CANCELED ou FAILED recebe o erro `-32004`.

**O agente só traduz protocolo.** Conflito, política e alternativas são decididos no servidor MCP. O agente faz o parse do formato fixo por expressão regular, confere a escolha contra o enum que o próprio servidor mandou e copia a mensagem de erro da tool para a Task FAILED.

**Reservas** ficam em memória no servidor e são perdidas a cada restart, como o enunciado permite.

### Limitação do SDK: capability de elicitation do agente

O enunciado pede que o agente declare `{"elicitation": {"form": {}}}`. O `Client` do SDK `mcp==2.3.0` não oferece um parâmetro público para escolher os modos de elicitation: quando há um `elicitation_callback`, ele sempre declara form **e** url (`mcp/client/session.py`, linhas 634-638):

```python
elicitation = (
    types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
    if self._elicitation_callback is not _default_elicitation_callback
    else None
)
```

Por isso os requests do agente saem com `{"elicitation":{"form":{},"url":{}}}`. Isso **declara elicitation em form mode**, que é o que o servidor verifica, e o log acima comprova. Segui a orientação do enunciado de não contornar o SDK reescrevendo o protocolo. O callback existe só para a capability ser declarada e nunca é chamado ([host_mcp.py](agente/host_mcp.py#L66-L72)), porque todas as chamadas usam `allow_input_required=True`.

## Saída do validador

Última execução, com os dois processos recém-iniciados:

```
trace-id desta execucao: c798adcfbc38ece0755adae7e9232d8b
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
