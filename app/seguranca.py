"""Cookies assinados (sessão do painel, avisos de tela), token CSRF e links de calendário.

Sem dependências extras: HMAC-SHA256 com SECRET_KEY.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from . import config


def _sig(texto: str) -> str:
    return hmac.new(config.SECRET_KEY.encode(), texto.encode(), hashlib.sha256).hexdigest()[:40]


def assinar(dados: dict, validade_s: int) -> str:
    corpo = json.dumps({**dados, "exp": int(time.time()) + validade_s}, separators=(",", ":"), ensure_ascii=False)
    b64 = base64.urlsafe_b64encode(corpo.encode()).decode().rstrip("=")
    return f"{b64}.{_sig(b64)}"


def verificar(valor: str | None) -> dict | None:
    if not valor or "." not in valor:
        return None
    b64, sig = valor.rsplit(".", 1)
    if not hmac.compare_digest(sig, _sig(b64)):
        return None
    try:
        dados = json.loads(base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)))
    except ValueError:
        return None
    if not isinstance(dados, dict) or dados.get("exp", 0) < time.time():
        return None
    return dados


def senha_confere(senha: str | None) -> bool:
    return bool(config.PAINEL_SENHA) and hmac.compare_digest((senha or "").encode(), config.PAINEL_SENHA.encode())


def token_calendario(funcionario_id: int) -> str:
    """Token fixo por funcionário para assinar a agenda (.ics) em apps de calendário sem login."""
    return _sig(f"ics:{funcionario_id}")[:24]


def token_calendario_ok(funcionario_id: int, token: str | None) -> bool:
    return bool(token) and hmac.compare_digest(token, token_calendario(funcionario_id))
