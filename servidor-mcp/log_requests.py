"""Middleware ASGI que registra cada request MCP no stderr.

Fica fora do SDK de proposito: assim ate os requests que o SDK recusa (sem
_meta, capability faltando, requestState adulterado) aparecem no log com
metodo, id e traceparent. O corpo e lido uma vez e repassado intacto.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime


class RegistroDeRequests:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        partes: list[bytes] = []
        while True:
            mensagem = await receive()
            partes.append(mensagem.get("body", b""))
            if not mensagem.get("more_body"):
                break
        corpo = b"".join(partes)

        status: dict[str, int] = {}

        async def send_com_status(evento) -> None:
            if evento["type"] == "http.response.start":
                status["http"] = evento["status"]
            await send(evento)

        entregue = False

        async def receive_repetido():
            nonlocal entregue
            if not entregue:
                entregue = True
                return {"type": "http.request", "body": corpo, "more_body": False}
            return await receive()

        await self.app(scope, receive_repetido, send_com_status)
        _registrar(corpo, status.get("http"))


def _registrar(corpo: bytes, status: int | None) -> None:
    try:
        mensagem = json.loads(corpo)
    except ValueError:
        mensagem = {}
    if not isinstance(mensagem, dict):
        mensagem = {}
    params = mensagem.get("params") if isinstance(mensagem.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    alvo = params.get("name") or params.get("uri") or "-"
    retry = "sim" if "requestState" in params else "nao"
    capacidades = meta.get("io.modelcontextprotocol/clientCapabilities", "-")
    print(
        f"{datetime.now().isoformat(timespec='milliseconds')} mcp "
        f"method={mensagem.get('method', '-')} id={mensagem.get('id', '-')} alvo={alvo} "
        f"retry={retry} http={status} traceparent={meta.get('traceparent', '-')} "
        f"capabilities={json.dumps(capacidades, separators=(',', ':'))}",
        file=sys.stderr,
        flush=True,
    )
