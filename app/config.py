"""Configuração lida do ambiente (e do arquivo .env, se existir)."""

import os

from dotenv import load_dotenv

load_dotenv()  # não sobrescreve variáveis que já existem no ambiente

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./alocacao.db")

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
# Aceita IDs numéricos (recomendado) e @usuários, separados por vírgula.
_permitidos = [x.strip() for x in os.getenv("TELEGRAM_USUARIOS_PERMITIDOS", "").replace(";", ",").split(",")]
TELEGRAM_IDS_PERMITIDOS = {int(x) for x in _permitidos if x.lstrip("-").isdigit()}
TELEGRAM_USERNAMES_PERMITIDOS = {x.lstrip("@").lower() for x in _permitidos if x and not x.lstrip("-").isdigit()}

# Estado das conversas do agente (aprovações pendentes sobrevivem a reinícios)
AGENTE_DB = os.getenv("AGENTE_DB", "./agente.db")
