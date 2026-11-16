"""Fronteira MCP: sobe o servidor de verdade e cobre o que o validador nao exercita.

Subida com chave e TTL invalidos, expiracao do requestState, retry depois de
reiniciar o processo, resposta fora do enum e o registro no stderr.
"""

from __future__ import annotations

import json
import secrets
import time

import pytest

from conftest import CAP_FORM, h, porta_livre, post, rodar_ate_sair
from mcp.server.request_state import AESGCMRequestStateCodec

CONFLITO = {"sala": "sala-garagem", "inicio": h("14:00"), "fim": h("15:00"), "responsavel": "Marty"}


def _pausar(mcp) -> tuple[str, list[str], str]:
    """Faz o pedido em conflito e devolve (chave, enum, requestState)."""
    _, resposta = mcp.reservar(**CONFLITO)
    resultado = resposta["result"]
    assert resultado["resultType"] == "input_required"
    chave, pedido = next(iter(resultado["inputRequests"].items()))
    enum = pedido["params"]["requestedSchema"]["properties"]["sala"]["enum"]
    return chave, enum, resultado["requestState"]


# --- subida -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "trecho"),
    [
        ({"REQUEST_STATE_SECRET": ""}, "REQUEST_STATE_SECRET"),
        ({"REQUEST_STATE_SECRET": "curta"}, "32 bytes"),
        ({"REQUEST_STATE_TTL": "60"}, "fora da faixa"),
        ({"REQUEST_STATE_TTL": "3600"}, "fora da faixa"),
        ({"REQUEST_STATE_TTL": "dez"}, "invalido"),
    ],
)
def test_servidor_recusa_subir_sem_chave_ou_com_ttl_fora_da_faixa(env, trecho):
    porta = porta_livre()
    base = {"REQUEST_STATE_SECRET": secrets.token_hex(32), "MCP_PORT": str(porta)}
    saida = rodar_ate_sair("servidor-mcp/servidor.py", {**base, **env})
    assert saida.returncode != 0
    assert trecho in saida.stderr


def test_discover_declara_tools_e_resources(mcp):
    status, resposta = mcp.chamar("server/discover", {})
    assert status == 200
    capacidades = resposta["result"]["capabilities"]
    assert "tools" in capacidades and "resources" in capacidades
    # subscriptions esta fora do escopo: nada de anunciar o que o servidor nao entrega
    assert capacidades["resources"].get("subscribe") is False
    assert not capacidades["resources"].get("listChanged") and not capacidades["tools"].get("listChanged")


# --- so a revisao 2026-07-28, sem sessao (ver servidor-mcp/apenas_2026.py) ----------

ENVELOPE = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": CAP_FORM}


def _cru(url: str, corpo, cabecalhos: dict[str, str]) -> tuple[int, dict]:
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    return post(url, corpo, {**base, **cabecalhos})


@pytest.mark.parametrize(
    ("cabecalhos", "params", "codigo"),
    [
        ({}, {}, -32602),  # sem header de versao e sem _meta
        ({"MCP-Protocol-Version": "2025-11-25"}, {}, -32602),  # versao de handshake, sem _meta
        ({"MCP-Protocol-Version": "2025-11-25", "Mcp-Method": "tools/list"}, {"_meta": ENVELOPE}, -32020),
        ({"Mcp-Method": "tools/list"}, {"_meta": ENVELOPE}, -32020),  # _meta completo, header ausente
        ({"MCP-Protocol-Version": "2099-01-01", "Mcp-Method": "tools/list"}, {"_meta": ENVELOPE}, -32020),
    ],
)
def test_request_fora_da_revisao_2026_e_recusado(servidor_mcp, cabecalhos, params, codigo):
    status, resposta = _cru(servidor_mcp.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": params},
                            cabecalhos)
    assert status == 400 and resposta["error"]["code"] == codigo


def test_initialize_legado_nao_abre_sessao(servidor_mcp):
    corpo = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "x", "version": "1"}}}
    status, resposta = _cru(servidor_mcp.url, corpo, {"MCP-Protocol-Version": "2025-11-25"})
    assert status == 400 and "result" not in resposta


@pytest.mark.parametrize(
    ("metodo", "params", "cabecalhos"),
    [
        ("tools/call", {"name": "listar_salas", "arguments": {}}, {"Mcp-Method": "tools/list", "Mcp-Name": "listar_salas"}),
        ("tools/call", {"name": "listar_salas", "arguments": {}}, {"Mcp-Method": "tools/call", "Mcp-Name": "reservar_sala"}),
        ("tools/call", {"name": "listar_salas", "arguments": {}}, {"Mcp-Method": "tools/call"}),
        ("resources/read", {"uri": "politica://uso"}, {"Mcp-Method": "resources/read", "Mcp-Name": "politica://outra"}),
    ],
)
def test_header_que_nao_bate_com_o_corpo_e_recusado_com_32020(servidor_mcp, metodo, params, cabecalhos):
    corpo = {"jsonrpc": "2.0", "id": 1, "method": metodo, "params": {**params, "_meta": ENVELOPE}}
    status, resposta = _cru(servidor_mcp.url, corpo, {"MCP-Protocol-Version": "2026-07-28", **cabecalhos})
    assert status == 400 and resposta["error"]["code"] == -32020


