"""Configuração lida do ambiente (e do arquivo .env, se existir)."""

import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # não sobrescreve variáveis que já existem no ambiente


def _ids(valor: str | None) -> tuple[set[int], set[str]]:
    """Aceita IDs numéricos (recomendado) e @usuários, separados por vírgula."""
    itens = [x.strip() for x in (valor or "").replace(";", ",").split(",")]
    ids = {int(x) for x in itens if x.lstrip("-").isdigit()}
    nomes = {x.lstrip("@").lower() for x in itens if x and not x.lstrip("-").isdigit()}
    return ids, nomes


def _bool(nome: str, padrao: bool) -> bool:
    valor = os.getenv(nome)
    return padrao if valor is None or not valor.strip() else valor.strip().lower() in ("1", "true", "sim", "yes", "on")


DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./alocacao.db")

# Todas as datas do sistema são horário local do negócio, guardadas sem fuso.
APP_TIMEZONE = os.getenv("APP_TIMEZONE", "America/Sao_Paulo")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_IDS_PERMITIDOS, TELEGRAM_USERNAMES_PERMITIDOS = _ids(os.getenv("TELEGRAM_USUARIOS_PERMITIDOS"))

# Estado das conversas do agente (aprovações pendentes sobrevivem a reinícios)
AGENTE_DB = os.getenv("AGENTE_DB", "./agente.db")

# Avisos para a equipe pelo Telegram (alocado, removido, remarcado). Precisa do ID no cadastro.
AVISOS_EQUIPE = _bool("AVISOS_EQUIPE", True)
# Resumo semanal de certificações vencendo, mandado aos gestores na segunda-feira.
RESUMO_SEMANAL = _bool("RESUMO_SEMANAL", True)

# Regras de agenda do motor
DESCANSO_MIN_HORAS = float(os.getenv("DESCANSO_MIN_HORAS", "11"))  # entre jornadas (CLT art. 66); 0 desliga
DESLOCAMENTO_MIN_MINUTOS = float(os.getenv("DESLOCAMENTO_MIN_MINUTOS", "60"))  # entre campos em cidades diferentes

# Segurança do painel e da API. Vazio = sem login (uso local).
PAINEL_SENHA = os.getenv("PAINEL_SENHA") or None
API_TOKEN = os.getenv("API_TOKEN") or None


def _chave_secreta() -> str:
    """SECRET_KEY assina sessões, avisos de tela e links de calendário. Sem ela, gera uma e guarda em .secret_key."""
    if chave := os.getenv("SECRET_KEY"):
        return chave
    arquivo = Path(os.getenv("SECRET_KEY_FILE", ".secret_key"))
    try:
        if arquivo.exists():
            return arquivo.read_text().strip()
        chave = secrets.token_urlsafe(32)
        arquivo.write_text(chave)
        return chave
    except OSError:
        logging.getLogger(__name__).warning(
            "Defina SECRET_KEY no ambiente: sem ela, sessões e links de calendário mudam a cada reinício."
        )
        return secrets.token_urlsafe(32)


SECRET_KEY = _chave_secreta()
