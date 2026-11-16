"""A ponte: onde o estado do MCP (requestState) encontra o estado do A2A (Task).

- `iniciar`: abre o pedido, descobre as tools, le a politica e faz o tools/call.
- `continuar`: recebe a escolha do cliente A2A e repete o tools/call original,
  com id novo, levando inputResponses e o requestState guardado na Task.
- `_aplicar`: traduz o resultado MCP em estado de Task. E aqui que
  `resultType: input_required` vira `TASK_STATE_INPUT_REQUIRED`.

O agente nao decide nada de sala: conflito, politica e alternativas vem do
servidor MCP. Ele so traduz protocolo, por regra fixa e sem LLM.
"""

from __future__ import annotations

import json
import re
import sys

from host_mcp import Completo, ErroProtocolo, HostMCP, PrecisaInput, Resultado
from tarefas import (
    CANCELED,
    COMPLETED,
    FAILED,
    INPUT_REQUIRED,
    WORKING,
    Pedido,
    Pendencia,
    Task,
    Tarefas,
)

TOOL_DE_RESERVA = "reservar_sala"
RECUSAR = "recusar"

PEDIDO = re.compile(r"^reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")

FORMATO_PEDIDO = "Pedido invalido: use reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"


class ErroA2A(Exception):
    """Erro de JSON-RPC devolvido ao cliente A2A (a Task nao muda)."""

    def __init__(self, codigo: int, mensagem: str) -> None:
        super().__init__(mensagem)
        self.codigo = codigo
        self.mensagem = mensagem


TASK_NAO_ENCONTRADA = -32001
OPERACAO_NAO_SUPORTADA = -32004


def linha_de_alternativas(opcoes: list[str]) -> str:
    return "alternativas: " + ", ".join(opcoes)


class Ponte:
    def __init__(self, host: HostMCP, tarefas: Tarefas) -> None:
        self.host = host
        self.tarefas = tarefas

    async def iniciar(self, mensagem: dict, texto: str, trace_id: str) -> Task:
        task = self.tarefas.criar(mensagem.get("contextId"))
        task.historico.append(mensagem)
        async with task.trava:
            task.mudar(WORKING)
            casou = PEDIDO.match(texto.strip())
            if not casou:
                task.mudar(FAILED, FORMATO_PEDIDO)
                return task
            sala, inicio, fim, responsavel = casou.groups()
            argumentos = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel.strip()}
            try:
                tools = await self.host.descobrir_tools(trace_id)
                if TOOL_DE_RESERVA not in tools:
                    task.mudar(FAILED, f"O servidor MCP nao oferece a tool {TOOL_DE_RESERVA}")
                    return task
                politica = await self.host.versao_da_politica(trace_id)
                pedido = Pedido(TOOL_DE_RESERVA, argumentos, politica, trace_id)
                self.tarefas.guardar_pedido(task.id, pedido)
                resultado = await self.host.chamar_tool(pedido.tool, pedido.argumentos, trace_id)
            except Exception as e:  # servidor MCP fora do ar, resposta malformada
                print(f"agente: falha falando com o servidor MCP: {e!r}", file=sys.stderr, flush=True)
                task.mudar(FAILED, f"Falha ao falar com o servidor MCP: {e}")
                return task
            self._aplicar(task, pedido, resultado)
        return task

    async def continuar(self, mensagem: dict, texto: str, task_id: str) -> Task:
        task = self.tarefas.obter(task_id)
        if task is None:
            raise ErroA2A(TASK_NAO_ENCONTRADA, f"Task nao encontrada: {task_id}")
        async with task.trava:
            if task.terminal:
                raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"Task {task_id} esta em estado terminal ({task.estado})")
            pendencia = self.tarefas.pendencia(task.id)
            pedido = self.tarefas.pedido(task.id)
            if task.estado != INPUT_REQUIRED or pendencia is None or pedido is None:
                raise ErroA2A(OPERACAO_NAO_SUPORTADA, f"Task {task_id} nao esta aguardando resposta")
            task.historico.append(mensagem)

            casou = ESCOLHA.match(texto.strip())
            escolha = casou.group(1) if casou else None
            if escolha != RECUSAR and escolha not in pendencia.opcoes:
                # Fora do enum: a Task continua pausada e a pergunta se repete igual.
                task.mudar(INPUT_REQUIRED, linha_de_alternativas(pendencia.opcoes))
                return task

            if escolha == RECUSAR:
                resposta = {"action": "decline"}
            else:
                resposta = {"action": "accept", "content": {pendencia.campo: escolha}}

            task.mudar(WORKING)
            self.tarefas.retirar_pendencia(task.id)
            try:
                # O retry: mesmo tools/call, id JSON-RPC novo (gerado pelo SDK),
                # a mesma chave do inputRequests e o requestState ecoado sem tocar.
                resultado = await self.host.chamar_tool(
                    pedido.tool,
                    pedido.argumentos,
                    pedido.trace_id,
                    input_responses={pendencia.chave: resposta},
                    request_state=pendencia.request_state,
                )
            except Exception as e:
                print(f"agente: falha no retry MCP: {e!r}", file=sys.stderr, flush=True)
                task.mudar(FAILED, f"Falha ao falar com o servidor MCP: {e}")
                return task
            self._aplicar(task, pedido, resultado)
        return task

    def _aplicar(self, task: Task, pedido: Pedido, resultado: Resultado) -> None:
        """Traduz o resultado MCP em estado de Task. Este e o coracao da ponte."""
        if isinstance(resultado, PrecisaInput):
            # input_required do MCP -> TASK_STATE_INPUT_REQUIRED do A2A.
            # O requestState fica guardado, ligado a esta Task, e so volta ao servidor MCP.
            self.tarefas.pausar(
                task.id,
                Pendencia(resultado.chave, resultado.campo, resultado.opcoes, resultado.request_state),
            )
            task.mudar(INPUT_REQUIRED, linha_de_alternativas(resultado.opcoes))
            return

        if isinstance(resultado, ErroProtocolo):
            task.mudar(FAILED, f"Erro do servidor MCP ({resultado.codigo}): {resultado.mensagem}")
            return

        assert isinstance(resultado, Completo)
        if resultado.erro:
            # Erro de execucao da tool: a mensagem exata vai para o historico da Task.
            task.mudar(FAILED, resultado.texto)
            return

        dados = resultado.estruturado
        if dados.get("reservado") is False:
            task.mudar(CANCELED, "Reserva recusada: nenhuma sala foi reservada.")
            return

        reserva = {
            "reserva": dados.get("reserva"),
            "sala": dados.get("sala"),
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": pedido.politica,  # versao lida do resource politica://uso
        }
        task.anexar_artifact("reserva", json.dumps(reserva))
        task.mudar(COMPLETED, f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")
