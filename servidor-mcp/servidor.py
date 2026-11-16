"""Servidor MCP da Central de Salas, em Streamable HTTP stateless.

Tres tools, um resource e o ciclo de MRTR na reserva. O MRTR nao e um callback:
quando a sala pedida esta ocupada, `reservar_sala` devolve um InputRequiredResult
com a elicitation e um requestState que carrega o pedido e as alternativas
oferecidas. O RequestStateBoundary do SDK sela esse estado (AES-256-GCM com a
chave de REQUEST_STATE_SECRET) e o verifica no retry. Nada fica guardado aqui
entre as duas rodadas, entao o retry funciona mesmo depois de um restart.

Rode com `python servidor-mcp/servidor.py`.
"""

from __future__ import annotations

import json
import os
import sys

import uvicorn
from mcp_types import (
    INVALID_PARAMS,
    MISSING_REQUIRED_CLIENT_CAPABILITY,
    ElicitRequest,
    ElicitRequestFormParams,
    ElicitResult,
    InputRequiredResult,
)
from pydantic import BaseModel

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.request_state import RequestStateSecurity
from mcp.shared.exceptions import MCPError

import dominio
from log_requests import RegistroDeRequests

NOME = "central-de-salas"
CHAVE_DA_ESCOLHA = "escolha_de_sala"  # chave do inputRequests; o cliente devolve a mesma
VERSAO_DO_ESTADO = 1
VALIDADE_PADRAO_SEGUNDOS = 600  # 10 minutos
VALIDADE_MINIMA_SEGUNDOS = 300  # o enunciado exige expiracao entre 5 e 30 minutos
VALIDADE_MAXIMA_SEGUNDOS = 1800
MENSAGEM_ELICITACAO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def _segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere uma com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return segredo


def _validade() -> int:
    """REQUEST_STATE_TTL em segundos, opcional, sempre dentro da faixa de 5 a 30 minutos."""
    bruto = os.environ.get("REQUEST_STATE_TTL", "").strip()
    if not bruto:
        return VALIDADE_PADRAO_SEGUNDOS
    try:
        ttl = int(bruto)
    except ValueError:
        sys.exit(f"REQUEST_STATE_TTL invalido: {bruto!r} nao e um numero inteiro de segundos")
    if not VALIDADE_MINIMA_SEGUNDOS <= ttl <= VALIDADE_MAXIMA_SEGUNDOS:
        sys.exit(
            f"REQUEST_STATE_TTL fora da faixa: {ttl}s. Use entre "
            f"{VALIDADE_MINIMA_SEGUNDOS} e {VALIDADE_MAXIMA_SEGUNDOS} segundos (5 a 30 minutos)"
        )
    return ttl


VALIDADE_DO_ESTADO_SEGUNDOS = _validade()

mcp = MCPServer(
    NOME,
    version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=VALIDADE_DO_ESTADO_SEGUNDOS),
)


# --- modelos de saida (geram outputSchema e structuredContent) ---------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


def _validar(sala: str, inicio: str, fim: str) -> dominio.Intervalo:
    try:
        return dominio.validar(sala, inicio, fim)
    except dominio.RegraVioladaError as e:
        raise ToolError(str(e)) from None


# --- tools --------------------------------------------------------------------


@mcp.tool(description="Lista todas as salas com capacidade e recursos.")
def listar_salas() -> ListaDeSalas:
    return ListaDeSalas(salas=[SalaOut(**s) for s in dominio.SALAS])


@mcp.tool(description="Diz se uma sala esta livre no intervalo, e quais reservas conflitam.")
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    intervalo = _validar(sala, inicio, fim)
    em_conflito = dominio.conflitos(sala, intervalo)
    return Disponibilidade(
        sala=sala,
        livre=not em_conflito,
        conflitos=[ConflitoOut(**{k: r[k] for k in ("id", "inicio", "fim", "responsavel")}) for r in em_conflito],
    )


# --- MRTR da reserva ------------------------------------------------------------
#
# O servidor nao chama o cliente: ele termina a resposta com input_required, e o
# cliente volta com um request novo. Tudo o que a segunda rodada precisa (o pedido
# e as alternativas oferecidas) viaja no requestState, que o RequestStateBoundary
# do SDK sela na saida e verifica na volta. Nada fica guardado neste processo.


def _schema_da_escolha(alternativas: list[str]) -> dict:
    """Schema plano: `sala` restrita as alternativas, na ordem da regra fechada."""
    return {
        "type": "object",
        "properties": {
            "sala": {
                "type": "string",
                "title": "Sala",
                "description": "Sala alternativa escolhida",
                "enum": alternativas,
            }
        },
        "required": ["sala"],
    }


def _declarou_elicitation_form(ctx: Context) -> bool:
    """Le a capability do _meta deste request, nunca de um anterior.

    `elicitation: {}` sem modos conta como form (a forma de antes dos modos);
    so url, sem form, nao conta.
    """
    capacidades = ctx.client_capabilities
    elicitation = capacidades.elicitation if capacidades is not None else None
    return elicitation is not None and (elicitation.form is not None or elicitation.url is None)


