"""Entrega: o validador do starter de ponta a ponta e as regras do repositorio."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import RAIZ, Processo, porta_livre

SDKS_DE_LLM = ("anthropic", "openai", "google-genai", "google-generativeai", "langchain", "mistralai",
               "cohere", "ollama", "litellm")


def _git(*args: str) -> str:
    return subprocess.run(["git", "-c", f"safe.directory={RAIZ}", *args], cwd=RAIZ,
                          capture_output=True, text=True, check=True).stdout


def test_validador_passa_nas_36_verificacoes(segredo):
    porta_mcp, porta_agente = porta_livre(), porta_livre()
    servidor = Processo("servidor-mcp/servidor.py", porta_mcp,
                        {"REQUEST_STATE_SECRET": segredo, "MCP_PORT": str(porta_mcp)}).subir()
    agente = Processo("agente/agente.py", porta_agente, {
        "AGENT_PORT": str(porta_agente),
        "AGENT_PUBLIC_URL": f"http://127.0.0.1:{porta_agente}",
        "MCP_URL": f"http://127.0.0.1:{porta_mcp}/mcp",
    }).subir()
    try:
        saida = subprocess.run(
            [sys.executable, "validador/validar.py", "--agente", f"http://127.0.0.1:{porta_agente}",
             "--mcp", f"http://127.0.0.1:{porta_mcp}"],
            cwd=RAIZ, capture_output=True, text=True, timeout=120,
        )
    finally:
        agente.parar()
        servidor.parar()
    assert saida.returncode == 0, saida.stdout
    assert "36 passaram, 0 falharam" in saida.stdout
    trace_id = saida.stdout.splitlines()[0].split()[-1]
    assert trace_id in servidor.stderr, "o trace-id do validador nao chegou ao stderr do servidor MCP"


def test_dados_validador_e_exemplos_intocados():
    try:
        inicial = _git("rev-list", "--max-parents=0", "HEAD").split()[0]
    except (subprocess.CalledProcessError, FileNotFoundError, IndexError):
        pytest.skip("fora de um clone git")
    alterados = _git("diff", "--name-only", inicial, "--", "dados", "validador", "exemplos")
    assert alterados.strip() == ""


def test_nenhum_segredo_literal_versionado():
    try:
        arquivos = _git("ls-files").split()
    except (subprocess.CalledProcessError, FileNotFoundError):
        pytest.skip("fora de um clone git")
    hex64 = re.compile(r"\b[0-9a-f]{64}\b")
    for nome in arquivos:
        texto = (RAIZ / nome).read_text(encoding="utf-8", errors="ignore")
        assert not hex64.search(texto), f"parece haver uma chave em {nome}"
        assert not re.search(r"REQUEST_STATE_SECRET\s*=\s*['\"]\w", texto), nome


@pytest.mark.parametrize("projeto", ["servidor-mcp", "agente", "testes"])
def test_versoes_travadas_e_sem_sdk_de_llm(projeto):
    # Leitura simples da lista `dependencies` (tomllib so existe a partir do 3.11).
    texto = (RAIZ / projeto / "pyproject.toml").read_text(encoding="utf-8")
    bloco = re.search(r"^dependencies = \[(.*?)^\]", texto, re.S | re.M).group(1)
    dependencias = re.findall(r'"([^"]+)"', bloco)
    assert dependencias and all("==" in d for d in dependencias), dependencias
    assert not any(d.lower().startswith(SDKS_DE_LLM) for d in dependencias)


@pytest.mark.parametrize("secao", ["## Como rodar", "## Onde a ponte acontece", "## Decisões técnicas",
                                   "## Saída do validador"])
def test_readme_tem_as_quatro_secoes(secao):
    assert secao in Path(RAIZ / "README.md").read_text(encoding="utf-8")
