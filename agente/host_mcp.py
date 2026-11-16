"""O agente como host MCP: um cliente do SDK oficial falando HTTP com o servidor.

O cliente fica vivo entre chamadas (isso e processo, nao sessao de protocolo):
cada request leva o proprio _meta com versao, capabilities e traceparent.

O ponto central aqui e `allow_input_required=True`. Sem ele, o SDK resolveria o
input_required sozinho (via callback de elicitation) e a Task A2A nunca pausaria.
Com ele, o InputRequiredResult volta cru para o agente, que decide o que fazer.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from typing import Any

from mcp import Client
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
        self._client = Client(
            url,
            mode=PROTOCOLO,  # versao fixada: nada de inferir de um request anterior
            client_info=CLIENTE,
            elicitation_callback=_nunca_responder,
            cache=None,  # sem cache: tools/list e resources/read sempre chegam ao servidor
        )

    async def abrir(self) -> None:
        await self._client.__aenter__()

    async def fechar(self) -> None:
        await self._client.__aexit__(None, None, None)

    async def descobrir_tools(self, trace_id: str) -> set[str]:
        params = PaginatedRequestParams(_meta={"traceparent": traceparent(trace_id)})
        resultado = await self._client.session.list_tools(params=params)
        return {t.name for t in resultado.tools}

    async def versao_da_politica(self, trace_id: str) -> str:
        resultado = await self._client.session.read_resource(
            "politica://uso", meta={"traceparent": traceparent(trace_id)}
        )
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
        """Um tools/call. No retry, o SDK gera um id JSON-RPC novo a cada chamada."""
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