def _perguntar(ctx: Context, pedido: dict[str, str], alternativas: list[str]) -> InputRequiredResult:
    if not _declarou_elicitation_form(ctx):
        raise MCPError(
            code=MISSING_REQUIRED_CLIENT_CAPABILITY,
            message="Client did not declare the form elicitation capability required by reservar_sala",
            data={"requiredCapabilities": {"elicitation": {"form": {}}}},
        )
    pergunta = ElicitRequest(
        params=ElicitRequestFormParams(message=MENSAGEM_ELICITACAO,
                                       requested_schema=_schema_da_escolha(alternativas))
    )
    estado = {"v": VERSAO_DO_ESTADO, "pedido": pedido, "alternativas": alternativas}
    # Em texto aqui; o boundary do SDK cifra e assina antes de sair (AES-256-GCM).
    return InputRequiredResult(input_requests={CHAVE_DA_ESCOLHA: pergunta},
                               request_state=json.dumps(estado, separators=(",", ":")))


def _estado_selado(ctx: Context) -> tuple[dict[str, str], list[str]] | None:
    """O requestState ja verificado pelo boundary (None na primeira rodada)."""
    bruto = ctx.request_state
    if bruto is None:
        return None
    try:
        estado = json.loads(bruto)
        assert estado["v"] == VERSAO_DO_ESTADO
        pedido, alternativas = estado["pedido"], estado["alternativas"]
        assert isinstance(pedido, dict) and isinstance(alternativas, list)
    except (ValueError, KeyError, TypeError, AssertionError):
        raise MCPError(code=INVALID_PARAMS, message="Invalid requestState") from None
    return pedido, alternativas


def _reservar(sala: str, pedido: dict[str, str]) -> ReservaOut:
    reserva = dominio.criar_reserva(sala, pedido["inicio"], pedido["fim"], pedido["responsavel"])
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=sala,
        inicio=pedido["inicio"],
        fim=pedido["fim"],
        responsavel=pedido["responsavel"],
        politica=dominio.POLITICA_VERSAO,
    )


def _resolver_conflito(ctx: Context, pedido: dict[str, str], intervalo: dominio.Intervalo) -> InputRequiredResult:
    alternativas = dominio.alternativas(pedido["sala"], intervalo)
    if not alternativas:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    return _perguntar(ctx, pedido, alternativas)


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str, ctx: Context) -> ReservaOut | InputRequiredResult:
    selado = _estado_selado(ctx)

    if selado is None:
        # Primeira rodada: valida, reserva se estiver livre, pergunta se houver conflito.
        pedido = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        intervalo = _validar(sala, inicio, fim)
        if not dominio.conflitos(sala, intervalo):
            return _reservar(sala, pedido)
        return _resolver_conflito(ctx, pedido, intervalo)

    # Retry: o pedido sai do estado selado, nao dos argumentos reenviados. (O boundary
    # ja recusa com -32602 argumentos que divergem do que foi selado.)
    pedido, oferecidas = selado
    resposta = (ctx.input_responses or {}).get(CHAVE_DA_ESCOLHA)
    if resposta is None:
        return _resolver_conflito(ctx, pedido, _validar(pedido["sala"], pedido["inicio"], pedido["fim"]))
    if not isinstance(resposta, ElicitResult):
        raise ToolError("Resposta invalida: era esperada a resposta de uma elicitation")
    if resposta.action == "decline":
        return ReservaOut(reservado=False, motivo="recusado")
    if resposta.action == "cancel":
        return ReservaOut(reservado=False, motivo="cancelado")

    escolhida = (resposta.content or {}).get("sala")
    if escolhida not in oferecidas:
        raise ToolError(f"Escolha fora das alternativas oferecidas: {escolhida}")
    intervalo = _validar(escolhida, pedido["inicio"], pedido["fim"])
    if not dominio.conflitos(escolhida, intervalo):
        return _reservar(escolhida, pedido)
    # A sala escolhida foi ocupada entre as rodadas: pergunta de novo com as alternativas de agora.
    return _resolver_conflito(ctx, pedido, intervalo)


# --- resource -----------------------------------------------------------------


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


def main() -> None:
    porta = int(os.environ.get("MCP_PORT", "7301"))
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    app = mcp.streamable_http_app(stateless_http=True, json_response=True)
    print(
        f"mcp {NOME} em http://{host}:{porta}/mcp (politica {dominio.POLITICA_VERSAO}, "
        f"requestState valido por {VALIDADE_DO_ESTADO_SEGUNDOS}s)",
        file=sys.stderr,
        flush=True,
    )
    uvicorn.run(RegistroDeRequests(app), host=host, port=porta, log_level="warning")


if __name__ == "__main__":
    main()
