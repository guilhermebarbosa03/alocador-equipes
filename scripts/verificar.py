"""Confere o .env: provedor de IA, chave, modelo, uma chamada de teste e o token do Telegram.

Uso:  python -m scripts.verificar
Não mostra nenhum segredo, só se cada item funciona.
"""

import asyncio
import sys

from langchain_core.messages import HumanMessage, SystemMessage

from app import config, provedores
from app.provedores import explicar_erro


def _conferir_gemini(p: provedores.Provedor) -> bool:
    """Só o Gemini lista os modelos da conta: ajuda a achar um nome válido para GEMINI_MODEL."""
    from google import genai

    try:
        cliente = genai.Client(api_key=p.chave)  # precisa ficar vivo enquanto a lista é lida
        nomes = {m.name.removeprefix("models/") for m in cliente.models.list()}
    except Exception as e:
        print(f"✗ chave recusada: {explicar_erro(e)}")
        print(f"  detalhe: {type(e).__name__}: {str(e)[:250]}")
        return False
    if p.modelo in nomes:
        print("✓ chave aceita e modelo disponível")
        return True
    leves = sorted(n for n in nomes if "flash-lite" in n or n.startswith("gemma"))
    print(f"✗ modelo não encontrado. Opções leves nesta conta: {', '.join(leves)}")
    return False


def _conferir_ia() -> bool:
    try:
        p = provedores.atual()
    except RuntimeError as e:
        print(f"✗ {e}")
        return False
    print(f"Modelo de IA: {p.rotulo} · {p.modelo} (LLM_PROVIDER={config.LLM_PROVIDER})")
    if not p.configurado:
        print(f"✗ {p.var_chave} vazia. Crie uma chave em {p.onde_criar_chave}")
        return False
    if p.rotulo == "Gemini" and not _conferir_gemini(p):
        return False

    from app.agente.llm import criar_interpretador

    try:
        nome, args = criar_interpretador()([
            SystemMessage("Hoje: segunda, 2026-11-16. Funções cadastradas: tecnico."),
            HumanMessage("quem está livre amanhã?"),
        ])
    except Exception as e:
        print(f"✗ chamada de teste falhou: {str(e) if isinstance(e, RuntimeError) else explicar_erro(e)}")
        print(f"  detalhe: {type(e).__name__}: {str(e)[:250]}")
        return False
    print(f"✓ chamada de teste: {nome} {args}")
    if not p.aceita_audio:
        print(f"  aviso: o {p.rotulo} não ouve áudio; o bot vai pedir que mandem texto no lugar de mensagens de voz")
    return True


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    ok = _conferir_ia()

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
