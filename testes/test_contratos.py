"""Contratos: as respostas tem a mesma forma dos exemplos de exemplos/wire/.

Cada teste envia o request do exemplo a um processo recem-iniciado e compara a
forma da resposta com a do exemplo: chaves de cada objeto, recursivamente, e o
tipo de cada valor. Onde o estado de um processo novo e o mesmo da captura,
compara tambem os valores. Ficam de fora so os valores que variam por natureza:
ids, requestState, a chave do inputRequests (cada SDK gera a sua) e o numero da
reserva.
"""

from __future__ import annotations

import json
import urllib.request
from typing import Any

import pytest

from conftest import RAIZ, post


def exemplo(nome: str) -> dict:
    return json.loads((RAIZ / "exemplos" / "wire" / nome).read_text(encoding="utf-8"))


def forma(valor: Any) -> Any:
    """Esqueleto de um JSON: chaves e tipos, sem os valores."""
    if isinstance(valor, dict):
        return {k: forma(v) for k, v in valor.items()}
    if isinstance(valor, list):
        return [forma(valor[0])] if valor else []
    return type(valor).__name__


def sem_chave_do_input_request(resultado: dict) -> dict:
    """Troca a chave atribuida pelo servidor por um marcador fixo."""
    copia = json.loads(json.dumps(resultado))
    if "inputRequests" in copia:
        copia["inputRequests"] = {"<chave>": next(iter(copia["inputRequests"].values()))}
    return copia


def enviar_mcp(url: str, nome: str, id_: int = 1, ajuste=None) -> tuple[int, dict, dict]:
    ex = exemplo(nome)
    corpo = json.loads(json.dumps(ex["request"]["body"]))
    corpo["id"] = id_
    if ajuste:
        ajuste(corpo)
    status, resposta = post(url, corpo, ex["request"]["headers"])
    return status, resposta, ex["response"]


# --- MCP ------------------------------------------------------------------------


def test_01_tools_list(servidor_mcp):
    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "01-tools-list.json")
    assert status == esperado["httpStatus"]
    nossas = {t["name"]: t for t in resposta["result"]["tools"]}
    deles = {t["name"]: t for t in esperado["body"]["result"]["tools"]}
    assert set(nossas) == set(deles)
    for nome, tool in deles.items():
        assert nossas[nome]["inputSchema"]["type"] == "object"
        assert nossas[nome]["inputSchema"]["properties"] == tool["inputSchema"]["properties"]
        assert nossas[nome]["inputSchema"].get("required") == tool["inputSchema"].get("required")
        assert nossas[nome]["outputSchema"]["properties"].keys() == tool["outputSchema"]["properties"].keys()
    assert set(resposta["result"]) >= {"resultType", "tools"}


def test_02_reserva_livre(servidor_mcp):
    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "02-tools-call-livre.json")
    assert status == esperado["httpStatus"]
    assert forma(resposta["result"]) == forma(esperado["body"]["result"])
    assert resposta["result"]["structuredContent"] == esperado["body"]["result"]["structuredContent"]
    assert json.loads(resposta["result"]["content"][0]["text"]) == resposta["result"]["structuredContent"]


def test_03_conflito_e_04_retry(servidor_mcp):
    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "03-tools-call-conflito-input-required.json")
    assert status == esperado["httpStatus"]
    nosso, deles = sem_chave_do_input_request(resposta["result"]), sem_chave_do_input_request(esperado["body"]["result"])
    assert forma(nosso) == forma(deles)
    # num processo novo as alternativas sao as mesmas da captura: o pedido de elicitation e identico
    assert nosso["inputRequests"]["<chave>"] == deles["inputRequests"]["<chave>"]

    chave, estado = next(iter(resposta["result"]["inputRequests"])), resposta["result"]["requestState"]

    def retry(corpo: dict) -> None:
        corpo["params"]["inputResponses"] = {chave: {"action": "accept", "content": {"sala": "sala-fusca"}}}
        corpo["params"]["requestState"] = estado

    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "04-tools-call-retry.json", 2, retry)
    assert status == esperado["httpStatus"]
    assert forma(resposta["result"]) == forma(esperado["body"]["result"])
    nosso_dado = dict(resposta["result"]["structuredContent"], reserva=None)
    deles_dado = dict(esperado["body"]["result"]["structuredContent"], reserva=None)
    assert nosso_dado == deles_dado


