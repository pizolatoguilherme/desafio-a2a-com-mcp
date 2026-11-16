"""Fronteira A2A + host MCP: o agente de verdade, com um proxy gravando o fio MCP.

O proxy fica entre o agente e o servidor MCP, entao estes testes conferem o que
o agente realmente emite: ordem dos metodos, headers espelhados, _meta,
traceparent, id novo no retry, a mesma chave e o requestState ecoado intacto.
"""

from __future__ import annotations

import json
import secrets
import urllib.request

from conftest import estado_de, h, task_de, texto_de

PEDIDO_CONFLITO = f"reservar sala=sala-garagem inicio={h('14:00')} fim={h('15:00')} responsavel=Marty"
PEDIDO_LIVRE = f"reservar sala=sala-porao inicio={h('09:00')} fim={h('10:00')} responsavel=Doc"
NOMEADOS = {"tools/call": "name", "resources/read": "uri"}


def _traceparent() -> tuple[str, str]:
    trace_id = secrets.token_hex(16)
    return trace_id, f"00-{trace_id}-{secrets.token_hex(8)}-01"


def test_cada_request_mcp_do_agente_leva_meta_e_headers_espelhados(a2a, proxy):
    a2a.enviar(PEDIDO_LIVRE)
    requests = proxy.jsonrpc()
    assert requests, "o agente nao falou com o servidor MCP"
    for troca in requests:
        corpo, headers = troca["corpo"], troca["headers"]
        meta = corpo["params"]["_meta"]
        assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
        assert meta["io.modelcontextprotocol/clientCapabilities"]["elicitation"]["form"] == {}
        assert headers["mcp-protocol-version"] == "2026-07-28"
        assert headers["mcp-method"] == corpo["method"]
        if corpo["method"] in NOMEADOS:
            assert headers["mcp-name"] == corpo["params"][NOMEADOS[corpo["method"]]]
        assert troca["status"] == 200


def test_descobre_tools_e_le_a_politica_antes_de_chamar(a2a, proxy):
    a2a.enviar(PEDIDO_LIVRE)
    metodos = [t["corpo"]["method"] for t in proxy.jsonrpc()]
    assert metodos.index("tools/list") < metodos.index("tools/call")
    assert metodos.index("resources/read") < metodos.index("tools/call")


def test_ponte_completa_no_fio(a2a, proxy):
    trace_id, traceparent = _traceparent()
    pausa = a2a.enviar(PEDIDO_CONFLITO, traceparent=traceparent)
    assert estado_de(pausa) == "TASK_STATE_INPUT_REQUIRED"
    assert texto_de(pausa) == "alternativas: sala-fusca, sala-mirante"

    fim = a2a.enviar("escolha=sala-mirante", task_id=task_de(pausa)["id"], traceparent=traceparent)
    assert estado_de(fim) == "TASK_STATE_COMPLETED"
    reserva = json.loads(task_de(fim)["artifacts"][0]["parts"][0]["text"])
    assert reserva["sala"] == "sala-mirante" and reserva["politica"] == "2026-11-01"

    chamadas = [t for t in proxy.jsonrpc() if t["corpo"]["method"] == "tools/call"]
    assert len(chamadas) == 2
    inicial, retry = chamadas
    pedido_mcp = inicial["resposta"]["result"]
    chave = next(iter(pedido_mcp["inputRequests"]))

    assert retry["corpo"]["id"] != inicial["corpo"]["id"]
    assert retry["corpo"]["params"]["requestState"] == pedido_mcp["requestState"]
    assert retry["corpo"]["params"]["inputResponses"] == {
        chave: {"action": "accept", "content": {"sala": "sala-mirante"}}
    }
    assert retry["corpo"]["params"]["arguments"] == inicial["corpo"]["params"]["arguments"]

    # todos os requests MCP da Task carregam o trace-id do cliente A2A, cada um com span proprio
    traces = [t["corpo"]["params"]["_meta"]["traceparent"] for t in proxy.jsonrpc()]
    assert all(tp.split("-")[1] == trace_id for tp in traces)
    assert len({tp.split("-")[2] for tp in traces}) == len(traces)

    # e o requestState nunca chegou ao cliente A2A
    assert not any(pedido_mcp["requestState"][:40] in r for r in a2a.respostas_brutas)


def test_recusa_vira_decline_e_cancela_a_task(a2a, proxy):
    pausa = a2a.enviar(PEDIDO_CONFLITO)
    fim = a2a.enviar("escolha=recusar", task_id=task_de(pausa)["id"])
    assert estado_de(fim) == "TASK_STATE_CANCELED"
    retry = [t for t in proxy.jsonrpc() if t["corpo"]["method"] == "tools/call"][-1]
    assert list(retry["corpo"]["params"]["inputResponses"].values()) == [{"action": "decline"}]


def test_escolha_fora_do_enum_nao_toca_o_servidor(a2a, proxy):
    pausa = a2a.enviar(PEDIDO_CONFLITO)
    antes = len(proxy.jsonrpc())
    de_novo = a2a.enviar("escolha=sala-aquario", task_id=task_de(pausa)["id"])
    assert estado_de(de_novo) == "TASK_STATE_INPUT_REQUIRED"
    assert texto_de(de_novo) == texto_de(pausa)
    assert len(proxy.jsonrpc()) == antes