def test_stderr_registra_metodo_id_e_traceparent(servidor_mcp, mcp):
    traceparent = f"00-{secrets.token_hex(16)}-{secrets.token_hex(8)}-01"
    mcp.chamar("tools/list", {}, traceparent=traceparent)
    time.sleep(0.2)
    linha = next(l for l in servidor_mcp.stderr.splitlines() if traceparent in l)
    assert "method=tools/list" in linha and "id=" in linha


def test_stderr_registra_tambem_o_request_recusado(servidor_mcp, mcp):
    status, _ = mcp.reservar(**CONFLITO, capabilities={})
    assert status == 400
    time.sleep(0.2)
    assert "http=400" in servidor_mcp.stderr


# --- MRTR: o que o validador nao cobre ------------------------------------------


def test_request_state_expirado_e_recusado_sem_reservar(segredo, mcp):
    chave, _, estado = _pausar(mcp)
    # Reabre o token com a mesma chave do servidor, empurra a expiracao para o
    # passado e sela de novo: um token autentico, so que vencido.
    codec = AESGCMRequestStateCodec([segredo])
    claims = json.loads(codec.unseal(estado))
    agora = time.time()
    claims["iat"], claims["exp"] = agora - 700, agora - 100
    vencido = codec.seal(json.dumps(claims, separators=(",", ":")).encode())

    _, resposta = mcp.retomar(CONFLITO, chave, {"action": "accept", "content": {"sala": "sala-fusca"}}, vencido)
    assert resposta["error"]["code"] == -32602
    assert mcp.livre("sala-fusca", h("14:00"), h("15:00"))


def test_request_state_de_outra_chave_e_recusado(mcp):
    chave, _, estado = _pausar(mcp)
    outra = AESGCMRequestStateCodec([secrets.token_hex(32)])
    forjado = outra.seal(b'{"v":1}')
    _, resposta = mcp.retomar(CONFLITO, chave, {"action": "accept", "content": {"sala": "sala-fusca"}}, forjado)
    assert resposta["error"]["code"] == -32602


def test_retry_conclui_depois_de_reiniciar_o_servidor(servidor_mcp, mcp):
    chave, enum, estado = _pausar(mcp)
    servidor_mcp.reiniciar()  # mesma chave, memoria zerada
    _, resposta = mcp.retomar(CONFLITO, chave, {"action": "accept", "content": {"sala": enum[-1]}}, estado)
    resultado = resposta["result"]
    assert resultado["resultType"] == "complete" and not resultado.get("isError")
    assert resultado["structuredContent"]["sala"] == "sala-mirante"
    assert resultado["structuredContent"]["politica"] == "2026-11-01"


def test_retry_apos_restart_conclui_mesmo_que_as_alternativas_tenham_mudado(servidor_mcp, mcp):
    """O cenario do fluxo do avaliador (passos 8, 11 e 12).

    Com o mirante ocupado, o estado selado oferece so a fusca. O restart zera as
    reservas e o mirante volta a ficar livre, entao a pergunta, se recalculada, seria
    outra. O retry tem que concluir pelo que foi selado, e nao perguntar de novo.
    """
    mcp.reservar("sala-mirante", h("14:00"), h("15:00"))
    chave, enum, estado = _pausar(mcp)
    assert enum == ["sala-fusca"]
    servidor_mcp.reiniciar()
    _, resposta = mcp.retomar(CONFLITO, chave, {"action": "accept", "content": {"sala": "sala-fusca"}}, estado)
    resultado = resposta["result"]
    assert resultado["resultType"] == "complete" and not resultado.get("isError")
    assert resultado["structuredContent"]["sala"] == "sala-fusca"


def test_sala_escolhida_ocupada_entre_as_rodadas_pergunta_de_novo(mcp):
    chave, enum, estado = _pausar(mcp)
    assert enum == ["sala-fusca", "sala-mirante"]
    mcp.reservar("sala-fusca", h("14:00"), h("15:00"), "Outra pessoa")
    _, resposta = mcp.retomar(CONFLITO, chave, {"action": "accept", "content": {"sala": "sala-fusca"}}, estado)
    resultado = resposta["result"]
    assert resultado["resultType"] == "input_required"
    novo = next(iter(resultado["inputRequests"].values()))
    assert novo["params"]["requestedSchema"]["properties"]["sala"]["enum"] == ["sala-mirante"]
    assert resultado["requestState"] != estado


