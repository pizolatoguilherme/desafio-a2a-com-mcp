"""Unidade: as regras de sala e de politica do servidor MCP (servidor-mcp/dominio.py)."""

from __future__ import annotations

import pytest

import dominio
from conftest import h


@pytest.mark.parametrize(
    ("sala", "inicio", "fim", "mensagem"),
    [
        ("sala-delorean", h("09:00"), h("10:00"), "Sala inexistente: sala-delorean"),
        ("sala-aquario", h("10:00"), h("09:00"), dominio.ERRO_INTERVALO),
        ("sala-aquario", h("10:00"), h("10:00"), dominio.ERRO_INTERVALO),
        ("sala-aquario", h("07:00"), h("08:00"), dominio.ERRO_JANELA),
        ("sala-aquario", h("19:30"), h("20:30"), dominio.ERRO_JANELA),
        ("sala-aquario", h("09:00"), h("12:00"), dominio.ERRO_DURACAO),
        ("sala-aquario", "2026-11-03T09:00:00", h("10:00"), dominio.ERRO_DATA),
        ("sala-aquario", "amanha", h("10:00"), dominio.ERRO_DATA),
    ],
)
def test_validar_recusa_com_a_mensagem_exata(sala, inicio, fim, mensagem):
    with pytest.raises(dominio.RegraVioladaError) as erro:
        dominio.validar(sala, inicio, fim)
    assert str(erro.value) == mensagem


@pytest.mark.parametrize(("inicio", "fim"), [(h("08:00"), h("10:00")), (h("18:00"), h("20:00"))])
def test_validar_aceita_as_bordas_da_janela_e_duas_horas_exatas(inicio, fim):
    dominio.validar("sala-aquario", inicio, fim)


def test_validar_converte_outro_fuso_para_sao_paulo():
    # 12:00Z = 09:00-03:00, dentro da janela
    dominio.validar("sala-aquario", "2026-11-03T12:00:00+00:00", "2026-11-03T13:00:00+00:00")
    with pytest.raises(dominio.RegraVioladaError, match="Fora da janela"):
        dominio.validar("sala-aquario", "2026-11-03T10:00:00+00:00", "2026-11-03T11:00:00+00:00")


def test_conflito_e_sobreposicao_estrita():
    garagem = dominio.validar("sala-garagem", h("15:00"), h("16:00"))  # encosta em res-0001, nao sobrepoe
    assert dominio.conflitos("sala-garagem", garagem) == []
    sobreposto = dominio.validar("sala-garagem", h("14:30"), h("15:30"))
    assert [r["id"] for r in dominio.conflitos("sala-garagem", sobreposto)] == ["res-0001"]


@pytest.mark.parametrize(
    ("sala", "inicio", "fim", "esperado"),
    [
        # o exemplo do enunciado
        ("sala-garagem", h("14:00"), h("15:00"), ["sala-fusca", "sala-mirante"]),
        # empate de capacidade (12) desempata por id: fusca antes de garagem
        ("sala-porao", h("09:00"), h("10:00"), ["sala-fusca", "sala-garagem", "sala-mirante"]),
        # no maximo tres, por capacidade crescente
        ("sala-aquario", h("09:00"), h("10:00"), ["sala-porao", "sala-fusca", "sala-garagem"]),
        # ninguem cabe 20 pessoas alem do proprio mirante
        ("sala-mirante", h("09:00"), h("10:00"), []),
        # sala ocupada no intervalo nao e alternativa (fusca tem res-0002 as 16h)
        ("sala-garagem", h("16:00"), h("17:00"), ["sala-mirante"]),
    ],
)
def test_alternativas_seguem_a_regra_fechada(sala, inicio, fim, esperado):
    assert dominio.alternativas(sala, dominio.validar(sala, inicio, fim)) == esperado


def test_versao_da_politica_vem_da_primeira_linha():
    assert dominio.POLITICA_VERSAO == "2026-11-01"
    assert dominio.POLITICA_TEXTO.startswith("versao: 2026-11-01")
