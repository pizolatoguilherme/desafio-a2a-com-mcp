"""O agente como host MCP: um cliente do SDK oficial falando HTTP com o servidor.

O cliente fica vivo entre chamadas (isso e processo, nao sessao de protocolo):
cada request leva o proprio _meta com versao, capabilities e traceparent.

O ponto central aqui e `allow_input_required=True`. Sem ele, o SDK resolveria o
input_required sozinho (via callback de elicitation) e a Task A2A nunca pausaria.
Com ele, o InputRequiredResult volta cru para o agente, que decide o que fazer.

O cliente e um so durante a vida do processo, e isso importa: e o contador dele
que garante que o retry saia com um id JSON-RPC diferente do request inicial.
Por isso ele nao pode morrer quando o servidor MCP cai (ver TransporteResiliente).
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from mcp_types import (
    CallToolResult,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitRequestParams,
    ElicitResult,
    ErrorData,
    Implementation,
    InputRequiredResult,
    PaginatedRequestParams,
)

PROTOCOLO = "2026-07-28"
CLIENTE = Implementation(name="agente-central-de-salas", version="1.0.0")
CONEXAO_ENCERRADA = -32000  # CONNECTION_CLOSED do SDK
ERRO_DE_TRANSPORTE = -32000


class FalhaDeTransporte(Exception):
    """O servidor MCP nao chegou a responder: nada foi decidido do lado dele."""


class TransporteResiliente(httpx2.AsyncBaseTransport):
    """Transporte HTTP que nao deixa uma queda de conexao derrubar o cliente MCP.

    No SDK 2.3.0, um `ConnectError` dentro do transporte Streamable HTTP derruba o
    grupo de tarefas do `Client`, e ele fica morto ("Connection closed") mesmo
    depois que o servidor volta. Aqui a falha de rede vira uma resposta HTTP 503
    com um erro JSON-RPC marcado como de transporte, que o SDK entrega como
    `MCPError` comum. O cliente sobrevive e o contador de ids segue de onde estava.
    """

    def __init__(self) -> None:
        self._real = httpx2.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
        try:
            return await self._real.handle_async_request(request)
        except httpx2.TransportError as e:
            erro = {
                "code": ERRO_DE_TRANSPORTE,
                "message": f"Servidor MCP inacessivel ({type(e).__name__})",
                "data": {"transporte": True},
            }
            corpo = json.dumps({"jsonrpc": "2.0", "id": None, "error": erro}).encode()
            return httpx2.Response(503, headers={"content-type": "application/json"}, content=corpo,
                                   request=request)

    async def aclose(self) -> None:
        await self._real.aclose()


def _eh_transporte(e: MCPError) -> bool:
    data = e.error.data
    return e.error.code == CONEXAO_ENCERRADA or (isinstance(data, dict) and data.get("transporte") is True)


def traceparent(trace_id: str) -> str:
    """Mesmo trace-id da chamada A2A, span-id novo para cada request MCP."""
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


# --- as tres formas que um tools/call pode tomar, do ponto de vista do agente ---


@dataclass(frozen=True)
class Completo:
    estruturado: dict[str, Any]
    texto: str
    erro: bool


@dataclass(frozen=True)
class PrecisaInput:
    chave: str  # chave atribuida pelo servidor no inputRequests, devolvida igual no inputResponses
    campo: str  # a propriedade unica do requestedSchema (hoje, "sala")
    opcoes: list[str]  # o enum (ou const) da elicitation, na ordem do servidor
    request_state: str  # opaco: guardado e ecoado, nunca aberto


@dataclass(frozen=True)
class ErroProtocolo:
    codigo: int
    mensagem: str


Resultado = Completo | PrecisaInput | ErroProtocolo


async def _nunca_responder(_ctx: Any, _params: ElicitRequestParams) -> ElicitResult | ErrorData:
    """Callback que so existe para o SDK declarar a capability de elicitation.

    Ele nunca roda: todas as chamadas usam allow_input_required=True, entao quem
    responde a elicitation e o cliente A2A, nao este processo.
    """
    return ErrorData(code=-32603, message="o agente nao responde elicitation sozinho")


class HostMCP:
    def __init__(self, url: str) -> None:
        self._http = httpx2.AsyncClient(
            transport=TransporteResiliente(),
            timeout=httpx2.Timeout(30.0, read=300.0),  # os mesmos padroes do SDK
        )
        self._client = Client(
            streamable_http_client(url, http_client=self._http),
            mode=PROTOCOLO,  # versao fixada: nada de inferir de um request anterior
            client_info=CLIENTE,
            elicitation_callback=_nunca_responder,
            cache=None,  # sem cache: tools/list e resources/read sempre chegam ao servidor
        )

    async def abrir(self) -> None:
        await self._client.__aenter__()

    async def fechar(self) -> None:
        try:
            await self._client.__aexit__(None, None, None)
        finally:
            await self._http.aclose()

    async def descobrir_tools(self, trace_id: str) -> set[str]:
        params = PaginatedRequestParams(_meta={"traceparent": traceparent(trace_id)})
        try:
            resultado = await self._client.session.list_tools(params=params)
        except MCPError as e:
            raise (FalhaDeTransporte(e.error.message) if _eh_transporte(e) else e) from None
        return {t.name for t in resultado.tools}

    async def versao_da_politica(self, trace_id: str) -> str:
        try:
            resultado = await self._client.session.read_resource(
                "politica://uso", meta={"traceparent": traceparent(trace_id)}
            )
        except MCPError as e:
            raise (FalhaDeTransporte(e.error.message) if _eh_transporte(e) else e) from None
        texto = next((c.text for c in resultado.contents if getattr(c, "text", None)), "")
        primeira = texto.splitlines()[0] if texto else ""
        chave, _, valor = primeira.partition(":")
        if chave.strip() != "versao" or not valor.strip():
            raise ValueError("o resource politica://uso nao declara a versao na primeira linha")
        return valor.strip()

    async def chamar_tool(
        self,
        nome: str,
        argumentos: dict[str, Any],
        trace_id: str,
        input_responses: dict[str, Any] | None = None,
        request_state: str | None = None,
    ) -> Resultado:
        """Um tools/call. No retry, o SDK gera um id JSON-RPC novo a cada chamada.

        Erro de protocolo vira `ErroProtocolo` (o servidor decidiu); queda de rede
        levanta `FalhaDeTransporte` (o servidor nao chegou a decidir nada).
        """
        try:
            resultado = await self._client.session.call_tool(
                nome,
                argumentos,
                input_responses=input_responses,
                request_state=request_state,
                meta={"traceparent": traceparent(trace_id)},
                allow_input_required=True,
            )
        except MCPError as e:
            if _eh_transporte(e):
                raise FalhaDeTransporte(e.error.message) from None
            return ErroProtocolo(e.error.code, e.error.message)
        if isinstance(resultado, InputRequiredResult):
            return _precisa_input(resultado)
        assert isinstance(resultado, CallToolResult)
        texto = " ".join(getattr(b, "text", "") for b in resultado.content).strip()
        return Completo(resultado.structured_content or {}, texto, bool(resultado.is_error))


def _precisa_input(resultado: InputRequiredResult) -> Resultado:
    pedidos = resultado.input_requests or {}
    if len(pedidos) != 1 or not resultado.request_state:
        return ErroProtocolo(-32603, "input_required inesperado: esperava uma unica elicitation e um requestState")
    chave, pedido = next(iter(pedidos.items()))
    if not isinstance(pedido, ElicitRequest) or not isinstance(pedido.params, ElicitRequestFormParams):
        return ErroProtocolo(-32603, "input_required inesperado: so elicitation em form mode e suportada")
    propriedades = pedido.params.requested_schema.get("properties") or {}
    if len(propriedades) != 1:
        return ErroProtocolo(-32603, "input_required inesperado: o schema da elicitation deveria ter um campo")
    campo, schema = next(iter(propriedades.items()))
    opcoes = list(schema.get("enum") or ([schema["const"]] if "const" in schema else []))
    return PrecisaInput(chave, campo, [str(o) for o in opcoes], resultado.request_state)
