"""Bot do Telegram. Roda por long polling: não precisa de domínio, IP público nem porta aberta.

Uso:  python -m app.bot
Precisa de TELEGRAM_BOT_TOKEN, GEMINI_API_KEY e TELEGRAM_USUARIOS_PERMITIDOS no .env.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import config
from .agente import formatar as fmt
from .agente.grafo import Agente, Resultado, criar_agente
from .agente.llm import funcionario_por_telegram
from .db import SessionLocal, init_db

log = logging.getLogger("bot")
LIMITE = 4000  # o Telegram aceita até 4096 caracteres por mensagem


def _conversa(update: Update) -> str:
    return f"tg-{update.effective_chat.id}"


def _agente(ctx: ContextTypes.DEFAULT_TYPE) -> Agente:
    return ctx.application.bot_data["agente"]


def _autorizado(update: Update) -> bool:
    u = update.effective_user
    if not u:
        return False
    return u.id in config.TELEGRAM_IDS_PERMITIDOS or (
        bool(u.username) and u.username.lower() in config.TELEGRAM_USERNAMES_PERMITIDOS
    )


async def _negar(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    if update.callback_query:
        await update.callback_query.answer("Sem permissão.", show_alert=True)
        return
    await update.effective_message.reply_text(
        f"Acesso não liberado. Seu ID do Telegram é {uid}.\n"
        "Peça ao administrador para incluí-lo em TELEGRAM_USUARIOS_PERMITIDOS."
    )


def _teclado(pendente: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(rotulo, callback_data=f"{pendente['id']}|{valor}")] for valor, rotulo in pendente["opcoes"]]
    )


async def _enviar(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, r: Resultado) -> None:
    for m in r.mensagens:
        await ctx.bot.send_message(chat_id, m[:LIMITE])
    if r.pendente:
        await ctx.bot.send_message(chat_id, r.pendente["texto"][:LIMITE], reply_markup=_teclado(r.pendente))


def _remetente(telegram_user_id: int) -> int | None:
    with SessionLocal() as db:
        return funcionario_por_telegram(db, telegram_user_id)


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return await _negar(update)
    await update.message.reply_text(fmt.ajuda() + "\n\n/cancelar descarta um pedido pendente.")


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Liberado para todos: serve para descobrir o ID a colocar em TELEGRAM_USUARIOS_PERMITIDOS."""
    await update.message.reply_text(f"Seu ID do Telegram é {update.effective_user.id}.")


async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return await _negar(update)
    houve = await asyncio.to_thread(_agente(ctx).descartar, _conversa(update))
    await update.message.reply_text("Pedido pendente descartado." if houve else "Não havia nada pendente.")


async def ao_receber_texto(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return await _negar(update)
    chat_id = update.effective_chat.id
    await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        remetente = await asyncio.to_thread(_remetente, update.effective_user.id)
        r = await asyncio.to_thread(_agente(ctx).enviar, _conversa(update), update.message.text, remetente)
    except Exception:
        log.exception("Erro ao processar mensagem")
        await update.message.reply_text("Tive um erro interno ao processar isso. Tente de novo ou use o painel.")
        return
    await _enviar(ctx, chat_id, r)


async def ao_apertar_botao(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not _autorizado(update):
        return await _negar(update)
    await q.answer()
    pendente_id, _, valor = (q.data or "").partition("|")
    rotulo = next(
        (b.text for linha in q.message.reply_markup.inline_keyboard for b in linha if b.callback_data == q.data),
        valor,
    ) if q.message.reply_markup else valor

    try:
        r = await asyncio.to_thread(_agente(ctx).decidir, _conversa(update), pendente_id, valor)
    except Exception:
        log.exception("Erro ao processar botão")
        await q.message.reply_text("Tive um erro interno ao aplicar isso. Confira no painel.")
        return
    if r is None:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Esse pedido já foi resolvido ou substituído.")
        return
    await q.edit_message_text(f"{q.message.text}\n\n→ {rotulo}"[:LIMITE])  # tira os botões e mostra a escolha
    await _enviar(ctx, update.effective_chat.id, r)


async def ao_errar(update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if isinstance(ctx.error, NetworkError):  # queda de rede: a biblioteca tenta de novo sozinha
        log.warning("Falha de rede com o Telegram (%s); tentando de novo.", ctx.error)
        return
    log.error("Erro no bot", exc_info=ctx.error)


def main() -> None:
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Defina TELEGRAM_BOT_TOKEN no .env (crie o bot com o @BotFather no Telegram).")
    if not (config.TELEGRAM_IDS_PERMITIDOS or config.TELEGRAM_USERNAMES_PERMITIDOS):
        log.warning("TELEGRAM_USUARIOS_PERMITIDOS está vazio: o bot vai recusar todo mundo e mostrar o ID de quem tentar.")
    if config.TELEGRAM_USERNAMES_PERMITIDOS:
        log.warning(
            "Liberando por @usuário. Prefira o ID numérico (o bot mostra o seu ID ao recusar ou em /id): "
            "um @usuário pode ser trocado e reaproveitado por outra pessoa."
        )

    init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    app.bot_data["agente"] = criar_agente()
    app.add_handler(CommandHandler(["start", "ajuda"], cmd_start))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, ao_receber_texto))
    app.add_handler(CallbackQueryHandler(ao_apertar_botao))
    app.add_error_handler(ao_errar)
    log.info("Bot no ar (modelo: %s). Ctrl+C para parar.", config.GEMINI_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
