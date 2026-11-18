"""Leitura de listas digitadas, como "Elétrica:4, Automação" ou "NR10:2027-05-01, NR35"."""

import re
from datetime import date, datetime


def itens(texto: str | None) -> list[str]:
    return [p.strip() for p in re.split(r"[,;\n]", texto or "") if p.strip()]


def nome_valor(item: str) -> tuple[str, str | None]:
    if ":" in item:
        nome, valor = item.rsplit(":", 1)
        return nome.strip(), valor.strip() or None
    return item.strip(), None


def ler_data(valor: str) -> date:
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(valor.strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f"data inválida '{valor}' (use AAAA-MM-DD ou DD/MM/AAAA)")


def especialidades(entrada: str | list[str] | None, campo_nivel: str, padrao: int) -> list[dict]:
    """'Elétrica:4, Automação' -> [{'nome': 'Elétrica', campo_nivel: 4}, {'nome': 'Automação', campo_nivel: padrao}]

    Nível fora de 1 a 5 é erro (antes 'Elétrica:99' virava 5 em silêncio e 'Elétrica:0' quebrava a validação).
    Repetidas são unificadas depois, nos schemas (fica o maior nível).
    """
    lista = itens(entrada) if isinstance(entrada, str) or entrada is None else [i.strip() for i in entrada if i.strip()]
    out = []
    for item in lista:
        nome, valor = nome_valor(item)
        if not nome:
            raise ValueError(f"especialidade sem nome em '{item}'")
        if valor is not None and not (valor.isdigit() and 1 <= int(valor) <= 5):
            raise ValueError(f"nível inválido em '{item}' (use nome:1 a nome:5)")
        out.append({"nome": nome, campo_nivel: int(valor) if valor else padrao})
    return out


def certificacoes(texto: str | None) -> list[dict]:
    out = []
    for item in itens(texto):
        nome, valor = nome_valor(item)
        out.append({"nome": nome, "validade": ler_data(valor) if valor else None})
    return out
