"""Unidade: a maquina de estados da Task e a leitura do formato fixo (agente/)."""

from __future__ import annotations

import json

import pytest

import ponte
import tarefas as t


def _task_em(*estados: str) -> t.Task:
    task = t.Tarefas().criar()
    for estado in estados:
        task.mudar(estado)
    return task


@pytest.mark.parametrize("terminal", [t.COMPLETED, t.FAILED, t.CANCELED])
@pytest.mark.parametrize("destino", [t.WORKING, t.INPUT_REQUIRED, t.COMPLETED])
def test_estado_terminal_e_definitivo(terminal, destino):
    task = _task_em(t.WORKING, terminal)
    with pytest.raises(t.TransicaoInvalidaError):
        task.mudar(destino)
    assert task.estado == terminal


def test_task_nova_comeca_em_submitted_com_ids_proprios():
    registro = t.Tarefas()
    a, b = registro.criar(), registro.criar()
    assert a.estado == t.SUBMITTED
    assert a.id != b.id and a.context_id != b.context_id


def test_pausa_pode_se_repetir_e_voltar_a_working():
    task = _task_em(t.WORKING, t.INPUT_REQUIRED, t.INPUT_REQUIRED, t.WORKING, t.COMPLETED)
    assert task.terminal


def test_request_state_nunca_entra_no_wire_da_task():
    registro = t.Tarefas()
    task = registro.criar()
    registro.pausar(task.id, t.Pendencia("chave", "sala", ["sala-fusca"], "v1.SEGREDO-OPACO"))
    task.mudar(t.WORKING)
    task.mudar(t.INPUT_REQUIRED, ponte.linha_de_alternativas(["sala-fusca"]))
    assert "SEGREDO-OPACO" not in json.dumps(task.para_wire())
    assert registro.pendencia(task.id).request_state == "v1.SEGREDO-OPACO"


def test_linha_de_alternativas_e_exata():
    assert ponte.linha_de_alternativas(["sala-fusca", "sala-mirante"]) == "alternativas: sala-fusca, sala-mirante"


@pytest.mark.parametrize(
    ("texto", "esperado"),
    [
        ("reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty",
         ("sala-garagem", "2026-11-03T14:00:00-03:00", "2026-11-03T15:00:00-03:00", "Marty")),
        ("reservar sala=sala-porao inicio=a fim=b responsavel=Emmett Brown",
         ("sala-porao", "a", "b", "Emmett Brown")),
    ],
)
def test_pedido_em_formato_fixo(texto, esperado):
    assert ponte.PEDIDO.match(texto).groups() == esperado


@pytest.mark.parametrize("texto", ["reserve a garagem amanha", "reservar sala=sala-porao", "escolha=sala-fusca"])
def test_texto_fora_do_formato_nao_e_pedido(texto):
    assert ponte.PEDIDO.match(texto) is None
