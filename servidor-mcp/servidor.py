"""Servidor MCP da Central de Salas, em Streamable HTTP stateless.

Tres tools, um resource e o ciclo de MRTR na reserva. O MRTR nao e um callback:
quando a sala pedida esta ocupada, o resolver `escolha_de_sala` devolve um
`Elicit`, o SDK encerra a resposta com `resultType: input_required` e sela o
`requestState` (AES-256-GCM com a chave de REQUEST_STATE_SECRET). No retry o
SDK verifica o selo, reexecuta o resolver e entrega a resposta do cliente.
Nada fica guardado aqui entre as duas rodadas.

Rode com `python servidor-mcp/servidor.py`.
"""

from __future__ import annotations

import os
import sys
from typing import Annotated, Literal

import uvicorn
from pydantic import BaseModel, Field, create_model

from mcp.server.elicitation import AcceptedElicitation, ElicitationResult
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.resolve import Elicit, Resolve
from mcp.server.request_state import RequestStateSecurity

import dominio
from log_requests import RegistroDeRequests

NOME = "central-de-salas"
VALIDADE_DO_ESTADO_SEGUNDOS = 600  # 10 minutos, dentro da faixa de 5 a 30 do enunciado
MENSAGEM_ELICITACAO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


def _segredo() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode()) < 32:
        sys.exit(
            "REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere uma com:\n"
            '  export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")'
        )
    return segredo


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


class SalaLivre(BaseModel):
    """O que o resolver devolve quando nao ha conflito: a propria sala pedida."""

    sala: str


def _modelo_de_escolha(alternativas: list[str]) -> type[BaseModel]:
    """Schema plano da elicitation: `sala` restrita as alternativas, na ordem calculada."""
    opcoes = Literal.__getitem__(tuple(alternativas))
    return create_model(
        "EscolhaDeSala",
        sala=(opcoes, Field(title="Sala", description="Sala alternativa escolhida")),
    )


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


def escolha_de_sala(sala: str, inicio: str, fim: str) -> SalaLivre | Elicit[BaseModel]:
    """Resolver do MRTR: decide se a reserva precisa perguntar algo ao cliente.

    Roda de novo a cada rodada. Na primeira, um conflito vira `Elicit`, e o SDK
    responde input_required. No retry, a mesma pergunta e refeita e o SDK casa a
    resposta que veio em inputResponses, ja validada contra o enum.
    """
    intervalo = _validar(sala, inicio, fim)
    if not dominio.conflitos(sala, intervalo):
        return SalaLivre(sala=sala)
    alternativas = dominio.alternativas(sala, intervalo)
    if not alternativas:
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    return Elicit(MENSAGEM_ELICITACAO, _modelo_de_escolha(alternativas))


@mcp.tool(description="Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar.")
def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)],
) -> ReservaOut:
    if not isinstance(escolha, AcceptedElicitation):
        return ReservaOut(reservado=False, motivo="recusado")
    sala_final = escolha.data.sala
    intervalo = _validar(sala_final, inicio, fim)
    if dominio.conflitos(sala_final, intervalo):
        raise ToolError(dominio.ERRO_SEM_ALTERNATIVA)
    reserva = dominio.criar_reserva(sala_final, inicio, fim, responsavel)
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=sala_final,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=dominio.POLITICA_VERSAO,
    )


# --- resource -----------------------------------------------------------------


@mcp.resource("politica://uso", name="politica-de-uso", mime_type="text/markdown",
              description="Politica de uso das salas. A primeira linha declara a versao.")
def politica_de_uso() -> str:
    return dominio.POLITICA_TEXTO


def main() -> None:
    porta = int(os.environ.get("MCP_PORT", "7301"))
    app = mcp.streamable_http_app(stateless_http=True, json_response=True)
    uvicorn.run(RegistroDeRequests(app), host=os.environ.get("MCP_HOST", "127.0.0.1"), port=porta,
                log_level="warning")


if __name__ == "__main__":
    main()
