"""Borda HTTP: este servidor so fala a revisao 2026-07-28, sem sessao.

O SDK 2.3.0 escolhe a "era" do protocolo pelo header MCP-Protocol-Version, e so
por ele (mcp/server/streamable_http_manager.py: "header-only era-routing for
now"). Sem o header, ou com uma versao de handshake (2024/2025), o request cai
no caminho legado, que nao exige o envelope de _meta: um tools/list sem
protocolVersion nem clientCapabilities seria atendido com 200.

O enunciado manda recusar qualquer request sem os campos obrigatorios de _meta,
sem inferir nada. Entao, quando o header nao nomeia uma revisao moderna, este
middleware responde o que o caminho moderno do SDK responderia:

- _meta incompleto                  -> -32602, HTTP 400
- _meta completo, header divergente -> -32020, HTTP 400

Requests com header moderno passam intactos: o SDK faz toda a validacao.
"""

from __future__ import annotations

import json

from mcp_types import HEADER_MISMATCH, INVALID_PARAMS, INVALID_REQUEST, PARSE_ERROR
from mcp_types.version import HANDSHAKE_PROTOCOL_VERSIONS

CHAVES_DO_ENVELOPE = ("io.modelcontextprotocol/protocolVersion", "io.modelcontextprotocol/clientCapabilities")
HEADER_DE_VERSAO = b"mcp-protocol-version"


class ApenasEra2026:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        versao = next((v.decode("latin-1") for k, v in scope["headers"] if k == HEADER_DE_VERSAO), None)
        if versao is not None and versao not in HANDSHAKE_PROTOCOL_VERSIONS:
            await self.app(scope, receive, send)
            return

        corpo = b""
        while True:
            mensagem = await receive()
            corpo += mensagem.get("body", b"")
            if not mensagem.get("more_body"):
                break
        await _recusar(send, *_classificar(corpo))


def _classificar(corpo: bytes) -> tuple[object, int, str]:
    try:
        mensagem = json.loads(corpo)
    except ValueError:
        return None, PARSE_ERROR, "Parse error"
    if not isinstance(mensagem, dict):
        return None, INVALID_REQUEST, "Invalid request: a single JSON-RPC request object is required"
    id_ = mensagem.get("id")
    params = mensagem.get("params")
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or any(chave not in meta for chave in CHAVES_DO_ENVELOPE):
        return id_, INVALID_PARAMS, (
            "params._meta must be an object carrying the required "
            f"'{CHAVES_DO_ENVELOPE[0]}' and '{CHAVES_DO_ENVELOPE[1]}' envelope keys"
        )
    return id_, HEADER_MISMATCH, "mcp-protocol-version header does not match the request envelope's protocol version"


async def _recusar(send, id_: object, codigo: int, mensagem: str) -> None:
    corpo = json.dumps({"jsonrpc": "2.0", "id": id_, "error": {"code": codigo, "message": mensagem}}).encode()
    await send({"type": "http.response.start", "status": 400,
                "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(corpo)).encode())]})
    await send({"type": "http.response.body", "body": corpo})