def test_05_resource_politica(servidor_mcp):
    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "05-resources-read-politica.json")
    assert status == esperado["httpStatus"]
    assert resposta["result"]["contents"] == esperado["body"]["result"]["contents"]


def test_06_sem_elicitation(servidor_mcp):
    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "06-erro-32021-sem-elicitation.json")
    assert status == esperado["httpStatus"] == 400
    assert resposta["error"]["code"] == esperado["body"]["error"]["code"]
    assert resposta["error"]["data"] == esperado["body"]["error"]["data"]


def test_11_retry_com_recusa(servidor_mcp):
    def conflito(corpo: dict) -> None:
        corpo["params"]["arguments"] = exemplo("11-tools-call-retry-recusa.json")["request"]["body"]["params"]["arguments"]

    _, resposta, _ = enviar_mcp(servidor_mcp.url, "03-tools-call-conflito-input-required.json", 1, conflito)
    chave, estado = next(iter(resposta["result"]["inputRequests"])), resposta["result"]["requestState"]

    def recusa(corpo: dict) -> None:
        corpo["params"]["inputResponses"] = {chave: {"action": "decline"}}
        corpo["params"]["requestState"] = estado

    status, resposta, esperado = enviar_mcp(servidor_mcp.url, "11-tools-call-retry-recusa.json", 2, recusa)
    assert status == esperado["httpStatus"]
    assert forma(resposta["result"]) == forma(esperado["body"]["result"])
    assert resposta["result"]["structuredContent"] == esperado["body"]["result"]["structuredContent"]


# --- A2A ------------------------------------------------------------------------


def test_07_agent_card(agente):
    with urllib.request.urlopen(f"{agente.base}/.well-known/agent-card.json") as r:
        card = json.loads(r.read())
    esperado = exemplo("07-a2a-agent-card.json")["response"]["body"]
    esperado["supportedInterfaces"][0]["url"] = f"{agente.base}/a2a"
    assert card == esperado


def _a2a(agente, corpo: dict, headers: dict) -> dict:
    return post(f"{agente.base}/a2a", corpo, headers)[1]


def test_08_09_10_pausa_get_task_e_continuacao(agente):
    ex8, ex9, ex10 = (exemplo(n) for n in ("08-a2a-send-message.json", "09-a2a-get-task-input-required.json",
                                             "10-a2a-send-message-continuacao.json"))
    r8 = _a2a(agente, ex8["request"]["body"], ex8["request"]["headers"])
    assert forma(r8) == forma(ex8["response"]["body"])
    task = r8["result"]["task"]
    # num processo novo, a pausa e a do enunciado (a captura foi feita depois do validador)
    assert task["status"]["message"]["parts"][0]["text"] == "alternativas: sala-fusca, sala-mirante"
    assert task["history"][0] == ex8["request"]["body"]["params"]["message"]

    corpo9 = json.loads(json.dumps(ex9["request"]["body"]))
    corpo9["params"]["id"] = task["id"]
    r9 = _a2a(agente, corpo9, ex9["request"]["headers"])
    assert forma(r9) == forma(ex9["response"]["body"])
    assert r9["result"]["task"] == task

    corpo10 = json.loads(json.dumps(ex10["request"]["body"]))
    corpo10["params"]["message"]["taskId"] = task["id"]
    r10 = _a2a(agente, corpo10, ex10["request"]["headers"])
    assert forma(r10) == forma(ex10["response"]["body"])
    final = r10["result"]["task"]
    deles = ex10["response"]["body"]["result"]["task"]
    assert final["status"]["state"] == deles["status"]["state"] == "TASK_STATE_COMPLETED"
    assert [m["role"] for m in final["history"]] == [m["role"] for m in deles["history"]]
    nosso_artifact = json.loads(final["artifacts"][0]["parts"][0]["text"])
    deles_artifact = json.loads(deles["artifacts"][0]["parts"][0]["text"])
    assert final["artifacts"][0]["name"] == "reserva"
    assert dict(nosso_artifact, reserva=None) == dict(deles_artifact, reserva=None)
