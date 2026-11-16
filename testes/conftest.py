"""Infraestrutura dos testes: sobe os processos de verdade e fala HTTP com eles.

Cada teste de fronteira ganha um servidor MCP (e, quando precisa, um agente)
recem-iniciado em portas livres, entao os testes nao dependem dos processos que
voce tenha no ar nas portas 7300 e 7301, nem uns dos outros.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

RAIZ = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PROTOCOLO = "2026-07-28"
CAP_FORM = {"elicitation": {"form": {}}}
DIA = "2026-11-03"

# Os testes de unidade importam os modulos dos dois processos direto da pasta.
sys.path.insert(0, str(RAIZ / "servidor-mcp"))
sys.path.insert(0, str(RAIZ / "agente"))


def h(hora: str) -> str:
    return f"{DIA}T{hora}:00-03:00"


def porta_livre() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def esperar_porta(porta: int, processo: subprocess.Popen | None = None, limite: float = 20.0) -> None:
    fim = time.monotonic() + limite
    while time.monotonic() < fim:
        if processo is not None and processo.poll() is not None:
            raise RuntimeError(f"o processo saiu antes de abrir a porta {porta}")
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", porta)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nada escutando em {porta}")


class Processo:
    """Um dos dois entrypoints rodando como subprocesso, com stderr guardado em arquivo."""

    def __init__(self, script: str, porta: int, env: dict[str, str]) -> None:
        self.script = script
        self.porta = porta
        self.env = {**os.environ, **env}
        self.log = tempfile.NamedTemporaryFile(prefix="stderr-", suffix=".log", delete=False)
        self.proc: subprocess.Popen | None = None

    def subir(self) -> Processo:
        self.proc = subprocess.Popen(
            [PYTHON, str(RAIZ / self.script)], cwd=RAIZ, env=self.env,
            stdout=self.log, stderr=subprocess.STDOUT,
        )
        esperar_porta(self.porta, self.proc)
        return self

    def parar(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()

    def reiniciar(self) -> None:
        self.parar()
        self.subir()

    @property
    def stderr(self) -> str:
        return Path(self.log.name).read_text(encoding="utf-8", errors="replace")


def rodar_ate_sair(script: str, env: dict[str, str], limite: float = 15.0) -> subprocess.CompletedProcess:
    """Para os casos em que o processo deve recusar subir."""
    return subprocess.run(
        [PYTHON, str(RAIZ / script)], cwd=RAIZ, env={**os.environ, **env},
        capture_output=True, text=True, timeout=limite,
    )


# --- clientes HTTP minimos ------------------------------------------------------


def post(url: str, corpo: Any, cabecalhos: dict[str, str]) -> tuple[int, dict]:
    req = urllib.request.Request(url, data=json.dumps(corpo).encode(), headers=cabecalhos, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        bruto = e.read().decode()
        return e.code, json.loads(bruto) if bruto else {}


class ClienteMCP:
    def __init__(self, url: str) -> None:
        self.url = url

    def chamar(self, metodo: str, params: dict, *, capabilities: dict | None = None,
               nome: str | None = None, traceparent: str | None = None) -> tuple[int, dict]:
        meta: dict[str, Any] = {
            "io.modelcontextprotocol/protocolVersion": PROTOCOLO,
            "io.modelcontextprotocol/clientCapabilities": CAP_FORM if capabilities is None else capabilities,
        }
        if traceparent:
            meta["traceparent"] = traceparent
        cabecalhos = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": PROTOCOLO,
            "Mcp-Method": metodo,
        }
        if nome:
            cabecalhos["Mcp-Name"] = nome
        corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(4), "method": metodo, "params": {**params, "_meta": meta}}
        return post(self.url, corpo, cabecalhos)

    def tool(self, nome: str, argumentos: dict, **kw) -> tuple[int, dict]:
        return self.chamar("tools/call", {"name": nome, "arguments": argumentos}, nome=nome, **kw)

    def reservar(self, sala: str, inicio: str, fim: str, responsavel: str = "Teste", **kw) -> tuple[int, dict]:
        args = {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
        return self.tool("reservar_sala", args, **kw)

    def retomar(self, argumentos: dict, chave: str, resposta: dict, estado: str) -> tuple[int, dict]:
        params = {
            "name": "reservar_sala",
            "arguments": argumentos,
            "inputResponses": {chave: resposta},
            "requestState": estado,
        }
        return self.chamar("tools/call", params, nome="reservar_sala")

    def livre(self, sala: str, inicio: str, fim: str) -> bool:
        _, r = self.tool("consultar_disponibilidade", {"sala": sala, "inicio": inicio, "fim": fim})
        return r["result"]["structuredContent"]["livre"]


class ClienteA2A:
    def __init__(self, base: str) -> None:
        self.base = base
        self.respostas_brutas: list[str] = []

    def rpc(self, metodo: str, params: dict, traceparent: str | None = None) -> dict:
        cabecalhos = {"Content-Type": "application/json"}
        if traceparent:
            cabecalhos["traceparent"] = traceparent
        _, resposta = post(f"{self.base}/a2a", {"jsonrpc": "2.0", "id": 1, "method": metodo, "params": params},
                           cabecalhos)
        self.respostas_brutas.append(json.dumps(resposta))
        return resposta

    def enviar(self, texto: str, task_id: str | None = None, traceparent: str | None = None) -> dict:
        mensagem: dict[str, Any] = {"messageId": f"msg-{secrets.token_hex(4)}", "role": "ROLE_USER",
                                    "parts": [{"text": texto}]}
        if task_id:
            mensagem["taskId"] = task_id
        return self.rpc("SendMessage", {"message": mensagem}, traceparent)


def task_de(resposta: dict) -> dict:
    return (resposta.get("result") or {}).get("task") or {}


def estado_de(resposta: dict) -> str:
    return task_de(resposta).get("status", {}).get("state", "")


def texto_de(resposta: dict) -> str:
    partes = task_de(resposta).get("status", {}).get("message", {}).get("parts", [])
    return " ".join(p.get("text", "") for p in partes)


# --- proxy gravador -------------------------------------------------------------


class ProxyGravador:
    """Fica entre o agente e o servidor MCP e grava cada request e resposta.

    E assim que os testes enxergam o que o agente realmente poe no fio:
    headers, _meta, ids de JSON-RPC e o requestState ecoado.
    """

    def __init__(self, destino: str) -> None:
        self.destino = destino
        self.trocas: list[dict[str, Any]] = []
        self.porta = porta_livre()
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _encaminhar(self) -> None:
                tamanho = int(self.headers.get("Content-Length") or 0)
                corpo = self.rfile.read(tamanho) if tamanho else None
                cabecalhos = {k: v for k, v in self.headers.items() if k.lower() not in ("host", "content-length")}
                req = urllib.request.Request(proxy.destino, data=corpo, headers=cabecalhos, method=self.command)
                try:
                    with urllib.request.urlopen(req, timeout=30) as r:
                        status, tipo, resposta = r.status, r.headers.get("Content-Type", ""), r.read()
                except urllib.error.HTTPError as e:
                    status, tipo, resposta = e.code, e.headers.get("Content-Type", ""), e.read()
                except urllib.error.URLError:
                    # Servidor MCP fora do ar: derruba a conexao sem resposta,
                    # como o agente veria sem o proxy no meio.
                    self.close_connection = True
                    return
                proxy.trocas.append({
                    "metodo_http": self.command,
                    "headers": {k.lower(): v for k, v in self.headers.items()},
                    "corpo": json.loads(corpo) if corpo else None,
                    "status": status,
                    "resposta": json.loads(resposta) if resposta and "json" in tipo else None,
                })
                self.send_response(status)
                if tipo:
                    self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(resposta)))
                self.end_headers()
                self.wfile.write(resposta)

            do_POST = do_GET = do_DELETE = _encaminhar

        self.servidor = ThreadingHTTPServer(("127.0.0.1", self.porta), Handler)
        self.thread = threading.Thread(target=self.servidor.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.porta}/mcp"

    def __enter__(self) -> ProxyGravador:
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.servidor.shutdown()
        self.servidor.server_close()

    def jsonrpc(self) -> list[dict[str, Any]]:
        return [t for t in self.trocas if isinstance(t["corpo"], dict) and "method" in t["corpo"]]


# --- fixtures -------------------------------------------------------------------


@pytest.fixture
def segredo() -> str:
    return secrets.token_hex(32)


@pytest.fixture
def servidor_mcp(segredo):
    porta = porta_livre()
    processo = Processo("servidor-mcp/servidor.py", porta,
                        {"REQUEST_STATE_SECRET": segredo, "MCP_PORT": str(porta)}).subir()
    processo.url = f"http://127.0.0.1:{porta}/mcp"
    yield processo
    processo.parar()


@pytest.fixture
def mcp(servidor_mcp) -> ClienteMCP:
    return ClienteMCP(servidor_mcp.url)


@pytest.fixture
def proxy(servidor_mcp):
    with ProxyGravador(servidor_mcp.url) as p:
        yield p


@pytest.fixture
def agente(proxy):
    """Agente apontado para o servidor MCP atraves do proxy gravador."""
    porta = porta_livre()
    processo = Processo("agente/agente.py", porta, {
        "AGENT_PORT": str(porta),
        "AGENT_PUBLIC_URL": f"http://127.0.0.1:{porta}",
        "MCP_URL": proxy.url,
    }).subir()
    processo.base = f"http://127.0.0.1:{porta}"
    yield processo
    processo.parar()


@pytest.fixture
def a2a(agente) -> ClienteA2A:
    return ClienteA2A(agente.base)
