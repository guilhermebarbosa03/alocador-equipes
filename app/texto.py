"""Comparação de textos digitados por pessoas: nomes de especialidades, certificações, cidades."""

import re
import unicodedata
from datetime import datetime, time, timedelta

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def norm(texto: str | None) -> str:
    """Normaliza nomes para comparação: sem acento, minúsculo, espaços únicos."""
    s = unicodedata.normalize("NFKD", texto or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def cidade(texto: str | None) -> str:
    """'Contagem/MG' -> 'contagem'; 'Belo Horizonte - MG' -> 'belo horizonte'."""
    return re.split(r"\s*[/,(\-–]\s*", norm(texto), maxsplit=1)[0].strip()


def mesma_cidade(a: str | None, b: str | None) -> bool:
    ca, cb = cidade(a), cidade(b)
    return bool(ca) and ca == cb


def quando(inicio: datetime, fim: datetime) -> str:
    """'ter 17/11, 08:00–17:00', 'seg 16/11 a qua 18/11 (dias inteiros)'..."""

    def dia(d: datetime) -> str:
        return f"{DIAS[d.weekday()]} {d:%d/%m}"

    if inicio.date() == fim.date():
        return f"{dia(inicio)}, {inicio:%H:%M}–{fim:%H:%M}"
    if inicio.time() == fim.time() == time.min:  # dias inteiros (ausências)
        ultimo = fim - timedelta(days=1)
        if ultimo.date() == inicio.date():
            return f"{dia(inicio)} (dia inteiro)"
        return f"{dia(inicio)} a {dia(ultimo)} (dias inteiros)"
    return f"{dia(inicio)} {inicio:%H:%M} → {dia(fim)} {fim:%H:%M}"
