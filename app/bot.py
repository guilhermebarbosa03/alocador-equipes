"""Bot do Telegram. Roda por long polling: não precisa de domínio, IP público nem porta aberta.

Uso:  python -m app.bot
Precisa de TELEGRAM_BOT_TOKEN, GEMINI_API_KEY e TELEGRAM_USUARIOS_PERMITIDOS no .env.

Dois públicos:
- Gestores (TELEGRAM_USUARIOS_PERMITIDOS): o agente completo, com aprovação por botão.
- Equipe (ID do Telegram no cadastro do funcionário, em conversa privada): agenda, detalhes das
  próprias tarefas e aviso de ausência, que vai para um gestor aprovar.

Em segundo plano, o bot envia os avisos da caixa de saída (alocado, removido, remarcado), avisa os
gestores de pedidos de ausência e manda o resumo semanal de certificações vencendo.
"""

from __future__ import annotations

import asyncio
import base64
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ChatType
from telegram.error import NetworkError, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from . import config
from . import services as sv
from .agente import formatar as fmt
from .agente.equipe import AssistenteEquipe
from .agente.grafo import Agente, Resultado, criar_agente
from .agente.llm import criar_interpretador, funcionario_por_telegram
from .agente.intencoes import FERRAMENTAS_EQUIPE
from .db import SessionLocal, init_db
from .models import Funcionario, SolicitacaoAusencia

log = logging.getLogger("bot")
LIMITE = 4000  # o Telegram aceita até 4096 caracteres por mensagem
INTERVALO_AVISOS = 15  # segundos entre leituras da caixa de saída
MAX_AUDIO_SEGUNDOS = 120


def conversa_de(chat_id: int, user_id: int, privado: bool) -> str:
    """Em grupo, cada gestor tem a própria conversa: o "sim" de um não confirma o pedido do outro."""
    return f"tg-{chat_id}" if privado else f"tg-{chat_id}-{user_id}"


def _conversa(update: Update) -> str:
    chat = update.effective_chat
    return conversa_de(chat.id, update.effective_user.id, chat.type == ChatType.PRIVATE)


def _agente(ctx: ContextTypes.DEFAULT_TYPE) -> Agente:
    return ctx.application.bot_data["agente"]


def _equipe(ctx: ContextTypes.DEFAULT_TYPE) -> AssistenteEquipe:
    return ctx.application.bot_data["equipe"]


def eh_gestor(user_id: int, username: str | None) -> bool:
    return user_id in config.TELEGRAM_IDS_PERMITIDOS or (
        bool(username) and username.lower() in config.TELEGRAM_USERNAMES_PERMITIDOS
    )


def _autorizado(update: Update) -> bool:
    u = update.effective_user
    return bool(u) and eh_gestor(u.id, u.username)


def _autor(update: Update) -> str:
    u = update.effective_user
    return f"telegram:{u.full_name or u.username or u.id}"


def _funcionario_da_equipe(update: Update) -> int | None:
    """Funcionário ativo com este ID do Telegram, falando em conversa privada."""
    if update.effective_chat.type != ChatType.PRIVATE:
        return None
    with SessionLocal() as db:
        return funcionario_por_telegram(db, update.effective_user.id)


async def _negar(update: Update) -> None:
    uid = update.effective_user.id if update.effective_user else "?"
    if update.callback_query:
        await update.callback_query.answer("Sem permissão.", show_alert=True)
        return
    await update.effective_message.reply_text(
        f"Acesso não liberado. Seu ID do Telegram é {uid}.\n"
        "Gestor: peça ao administrador para incluí-lo em TELEGRAM_USUARIOS_PERMITIDOS.\n"
        "Funcionário: peça para colocarem esse ID no seu cadastro."
    )


def teclado_agente(pendente: dict, user_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(rotulo, callback_data=f"a|{pendente['id']}|{user_id}|{valor}")]
        for valor, rotulo in pendente["opcoes"]
    ])


