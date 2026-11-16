"""Regras de sala da Hill Valley Tech, sem nada de protocolo.

Carrega os arquivos de dados/ uma vez, guarda as reservas em memoria e responde
as perguntas que as tools fazem: a sala existe, o intervalo respeita a politica,
quem conflita e quais sao as alternativas.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path

DADOS = Path(__file__).resolve().parents[1] / "dados"

FUSO = timezone(timedelta(hours=-3))
ABERTURA = time(8, 0)
FECHAMENTO = time(20, 0)
DURACAO_MAXIMA = timedelta(hours=2)
MAX_ALTERNATIVAS = 3

ERRO_SALA = "Sala inexistente: {sala}"
ERRO_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
ERRO_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
ERRO_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
ERRO_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
ERRO_DATA = "Data invalida: use ISO 8601 com fuso, por exemplo 2026-11-03T14:00:00-03:00"


class RegraVioladaError(Exception):
    """Pedido que a politica ou os dados recusam. A mensagem vai ao cliente como esta."""


@dataclass(frozen=True)
class Intervalo:
    inicio: datetime
    fim: datetime

    def sobrepoe(self, outro: Intervalo) -> bool:
        return self.inicio < outro.fim and outro.inicio < self.fim


SALAS: list[dict] = json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))
SALAS_POR_ID: dict[str, dict] = {s["id"]: s for s in SALAS}
POLITICA_TEXTO: str = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")
POLITICA_VERSAO: str = POLITICA_TEXTO.splitlines()[0].split(":", 1)[1].strip()

_reservas: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
_proximo_id = itertools.count(len(_reservas) + 1)


def _ler_data(valor: str) -> datetime:
    try:
        data = datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        raise RegraVioladaError(ERRO_DATA) from None
    if data.tzinfo is None:
        raise RegraVioladaError(ERRO_DATA)
    return data


def _intervalo_de(reserva: dict) -> Intervalo:
    return Intervalo(_ler_data(reserva["inicio"]), _ler_data(reserva["fim"]))


def validar(sala: str, inicio: str, fim: str) -> Intervalo:
    """Aplica as checagens de sala e de politica, na ordem do enunciado."""
    if sala not in SALAS_POR_ID:
        raise RegraVioladaError(ERRO_SALA.format(sala=sala))
    intervalo = Intervalo(_ler_data(inicio), _ler_data(fim))
    if intervalo.fim <= intervalo.inicio:
        raise RegraVioladaError(ERRO_INTERVALO)
    local_inicio = intervalo.inicio.astimezone(FUSO)
    local_fim = intervalo.fim.astimezone(FUSO)
    dentro = (
        local_inicio.date() == local_fim.date()
        and ABERTURA <= local_inicio.time()
        and local_fim.time() <= FECHAMENTO
    )
    if not dentro:
        raise RegraVioladaError(ERRO_JANELA)
    if intervalo.fim - intervalo.inicio > DURACAO_MAXIMA:
        raise RegraVioladaError(ERRO_DURACAO)
    return intervalo


def conflitos(sala: str, intervalo: Intervalo) -> list[dict]:
    return [r for r in _reservas if r["sala"] == sala and _intervalo_de(r).sobrepoe(intervalo)]


def alternativas(sala: str, intervalo: Intervalo) -> list[str]:
    """Salas livres no intervalo, com capacidade >= a pedida, por (capacidade, id), no maximo tres."""
    minima = SALAS_POR_ID[sala]["capacidade"]
    candidatas = [
        s
        for s in SALAS
        if s["id"] != sala and s["capacidade"] >= minima and not conflitos(s["id"], intervalo)
    ]
    candidatas.sort(key=lambda s: (s["capacidade"], s["id"]))
    return [s["id"] for s in candidatas[:MAX_ALTERNATIVAS]]


def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> dict:
    reserva = {
        "id": f"res-{next(_proximo_id):04d}",
        "sala": sala,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    _reservas.append(reserva)
    return reserva