def test_falha_de_rede_na_retomada_mantem_a_task_pausada(a2a, servidor_mcp):
    pausa = a2a.enviar(PEDIDO_CONFLITO)
    task_id = task_de(pausa)["id"]

    servidor_mcp.parar()
    sem_servidor = a2a.enviar("escolha=sala-fusca", task_id=task_id)
    assert estado_de(sem_servidor) == "TASK_STATE_INPUT_REQUIRED"
    assert texto_de(sem_servidor) == "alternativas: sala-fusca, sala-mirante"

    # O servidor volta (mesma chave, memoria zerada) e o mesmo requestState ainda vale.
    servidor_mcp.subir()
    fim = a2a.enviar("escolha=sala-fusca", task_id=task_id)
    assert estado_de(fim) == "TASK_STATE_COMPLETED"
    assert json.loads(task_de(fim)["artifacts"][0]["parts"][0]["text"])["sala"] == "sala-fusca"


def test_erro_da_tool_falha_a_task_com_a_mensagem_exata(a2a):
    resposta = a2a.enviar(f"reservar sala=sala-delorean inicio={h('09:00')} fim={h('10:00')} responsavel=Doc")
    assert estado_de(resposta) == "TASK_STATE_FAILED"
    assert "Sala inexistente: sala-delorean" in texto_de(resposta)


def test_pedido_fora_do_formato_falha_sem_chamar_o_servidor(a2a, proxy):
    resposta = a2a.enviar("reserva a garagem pra mim")
    assert estado_de(resposta) == "TASK_STATE_FAILED"
    assert proxy.jsonrpc() == []


def test_task_terminal_e_task_inexistente_sao_recusadas(a2a):
    fim = a2a.enviar(PEDIDO_LIVRE)
    assert estado_de(fim) == "TASK_STATE_COMPLETED"
    terminal = a2a.enviar("escolha=sala-fusca", task_id=task_de(fim)["id"])
    assert terminal["error"]["code"] == -32004

    inexistente = a2a.enviar("escolha=sala-fusca", task_id="task-que-nao-existe")
    assert inexistente["error"]["code"] == -32001
    assert a2a.rpc("GetTask", {"id": "task-que-nao-existe"})["error"]["code"] == -32001


def test_get_task_reflete_o_estado_corrente(a2a):
    pausa = a2a.enviar(PEDIDO_CONFLITO)
    lida = a2a.rpc("GetTask", {"id": task_de(pausa)["id"]})
    assert task_de(lida)["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"
    assert task_de(lida)["contextId"] == task_de(pausa)["contextId"]


def test_card_v1_aponta_para_o_endpoint_do_agente(agente):
    with urllib.request.urlopen(f"{agente.base}/.well-known/agent-card.json") as r:
        card = json.loads(r.read())
    interface = card["supportedInterfaces"][0]
    assert interface == {"url": f"{agente.base}/a2a", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    assert [s["id"] for s in card["skills"]] == ["reservar-sala"]


def test_um_unico_pedido_em_conflito_percorre_a_sequencia_inteira(a2a, agente, proxy):
    """A frase final do fluxo do avaliador: abertura da Task, chamada MCP, input_required,
    pausa, retomada com id novo e reserva criada."""
    pausa = a2a.enviar(PEDIDO_CONFLITO)
    task_id = task_de(pausa)["id"]
    fim = a2a.enviar("escolha=sala-fusca", task_id=task_id)
    assert estado_de(fim) == "TASK_STATE_COMPLETED"

    transicoes = [l.split("estado=", 1)[1] for l in agente.stderr.splitlines() if f"task={task_id} estado=" in l]
    assert transicoes == [
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_SUBMITTED -> TASK_STATE_WORKING",
        "TASK_STATE_WORKING -> TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_INPUT_REQUIRED -> TASK_STATE_WORKING",
        "TASK_STATE_WORKING -> TASK_STATE_COMPLETED",
    ]
    chamadas = [t for t in proxy.jsonrpc() if t["corpo"]["method"] == "tools/call"]
    assert chamadas[0]["resposta"]["result"]["resultType"] == "input_required"
    assert chamadas[1]["resposta"]["result"]["resultType"] == "complete"
    assert chamadas[0]["corpo"]["id"] != chamadas[1]["corpo"]["id"]


def test_metodo_desconhecido_e_corpo_invalido_sao_recusados(a2a, agente):
    assert a2a.rpc("CancelTask", {"id": "x"})["error"]["code"] == -32601
    req = urllib.request.Request(f"{agente.base}/a2a", data=b"nao-e-json",
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as r:
        assert json.loads(r.read())["error"]["code"] == -32700


def test_escolha_fora_do_enum_com_alternativa_unica_e_texto_fora_do_formato(a2a):
    # A garagem das 16h esta livre e e reservada direto; a fusca das 16h (res-0002)
    # passa a ter uma unica alternativa: o mirante.
    livre = a2a.enviar(f"reservar sala=sala-garagem inicio={h('16:00')} fim={h('17:00')} responsavel=Lorraine")
    assert estado_de(livre) == "TASK_STATE_COMPLETED"
    unica = a2a.enviar(f"reservar sala=sala-fusca inicio={h('16:00')} fim={h('17:00')} responsavel=Biff")
    assert texto_de(unica) == "alternativas: sala-mirante"
    for texto in ("escolha=sala-garagem", "quero a garagem", "escolha="):
        de_novo = a2a.enviar(texto, task_id=task_de(unica)["id"])
        assert estado_de(de_novo) == "TASK_STATE_INPUT_REQUIRED"
        assert texto_de(de_novo) == "alternativas: sala-mirante"


def test_servidor_mcp_fora_do_ar_na_primeira_chamada_falha_a_task(a2a, servidor_mcp):
    servidor_mcp.parar()
    resposta = a2a.enviar(PEDIDO_LIVRE)
    assert estado_de(resposta) == "TASK_STATE_FAILED"
    assert "servidor MCP" in texto_de(resposta)