async def _enviar(bot, chat_id: int, r: Resultado, user_id: int) -> None:
    for m in r.mensagens:
        await bot.send_message(chat_id, m[:LIMITE])
    if r.pendente:
        await bot.send_message(chat_id, r.pendente["texto"][:LIMITE], reply_markup=teclado_agente(r.pendente, user_id))


def _remetente(telegram_user_id: int) -> int | None:
    with SessionLocal() as db:
        return funcionario_por_telegram(db, telegram_user_id)


# ---------- comandos ----------


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if _autorizado(update):
        await update.message.reply_text(fmt.ajuda() + "\n\n/cancelar descarta um pedido pendente.\n/vencimentos lista NRs vencendo.")
        return
    if fid := await asyncio.to_thread(_funcionario_da_equipe, update):
        with SessionLocal() as db:
            nome = db.get(Funcionario, fid).nome
        await update.message.reply_text(fmt.ajuda_equipe(nome) + "\n\n/agenda mostra sua agenda.")
        return
    await _negar(update)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Liberado para todos: serve para descobrir o ID a colocar no .env ou no cadastro do funcionário."""
    await update.message.reply_text(f"Seu ID do Telegram é {update.effective_user.id}.")


async def cmd_cancelar(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return await _negar(update)
    houve = await asyncio.to_thread(_agente(ctx).descartar, _conversa(update))
    await update.message.reply_text("Pedido pendente descartado." if houve else "Não havia nada pendente.")


async def cmd_vencimentos(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not _autorizado(update):
        return await _negar(update)

    def texto() -> str:
        with SessionLocal() as db:
            return sv.texto_vencimentos(sv.vencimentos(db, 30))

    await update.message.reply_text((await asyncio.to_thread(texto))[:LIMITE])


async def cmd_agenda(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    fid = await asyncio.to_thread(_funcionario_da_equipe, update)
    if not fid:
        return await _negar(update)
    await update.message.reply_text((await asyncio.to_thread(_equipe(ctx).agenda, fid))[:LIMITE])


# ---------- mensagens (texto e voz) ----------


async def _audio(update: Update) -> dict | None:
    voz = update.message.voice
    if voz is None:
        return None
    if voz.duration and voz.duration > MAX_AUDIO_SEGUNDOS:
        await update.message.reply_text(f"Mande áudios de até {MAX_AUDIO_SEGUNDOS // 60} minutos, por favor.")
        return {}
    arquivo = await voz.get_file()
    dados = await arquivo.download_as_bytearray()
    return {"base64": base64.b64encode(bytes(dados)).decode("ascii"), "mime_type": voz.mime_type or "audio/ogg"}


async def ao_receber_mensagem(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    gestor = _autorizado(update)
    fid = None if gestor else await asyncio.to_thread(_funcionario_da_equipe, update)
    if not gestor and not fid:
        return await _negar(update)
    chat_id, uid = update.effective_chat.id, update.effective_user.id
    try:
        audio = await _audio(update)
        if audio == {}:
            return
        texto = update.message.text or "🎤 (mensagem de voz)"
        await ctx.bot.send_chat_action(chat_id, ChatAction.TYPING)
        if gestor:
            remetente = await asyncio.to_thread(_remetente, uid)
            r = await asyncio.to_thread(_agente(ctx).enviar, _conversa(update), texto, remetente, audio, _autor(update))
            await _enviar(ctx.bot, chat_id, r, uid)
        else:
            resp = await asyncio.to_thread(_equipe(ctx).responder, fid, texto, audio)
            for m in resp.mensagens:
                await ctx.bot.send_message(chat_id, m[:LIMITE])
            if resp.solicitacao_id:
                await _avisar_gestores_solicitacao(ctx.bot, resp.solicitacao_id)
    except Exception:
        log.exception("Erro ao processar mensagem")
        await update.message.reply_text("Tive um erro interno ao processar isso. Tente de novo ou use o painel.")


# ---------- botões ----------


async def ao_apertar_botao(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    partes = (q.data or "").split("|")
    tipo = partes[0]
    try:
        if tipo == "n" and len(partes) == 3:
            return await _botao_aviso(update, ctx, int(partes[1]), partes[2])
        if not _autorizado(update):
            return await _negar(update)
        if tipo == "s" and len(partes) == 3:
            return await _botao_solicitacao(update, ctx, int(partes[1]), partes[2] == "aprovar")
        if tipo == "i" and len(partes) == 2:
            return await _botao_imprevisto(update, ctx, int(partes[1]))
        if tipo == "a" and len(partes) >= 4:
            dono, valor = int(partes[2]), "|".join(partes[3:])
            if dono != update.effective_user.id:
                await q.answer("Esse pedido é de outra pessoa.", show_alert=True)
                return
            return await _botao_agente(update, ctx, partes[1], valor)
        if len(partes) >= 2:  # formato antigo: "id|valor"
            return await _botao_agente(update, ctx, partes[0], "|".join(partes[1:]))
    except Exception:
        log.exception("Erro ao processar botão")
        await q.message.reply_text("Tive um erro interno ao aplicar isso. Confira no painel.")
        return
    await q.answer()


def _rotulo(q, padrao: str) -> str:
    if not q.message.reply_markup:
        return padrao
    return next((b.text for linha in q.message.reply_markup.inline_keyboard for b in linha if b.callback_data == q.data),
                padrao)


async def _botao_agente(update: Update, ctx, pendente_id: str, valor: str) -> None:
    q = update.callback_query
    await q.answer()
    rotulo = _rotulo(q, valor)
    r = await asyncio.to_thread(_agente(ctx).decidir, _conversa(update), pendente_id, valor)
    if r is None:
        await q.edit_message_reply_markup(None)
        await q.message.reply_text("Esse pedido já foi resolvido ou substituído.")
        return
    await q.edit_message_text(f"{q.message.text}\n\n→ {rotulo}"[:LIMITE])  # tira os botões e mostra a escolha
    await _enviar(ctx.bot, update.effective_chat.id, r, update.effective_user.id)


async def _botao_aviso(update: Update, ctx, notificacao_id: int, resposta: str) -> None:
    """Resposta do funcionário a um aviso: ciente ou imprevisto (este chama os gestores)."""
    q = update.callback_query
    if resposta not in ("ciente", "imprevisto"):
        await q.answer()
        return

    def gravar():
        with SessionLocal() as db:
            n, a = sv.responder_notificacao(db, notificacao_id, update.effective_user.id, resposta)
            info = None
            if a is not None:
                info = {"alocacao_id": a.id, "nome": n.funcionario.nome, "tarefa": fmt.tarefa_resumo(a.tarefa)}
            return info

    try:
        info = await asyncio.to_thread(gravar)
    except sv.ErroNegocio as e:
        await q.answer(e.mensagem, show_alert=True)
        return
    await q.answer()
    await q.edit_message_text(f"{q.message.text}\n\n→ {'Ciente' if resposta == 'ciente' else 'Tive um imprevisto'}"[:LIMITE])
    if resposta != "imprevisto":
        return
    if info is None:
        await q.message.reply_text("Anotado. Essa escala já mudou, então não há o que substituir.")
        return
    await q.message.reply_text("Anotado. Avisei o gestor para encontrar um substituto.")
    texto = f"⚠ {info['nome']} avisou um imprevisto em:\n{info['tarefa']}"
    botao = InlineKeyboardMarkup([[InlineKeyboardButton("Ver substitutos", callback_data=f"i|{info['alocacao_id']}")]])
    await _para_gestores(ctx.bot, texto, botao)


async def _botao_imprevisto(update: Update, ctx, alocacao_id: int) -> None:
    q = update.callback_query
    await q.answer()
    await q.edit_message_reply_markup(None)
    r = await asyncio.to_thread(
        _agente(ctx).iniciar_substituicao, _conversa(update), [alocacao_id], "Vamos ver quem pode substituir.",
        _autor(update),
    )
    if not r.pendente:
        r.mensagens.append("Essa alocação já foi resolvida.")
    await _enviar(ctx.bot, update.effective_chat.id, r, update.effective_user.id)


async def _botao_solicitacao(update: Update, ctx, solicitacao_id: int, aprovar: bool) -> None:
    q = update.callback_query

    def decidir():
        with SessionLocal() as db:
            s = db.get(SolicitacaoAusencia, solicitacao_id)
            if s is None:
                raise sv.ErroNegocio("Pedido não encontrado", 404)
            afetadas = sv.decidir_solicitacao(db, s, aprovar, _autor(update))
            return s.funcionario.nome, [a.id for a in afetadas]

    try:
        nome, afetadas = await asyncio.to_thread(decidir)
    except sv.ErroNegocio as e:
        await q.answer(e.mensagem, show_alert=True)
        await q.edit_message_reply_markup(None)
        return
    await q.answer()
    await q.edit_message_text(f"{q.message.text}\n\n→ {'Aprovado' if aprovar else 'Recusado'}"[:LIMITE])
    await _avisar_retornos(ctx.bot)
    if afetadas:
        r = await asyncio.to_thread(
            _agente(ctx).iniciar_substituicao, _conversa(update), afetadas,
            f"Ausência de {nome} registrada. Atinge {len(afetadas)} serviço(s); vamos ver os substitutos.",
            _autor(update),
        )
        await _enviar(ctx.bot, update.effective_chat.id, r, update.effective_user.id)


# ---------- tarefas em segundo plano ----------


async def _para_gestores(bot, texto: str, teclado: InlineKeyboardMarkup | None = None) -> int:
    enviados = 0
    for gid in sorted(config.TELEGRAM_IDS_PERMITIDOS):
        try:
            await bot.send_message(gid, texto[:LIMITE], reply_markup=teclado)
            enviados += 1
        except TelegramError as e:  # gestor que nunca abriu conversa com o bot, bot bloqueado...
            log.warning("Não consegui avisar o gestor %s: %s", gid, e)
    return enviados


async def _avisar_gestores_solicitacao(bot, solicitacao_id: int) -> None:
    def dados():
        with SessionLocal() as db:
            s = db.get(SolicitacaoAusencia, solicitacao_id)
            if s is None or s.status != "pendente" or s.avisada_em:
                return None
            afetadas = sv.alocacoes_confirmadas(db, s.funcionario_id, s.inicio, s.fim)
            texto = f"📩 {s.funcionario.nome} pediu ausência ({s.motivo}): {fmt.quando(s.inicio, s.fim)}."
            if afetadas:
                texto += "\nAtinge: " + ", ".join(f"#{a.tarefa_id} {a.tarefa.titulo}" for a in afetadas)
            s.avisada_em = sv.agora()
            db.commit()
            return texto

    texto = await asyncio.to_thread(dados)
    if texto:
        teclado = InlineKeyboardMarkup([[
            InlineKeyboardButton("Aprovar", callback_data=f"s|{solicitacao_id}|aprovar"),
            InlineKeyboardButton("Recusar", callback_data=f"s|{solicitacao_id}|recusar"),
        ]])
        await _para_gestores(bot, texto, teclado)


async def _avisar_retornos(bot) -> None:
    """Conta ao funcionário a decisão sobre o pedido de ausência (decidido no bot ou no painel)."""

    def pendentes():
        with SessionLocal() as db:
            out = []
            for s in db.query(SolicitacaoAusencia).filter(SolicitacaoAusencia.status != "pendente",
                                                          SolicitacaoAusencia.retorno_em.is_(None)).limit(30):
                s.retorno_em = sv.agora()
                if (s.funcionario.telegram_id or "").lstrip("-").isdigit():
                    verbo = "aprovado" if s.status == "aprovada" else "recusado"
                    out.append((int(s.funcionario.telegram_id),
                                f"Seu pedido de ausência ({s.motivo}, {fmt.quando(s.inicio, s.fim)}) foi {verbo}."))
            db.commit()
            return out

    for chat, texto in await asyncio.to_thread(pendentes):
        try:
            await bot.send_message(chat, texto)
        except TelegramError as e:
            log.warning("Não consegui avisar o funcionário %s: %s", chat, e)


async def _enviar_avisos(bot) -> None:
    def pendentes():
        with SessionLocal() as db:
            return [(n.id, n.funcionario.telegram_id, n.texto, n.tipo) for n in sv.notificacoes_pendentes(db)]

    for nid, telegram_id, texto, tipo in await asyncio.to_thread(pendentes):
        teclado = None
        if tipo in ("alocado", "remarcado"):
            teclado = InlineKeyboardMarkup([[
                InlineKeyboardButton("✔ Ciente", callback_data=f"n|{nid}|ciente"),
                InlineKeyboardButton("⚠ Tive um imprevisto", callback_data=f"n|{nid}|imprevisto"),
            ]])
        ok = True
        try:
            await bot.send_message(int(telegram_id), texto[:LIMITE], reply_markup=teclado)
        except (TelegramError, ValueError) as e:
            ok = False
            log.warning("Aviso %s não enviado: %s", nid, e)

        def marcar(nid=nid, ok=ok):
            with SessionLocal() as db:
                sv.marcar_notificacao(db, nid, ok)

        await asyncio.to_thread(marcar)


async def _resumo_semanal(bot) -> None:
    if not config.RESUMO_SEMANAL:
        return

    def preparar():
        with SessionLocal() as db:
            if not sv.resumo_semanal_devido(db):
                return None
            itens = sv.vencimentos(db, 30)
            texto = "Resumo da semana\n\n" + sv.texto_vencimentos(itens)
            pedidos = sv.solicitacoes(db)
            if pedidos:
                texto += f"\n\n{len(pedidos)} pedido(s) de ausência esperando aprovação no painel."
            sv.marcar_resumo_semanal(db, texto)
            return texto if itens or pedidos else None

    texto = await asyncio.to_thread(preparar)
    if texto:
        await _para_gestores(bot, texto)


async def _ciclo(app: Application) -> None:
    while True:
        try:
            await _enviar_avisos(app.bot)

            def novos():
                with SessionLocal() as db:
                    return [s.id for s in sv.solicitacoes(db) if not s.avisada_em]

            for sid in await asyncio.to_thread(novos):
                await _avisar_gestores_solicitacao(app.bot, sid)
            await _avisar_retornos(app.bot)
            await _resumo_semanal(app.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Erro no ciclo de avisos")
        await asyncio.sleep(INTERVALO_AVISOS)


async def _ao_iniciar(app: Application) -> None:
    app.create_task(_ciclo(app))


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
        log.warning("TELEGRAM_USUARIOS_PERMITIDOS está vazio: o bot vai recusar todo gestor e mostrar o ID de quem tentar.")
    if config.TELEGRAM_USERNAMES_PERMITIDOS:
        log.warning(
            "Liberando por @usuário. Prefira o ID numérico (o bot mostra o seu ID ao recusar ou em /id): "
            "um @usuário pode ser trocado e reaproveitado por outra pessoa. Avisos só chegam a IDs numéricos."
        )

    init_db()
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(_ao_iniciar).build()
    app.bot_data["agente"] = criar_agente()
    app.bot_data["equipe"] = AssistenteEquipe(criar_interpretador(FERRAMENTAS_EQUIPE))
    app.add_handler(CommandHandler(["start", "ajuda"], cmd_start))
    app.add_handler(CommandHandler("cancelar", cmd_cancelar))
    app.add_handler(CommandHandler("vencimentos", cmd_vencimentos))
    app.add_handler(CommandHandler("agenda", cmd_agenda))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(MessageHandler((filters.TEXT & ~filters.COMMAND) | filters.VOICE, ao_receber_mensagem))
    app.add_handler(CallbackQueryHandler(ao_apertar_botao))
    app.add_error_handler(ao_errar)
    log.info("Bot no ar (modelo: %s). Ctrl+C para parar.", config.GEMINI_MODEL)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
