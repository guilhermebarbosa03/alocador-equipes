"""Relógio do negócio. O banco guarda datas sem fuso, sempre no horário local (APP_TIMEZONE).

Use agora()/hoje() em vez de datetime.now()/date.today(): num container em UTC, depois das 21h
de Brasília o "hoje" do sistema operacional já é amanhã.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from . import config

FUSO = ZoneInfo(config.APP_TIMEZONE)


def agora() -> datetime:
    return datetime.now(FUSO).replace(tzinfo=None, microsecond=0)


def hoje() -> date:
    return agora().date()


def local(dt: datetime) -> datetime:
    """Datetime com fuso vira horário local sem fuso. Sem fuso, assume que já é horário local."""
    if dt.tzinfo is None:
        return dt
    return dt.astimezone(FUSO).replace(tzinfo=None)


def para_utc(dt: datetime) -> datetime:
    return (dt if dt.tzinfo else dt.replace(tzinfo=FUSO)).astimezone(timezone.utc)