@pytest.mark.parametrize("sala", ["sala-delorean", "sala-aquario", "sala-garagem"])
def test_resposta_fora_do_enum_nao_reserva(mcp, sala):
    chave, enum, estado = _pausar(mcp)
    assert sala not in enum
    _, resposta = mcp.retomar(CONFLITO, chave, {"action": "accept", "content": {"sala": sala}}, estado)
    assert resposta["result"]["isError"] is True
    if sala != "sala-delorean":
        assert mcp.livre(sala, h("14:00"), h("15:00")) is (sala != "sala-garagem")


@pytest.mark.parametrize("acao", ["decline", "cancel"])
def test_recusa_e_cancelamento_concluem_sem_reservar(mcp, acao):
    chave, enum, estado = _pausar(mcp)
    _, resposta = mcp.retomar(CONFLITO, chave, {"action": acao}, estado)
    resultado = resposta["result"]
    assert resultado["resultType"] == "complete" and not resultado.get("isError")
    assert resultado["structuredContent"]["reservado"] is False
    assert resultado["structuredContent"]["motivo"]
    assert all(mcp.livre(s, h("14:00"), h("15:00")) for s in enum)


def test_uma_unica_alternativa_vira_enum_de_um_item(mcp):
    mcp.reservar("sala-fusca", h("14:00"), h("15:00"))  # sobra so o mirante para a garagem as 14h
    _, resposta = mcp.reservar("sala-garagem", h("14:00"), h("15:00"))
    pedido = next(iter(resposta["result"]["inputRequests"].values()))
    campo = pedido["params"]["requestedSchema"]["properties"]["sala"]
    assert (campo.get("enum") or [campo.get("const")]) == ["sala-mirante"]


def test_sala_livre_nao_pergunta_nada(mcp):
    _, resposta = mcp.reservar("sala-aquario", h("08:00"), h("09:00"))
    resultado = resposta["result"]
    assert resultado["resultType"] == "complete"
    assert "requestState" not in resultado and "inputRequests" not in resultado


def test_consultar_aplica_as_mesmas_regras_e_lista_conflitos(mcp):
    _, resposta = mcp.tool("consultar_disponibilidade", {"sala": "sala-garagem", "inicio": h("14:30"),
                                                         "fim": h("15:30")})
    dados = resposta["result"]["structuredContent"]
    assert dados["livre"] is False and [c["id"] for c in dados["conflitos"]] == ["res-0001"]


def test_capability_e_lida_a_cada_request(mcp):
    """Nada de sessao: declarar a capability num request nao vale para o seguinte."""
    _, primeira = mcp.reservar(**CONFLITO, capabilities=CAP_FORM)
    assert primeira["result"]["resultType"] == "input_required"
    status, segunda = mcp.reservar(**CONFLITO, capabilities={})
    assert status == 400 and segunda["error"]["code"] == -32021


# --- decisoes de produto fechadas e persistencia ----------------------------------


def test_reservar_no_passado_e_permitido(mcp):
    _, resposta = mcp.reservar("sala-aquario", "2020-01-06T09:00:00-03:00", "2020-01-06T10:00:00-03:00")
    resultado = resposta["result"]
    assert resultado["resultType"] == "complete" and not resultado.get("isError")
    assert resultado["structuredContent"]["reservado"] is True


def test_reserva_criada_aparece_nas_consultas_seguintes(mcp):
    assert mcp.livre("sala-porao", h("11:00"), h("12:00"))
    _, resposta = mcp.reservar("sala-porao", h("11:00"), h("12:00"), "Doc")
    id_ = resposta["result"]["structuredContent"]["reserva"]
    _, consulta = mcp.tool("consultar_disponibilidade", {"sala": "sala-porao", "inicio": h("11:30"), "fim": h("12:30")})
    dados = consulta["result"]["structuredContent"]
    assert dados["livre"] is False and [c["id"] for c in dados["conflitos"]] == [id_]


def test_duracao_de_uma_hora_e_cinquenta_e_nove_e_aceita(mcp):
    _, resposta = mcp.tool("consultar_disponibilidade", {"sala": "sala-aquario", "inicio": "2026-11-03T09:00:00-03:00",
                                                         "fim": "2026-11-03T10:59:00-03:00"})
    assert not resposta["result"].get("isError")


def test_ttl_padrao_e_de_dez_minutos(segredo, mcp):
    _, _, estado = _pausar(mcp)
    claims = json.loads(AESGCMRequestStateCodec([segredo]).unseal(estado))
    assert round(claims["exp"] - claims["iat"]) == 600


def test_atende_na_7301_por_padrao(segredo):
    import socket

    from conftest import Processo

    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", 7301)) == 0:
            pytest.skip("a porta 7301 ja esta ocupada")
    env = {"REQUEST_STATE_SECRET": segredo}
    env.pop("MCP_PORT", None)
    processo = Processo("servidor-mcp/servidor.py", 7301, env)
    processo.env.pop("MCP_PORT", None)
    processo.subir()
    try:
        from conftest import ClienteMCP

        status, resposta = ClienteMCP("http://127.0.0.1:7301/mcp").chamar("tools/list", {})
        assert status == 200 and len(resposta["result"]["tools"]) == 3
    finally:
        processo.parar()
