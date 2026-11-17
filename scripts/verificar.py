"""Confere o .env: chave do Gemini, modelo, uma chamada de teste e o token do Telegram.

Uso:  python -m scripts.verificar
Não mostra nenhum segredo, só se cada item funciona.
"""

import asyncio
import sys

from langchain_core.messages import HumanMessage, SystemMessage

from app import config
from app.agente.grafo import _erro_modelo


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ok = True

    print(f"Modelo configurado: {config.GEMINI_MODEL}")
    if not config.GEMINI_API_KEY:
        print("✗ GEMINI_API_KEY vazia")
        ok = False
    else:
        from google import genai

        try:
            cliente = genai.Client(api_key=config.GEMINI_API_KEY)  # precisa ficar vivo enquanto a lista é lida
            nomes = {m.name.removeprefix("models/") for m in cliente.models.list()}
            if config.GEMINI_MODEL in nomes:
                print("✓ chave aceita e modelo disponível")
            else:
                ok = False
                leves = sorted(n for n in nomes if "flash-lite" in n or n.startswith("gemma"))
                print(f"✗ modelo não encontrado. Opções leves nesta conta: {', '.join(leves)}")
        except Exception as e:
            ok = False
            print(f"✗ chave recusada: {_erro_modelo(e)}")
            print(f"  detalhe: {type(e).__name__}: {str(e)[:250]}")

        if ok:
            from app.agente.llm import criar_interpretador

            try:
                nome, args = criar_interpretador()([
                    SystemMessage("Hoje: segunda, 2026-11-16. Funções cadastradas: tecnico."),
                    HumanMessage("quem está livre amanhã?"),
                ])
                print(f"✓ chamada de teste: {nome} {args}")
            except Exception as e:
                ok = False
                print(f"✗ chamada de teste falhou: {_erro_modelo(e)}")
                print(f"  detalhe: {str(e)[:250]}")

    if not config.TELEGRAM_BOT_TOKEN:
        print("✗ TELEGRAM_BOT_TOKEN vazio")
        ok = False
    else:
        from telegram import Bot

        async def eu():
            async with Bot(config.TELEGRAM_BOT_TOKEN) as b:
                return await b.get_me()

        try:
            print(f"✓ bot do Telegram: @{asyncio.run(eu()).username}")
        except Exception as e:
            ok = False
            print(f"✗ token do Telegram recusado: {type(e).__name__}")

    ids, users = config.TELEGRAM_IDS_PERMITIDOS, config.TELEGRAM_USERNAMES_PERMITIDOS
    if not ids and not users:
        print("✗ TELEGRAM_USUARIOS_PERMITIDOS vazio: ninguém consegue usar o bot")
        ok = False
    else:
        print(f"✓ liberados: {len(ids)} ID(s), {len(users)} @usuário(s)")
        if users:
            print("  dica: troque o @usuário pelo ID numérico (mande /id ao bot)")

    print("\nTudo certo." if ok else "\nCorrija os itens com ✗ e rode de novo.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
