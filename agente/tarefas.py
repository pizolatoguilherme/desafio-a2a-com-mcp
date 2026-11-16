"""Tasks A2A em memoria: identidade, estado, historico e produto.

A pendencia de MRTR (chave da elicitation + requestState) fica num dicionario
separado, ligado ao id da Task, e nunca e serializada: nao ha caminho do
requestState ate o card, o artifact ou qualquer mensagem devolvida ao cliente.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
from dataclasses import dataclass, field
from typing import Any

SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
FAILED = "TASK_STATE_FAILED"
CANCELED = "TASK_STATE_CANCELED"

TERMINAIS = frozenset({COMPLETED, FAILED, CANCELED})
TRANSICOES = {
    SUBMITTED: {WORKING, FAILED, CANCELED},
    WORKING: {INPUT_REQUIRED, COMPLETED, FAILED, CANCELED},
    INPUT_REQUIRED: {WORKING, INPUT_REQUIRED, FAILED, CANCELED},
}


def novo_id(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


class TransicaoInvalidaError(Exception):
    pass


@dataclass
class Task:
    id: str
    context_id: str
    estado: str = SUBMITTED
    mensagem_status: dict[str, Any] | None = None
    historico: list[dict[str, Any]] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    trava: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)

    @property
    def terminal(self) -> bool:
        return self.estado in TERMINAIS

    def mudar(self, estado: str, texto: str | None = None) -> None:
        """Muda o estado. Estado terminal e definitivo."""
        if estado not in TRANSICOES.get(self.estado, set()):
            raise TransicaoInvalidaError(f"{self.estado} -> {estado}")
        print(f"a2a task={self.id} estado={self.estado} -> {estado}", file=sys.stderr, flush=True)
        self.estado = estado
        if texto is not None:
            self.mensagem_status = self.anotar(texto)

    def anotar(self, texto: str) -> dict[str, Any]:
        """Acrescenta uma mensagem do agente ao historico, sem mudar o estado."""
        mensagem = {
            "messageId": novo_id("msg"),
            "role": "ROLE_AGENT",
            "parts": [{"text": texto}],
            "taskId": self.id,
            "contextId": self.context_id,
        }
        self.historico.append(mensagem)
        return mensagem

    def anexar_artifact(self, nome: str, texto: str) -> None:
        self.artifacts.append({"artifactId": novo_id("art"), "name": nome, "parts": [{"text": texto}]})

    def para_wire(self) -> dict[str, Any]:
        status: dict[str, Any] = {"state": self.estado}
        if self.mensagem_status is not None:
            status["message"] = self.mensagem_status
        return {
            "id": self.id,
            "contextId": self.context_id,
            "status": status,
            "history": list(self.historico),
            "artifacts": list(self.artifacts),
        }


@dataclass(frozen=True)
class Pedido:
    """O que o agente precisa lembrar da Task para repetir o tools/call original."""

    tool: str
    argumentos: dict[str, str]
    politica: str
    trace_id: str


@dataclass(frozen=True)
class Pendencia:
    """A pausa de MRTR de uma Task. Nunca sai deste processo."""

    chave: str
    campo: str
    opcoes: list[str]
    request_state: str


class Tarefas:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._pedidos: dict[str, Pedido] = {}
        self._pendencias: dict[str, Pendencia] = {}

    def criar(self, context_id: str | None = None) -> Task:
        task = Task(id=novo_id("task"), context_id=context_id or novo_id("ctx"))
        self._tasks[task.id] = task
        print(f"a2a task={task.id} estado={task.estado}", file=sys.stderr, flush=True)
        return task

    def obter(self, task_id: str) -> Task | None:
        return self._tasks.get(task_id)

    def guardar_pedido(self, task_id: str, pedido: Pedido) -> None:
        self._pedidos[task_id] = pedido

    def pedido(self, task_id: str) -> Pedido | None:
        return self._pedidos.get(task_id)

    def pausar(self, task_id: str, pendencia: Pendencia) -> None:
        self._pendencias[task_id] = pendencia

    def pendencia(self, task_id: str) -> Pendencia | None:
        return self._pendencias.get(task_id)

    def retirar_pendencia(self, task_id: str) -> Pendencia | None:
        return self._pendencias.pop(task_id, None)
