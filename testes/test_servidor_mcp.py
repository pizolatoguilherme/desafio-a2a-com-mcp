"""Fronteira MCP: sobe o servidor de verdade e cobre o que o validador nao exercita.

Subida com chave e TTL invalidos, expiracao do requestState, retry depois de
reiniciar o processo, resposta fora do enum e o registro no stderr.
"""

from __future__ import annotations

import json
import secrets
import time

import pytest

from conftest import CAP_FORM, h, porta_livre, rodar_ate_sair
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
