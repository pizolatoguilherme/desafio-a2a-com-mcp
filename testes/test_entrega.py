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


# --- restricoes nao negociaveis e estrutura (checagem estatica) ---------------------

import ast  # noqa: E402

AGENTE = sorted((RAIZ / "agente").glob("*.py"))
MODULOS_DO_SERVIDOR = {p.stem for p in (RAIZ / "servidor-mcp").glob("*.py")}


def _importados(caminho: Path) -> set[str]:
    arvore = ast.parse(caminho.read_text(encoding="utf-8"))
    nomes: set[str] = set()
    for no in ast.walk(arvore):
        if isinstance(no, ast.Import):
            nomes |= {a.name.split(".")[0] for a in no.names}
        elif isinstance(no, ast.ImportFrom) and no.module:
            nomes.add(no.module.split(".")[0])
    return nomes


def test_agente_nao_importa_codigo_do_servidor_nem_sdk_de_llm():
    """Dois processos separados: o agente so fala com o servidor por HTTP."""
    for arquivo in AGENTE:
        importados = _importados(arquivo)
        assert not importados & MODULOS_DO_SERVIDOR, (arquivo.name, importados & MODULOS_DO_SERVIDOR)
        assert not any(i.startswith(tuple(s.replace("-", "_") for s in SDKS_DE_LLM)) for i in importados), arquivo.name


def test_request_state_e_opaco_para_o_agente():
    """Guardar e ecoar, nunca abrir: nada de base64, decodificacao ou json sobre o token."""
    for arquivo in AGENTE:
        texto = arquivo.read_text(encoding="utf-8")
        assert "base64" not in texto and "b64decode" not in texto, arquivo.name
        for linha in texto.splitlines():
            if "request_state" in linha or "requestState" in linha:
                assert not re.search(r"loads|decode|split|\[\s*\d|unseal", linha), (arquivo.name, linha)


def test_agente_nao_tem_regra_de_sala():
    """Conflito, politica e alternativas sao do servidor MCP."""
    proibidos = re.compile(r"capacidade|salas\.json|reservas\.json|08:00|20:00|timedelta|fromisoformat|sobrepoe")
    for arquivo in AGENTE:
        codigo = "\n".join(l for l in arquivo.read_text(encoding="utf-8").splitlines() if not l.strip().startswith("#"))
        assert not proibidos.search(codigo), arquivo.name


@pytest.mark.parametrize("caminho", ["README.md", "dados/salas.json", "dados/reservas.json", "dados/politica-de-uso.md",
                                     "validador/validar.py", "exemplos/wire"])
def test_estrutura_obrigatoria(caminho):
    assert (RAIZ / caminho).exists()


@pytest.mark.parametrize("pasta", ["servidor-mcp", "agente"])
def test_pastas_da_entrega_preenchidas(pasta):
    assert list((RAIZ / pasta).glob("*.py")), pasta


def test_readme_documenta_como_gerar_e_exportar_o_segredo():
    readme = (RAIZ / "README.md").read_text(encoding="utf-8")
    assert 'export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")' in readme
