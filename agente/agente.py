"""O agente como servidor A2A v1.0, binding JSON-RPC 2.0 sobre HTTP.

    GET  /.well-known/agent-card.json   Agent Card
    POST /a2a                           SendMessage, GetTask

Por dentro ele e host MCP (host_mcp.py), e a costura entre os dois lados mora
em ponte.py. Rode com `python agente/agente.py`.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import sys
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from card import agent_card
from host_mcp import HostMCP
from ponte import ErroA2A, Ponte
from tarefas import Tarefas

PORTA = int(os.environ.get("AGENT_PORT", "7300"))
URL_PUBLICA = os.environ.get("AGENT_PUBLIC_URL", f"http://127.0.0.1:{PORTA}")
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

host = HostMCP(MCP_URL)
tarefas = Tarefas()
ponte = Ponte(host, tarefas)


def _trace_id(request: Request) -> str:
    """trace-id do header traceparent do cliente A2A; sem header, comeca um trace novo."""
    casou = TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    if casou and casou.group(1) != "0" * 32:
        return casou.group(1)
    return secrets.token_hex(16)


def _erro(id_: Any, codigo: int, mensagem: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}})


def _texto(mensagem: dict) -> str:
    partes = mensagem.get("parts") or []
    return " ".join(p.get("text", "") for p in partes if isinstance(p, dict)).strip()


async def send_message(params: dict, request: Request) -> dict:
    mensagem = params.get("message")
    if not isinstance(mensagem, dict) or not isinstance(mensagem.get("parts"), list):
        raise ErroA2A(INVALID_PARAMS, "params.message com parts e obrigatorio")
    texto = _texto(mensagem)
    task_id = mensagem.get("taskId")
    if task_id:
        task = await ponte.continuar(mensagem, texto, task_id)
    else:
        task = await ponte.iniciar(mensagem, texto, _trace_id(request))
    return {"task": task.para_wire()}


async def get_task(params: dict, _request: Request) -> dict:
    task_id = params.get("id")
    task = tarefas.obter(task_id) if isinstance(task_id, str) else None
    if task is None:
        raise ErroA2A(-32001, f"Task nao encontrada: {task_id}")
    return {"task": task.para_wire()}


METODOS = {"SendMessage": send_message, "GetTask": get_task}


async def a2a(request: Request) -> JSONResponse:
    try:
        corpo = await request.json()
    except ValueError:
        return _erro(None, PARSE_ERROR, "JSON invalido")
    if not isinstance(corpo, dict) or corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return _erro(corpo.get("id") if isinstance(corpo, dict) else None, INVALID_REQUEST, "Request JSON-RPC invalido")
    id_ = corpo.get("id")
    metodo = corpo["method"]
    print(f"a2a method={metodo} id={id_} traceparent={request.headers.get('traceparent', '-')}",
          file=sys.stderr, flush=True)
    handler = METODOS.get(metodo)
    if handler is None:
        return _erro(id_, METHOD_NOT_FOUND, f"Metodo nao suportado: {metodo}")
    params = corpo.get("params")
    if not isinstance(params, dict):
        return _erro(id_, INVALID_PARAMS, "params deve ser um objeto")
    try:
        resultado = await handler(params, request)
    except ErroA2A as e:
        return _erro(id_, e.codigo, e.mensagem)
    except Exception as e:
        print(f"agente: erro interno: {e!r}", file=sys.stderr, flush=True)
        return _erro(id_, INTERNAL_ERROR, "Erro interno do agente")
    return JSONResponse({"jsonrpc": "2.0", "id": id_, "result": resultado})


async def card(_request: Request) -> JSONResponse:
    return JSONResponse(agent_card(URL_PUBLICA))


@contextlib.asynccontextmanager
async def ciclo_de_vida(_app: Starlette):
    await host.abrir()
    try:
        yield
    finally:
        await host.fechar()


app = Starlette(
    routes=[
        Route("/.well-known/agent-card.json", card, methods=["GET"]),
        Route("/a2a", a2a, methods=["POST"]),
    ],
    lifespan=ciclo_de_vida,
)


if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("AGENT_HOST", "127.0.0.1"), port=PORTA, log_level="warning")
