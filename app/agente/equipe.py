"""Modo equipe: o próprio funcionário fala com o bot (agenda, detalhes da tarefa, avisar ausência).

Uma chamada ao modelo por mensagem, sem histórico e com ferramentas limitadas. O modelo só vê os
dados da própria pessoa. Ausência informada aqui não é gravada direto: vira um pedido que um
gestor aprova (pelo bot ou pelo painel).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from .. import services as sv
from ..db import SessionLocal
from ..models import Funcionario, Tarefa
from ..relogio import agora as agora_local
from . import formatar as fmt
from ..provedores import explicar_erro
from .grafo import _msg
from .intencoes import POR_NOME_EQUIPE, InformarAusencia, MinhaAgenda, MinhaTarefa, _data, resolver_periodo
from .llm import Interpretador, com_audio, montar_contexto_equipe

log = logging.getLogger(__name__)


@dataclass
class RespostaEquipe:
    mensagens: list[str]
    solicitacao_id: int | None = None  # pedido de ausência criado: o bot avisa os gestores


class AssistenteEquipe:
    def __init__(self, interpretador: Interpretador, agora: Callable[[], datetime] = agora_local):
        self.interpretador = interpretador
        self.agora = agora

    def responder(self, funcionario_id: int, texto: str, audio: dict | None = None) -> RespostaEquipe:
        with SessionLocal() as db:
            f = db.get(Funcionario, funcionario_id)
            if f is None or not f.ativo:
                return RespostaEquipe(["Seu cadastro não está ativo. Fale com o gestor."])
            contexto = montar_contexto_equipe(db, self.agora(), f)
            nome_pessoa = f.nome
        try:
            nome, args = self.interpretador(com_audio([SystemMessage(contexto), HumanMessage(texto)], audio))
        except Exception as err:
            log.exception("Falha ao chamar o modelo (modo equipe)")
            return RespostaEquipe([explicar_erro(err)])

        classe = POR_NOME_EQUIPE.get(nome)
        if classe is None or nome == "ResponderEquipe":
            return RespostaEquipe([(args or {}).get("texto") or fmt.ajuda_equipe(nome_pessoa)])
        try:
            a = classe.model_validate(args)
        except ValidationError:
            return RespostaEquipe(["Não entendi direito. Pode dizer de outro jeito?"])

        with SessionLocal() as db:
            f = db.get(Funcionario, funcionario_id)
            try:
                if isinstance(a, MinhaAgenda):
                    return RespostaEquipe([self._agenda(db, f, a.dias)])
                if isinstance(a, MinhaTarefa):
                    return RespostaEquipe([self._tarefa(db, f, a)])
                if isinstance(a, InformarAusencia):
                    return self._ausencia(db, f, a)
            except (ValueError, sv.ErroNegocio) as err:
                db.rollback()
                return RespostaEquipe([f"Não deu: {_msg(err)}"])
        return RespostaEquipe([fmt.ajuda_equipe(nome_pessoa)])

    def agenda(self, funcionario_id: int, dias: int = 14) -> str:
        with SessionLocal() as db:
            return self._agenda(db, db.get(Funcionario, funcionario_id), dias)

    def _agenda(self, db, f: Funcionario, dias: int) -> str:
        inicio = datetime.combine(self.agora().date(), time.min)
        eventos = sv.agenda(db, f, inicio, inicio + timedelta(days=dias))
        linhas = []
        for ev in eventos:
            if ev["tipo"] == "tarefa" and ev["status"] == "confirmada":
                local = f" · {ev['local']}" if ev.get("local") else ""
                linhas.append(f"• #{ev['id']} {ev['titulo']} · {fmt.quando(ev['inicio'], ev['fim'])}{local}")
            elif ev["tipo"] == "indisponibilidade":
                linhas.append(f"• ausência ({ev['titulo']}) · {fmt.quando(ev['inicio'], ev['fim'])}")
        if not linhas:
            return f"Você não tem nada confirmado nos próximos {dias} dias."
        return f"Sua agenda (próximos {dias} dias):\n" + "\n".join(linhas)

    def _tarefa(self, db, f: Funcionario, a: MinhaTarefa) -> str:
        inicio = datetime.combine(self.agora().date(), time.min) - timedelta(days=1)
        minhas = [sv.obter_tarefa(db, ev["id"]) for ev in sv.agenda(db, f, inicio, inicio + timedelta(days=90))
                  if ev["tipo"] == "tarefa" and ev["status"] == "confirmada"]
        escolhida: Tarefa | None = None
        if a.tarefa_id is not None:
            escolhida = next((t for t in minhas if t.id == a.tarefa_id), None)
            if escolhida is None:
                return f"Você não está escalado(a) na tarefa #{a.tarefa_id}."
        elif a.data:
            dia = _data(a.data)
            ini, fim = datetime.combine(dia, time.min), datetime.combine(dia + timedelta(days=1), time.min)
            escolhida = next((t for t in minhas if t.inicio < fim and t.fim > ini), None)
            if escolhida is None:
                return f"Você não tem tarefa confirmada em {dia:%d/%m}."
        else:
            escolhida = next((t for t in minhas if t.fim >= self.agora()), None)
            if escolhida is None:
                return "Você não tem tarefa confirmada pela frente."
        t = escolhida
        linhas = [fmt.tarefa_resumo(t)]
        if t.descricao:
            linhas.append(t.descricao)
        if t.contrato:
            linhas.append(f"Contrato: {t.contrato}")
        equipe = [x.funcionario_nome for x in t.alocacoes if x.status == "confirmada" and x.funcionario_id != f.id]
        linhas.append(f"Equipe: {', '.join(equipe) if equipe else 'só você'}")
        return "\n".join(linhas)

    def _ausencia(self, db, f: Funcionario, a: InformarAusencia) -> RespostaEquipe:
        inicio, fim = resolver_periodo(a, dia_inteiro=True)
        s = sv.criar_solicitacao(db, f, inicio, fim, a.motivo)
        afetadas = sv.alocacoes_confirmadas(db, f.id, inicio, fim)
        texto = f"Pedido enviado ao gestor: ausência ({a.motivo}) {fmt.quando(inicio, fim)}."
        if afetadas:
            texto += "\nVocê está escalado(a) em: " + ", ".join(f"#{x.tarefa_id} {x.tarefa.titulo}" for x in afetadas)
            texto += ". O gestor vai ver quem pode substituir."
        texto += "\nVocê recebe a resposta por aqui."
        return RespostaEquipe([texto], s.id)
