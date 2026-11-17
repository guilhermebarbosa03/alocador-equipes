"""Grafo do agente (LangGraph).

    interpretar ─┬─> consultar ──────────────────────────────> fim
                 ├─> preparar ─┬─> aprovar (pausa) ─> executar ─┬─> fim
                 │             └───────────────────────────────>┴─> substituir (pausa, 1 por vez) ─> fim
                 └─> fim (resposta direta / pergunta de esclarecimento)

Só "interpretar" chama o modelo. Tudo que grava passa por "aprovar" ou "substituir",
que pausam o grafo com interrupt() até o gestor escolher um botão.
"""

from __future__ import annotations

import logging
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Annotated, Any, Callable, TypedDict

from langchain_core.messages import AIMessage, AnyMessage, HumanMessage, RemoveMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, interrupt
from pydantic import ValidationError

from .. import config, parsing, schemas
from .. import services as sv
from ..allocation import disponiveis, norm, propor
from ..db import SessionLocal
from ..models import Alocacao
from . import formatar as fmt
from .intencoes import CONSULTA, ESCRITA, POR_NOME, resolver_periodo
from .llm import Interpretador, mensagens_para_modelo, montar_contexto

log = logging.getLogger(__name__)
MAX_HISTORICO = 20


class Estado(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    remetente_id: int | None
    acao: str | None
    args: dict | None
    previa: dict | None
    decisao: str | None
    rota: str | None
    pendencias: list[int]
    resposta: str | None


def _saida(texto: str, no_historico: bool = True) -> dict:
    out: dict[str, Any] = {"resposta": texto}
    if no_historico:
        out["messages"] = [AIMessage(texto)]
    return out


def _msg(err: Exception) -> str:
    if isinstance(err, sv.ErroNegocio):
        return err.mensagem
    if isinstance(err, ValidationError):
        return "; ".join(e["msg"] for e in err.errors())
    return str(err)


def _erro_modelo(err: Exception) -> str:
    texto = str(err)
    if "402" in texto or "prepayment" in texto.lower():
        return (
            "A chave do Gemini é de um projeto com faturamento pré-pago e sem créditos. "
            "Crie uma chave num projeto SEM faturamento (plano gratuito) no AI Studio e troque GEMINI_API_KEY no .env."
        )
    if any(k in texto for k in ("429", "RESOURCE_EXHAUSTED", "quota")):
        return "Atingi o limite gratuito do Gemini. Tente de novo em alguns minutos (ou use o painel)."
    if any(k in texto for k in ("API_KEY_INVALID", "API key not valid", "PERMISSION_DENIED", "401", "403")):
        return "A chave do Gemini foi recusada. Confira GEMINI_API_KEY no .env."
    if "404" in texto or "not found" in texto.lower():
        return f"O modelo '{config.GEMINI_MODEL}' não foi encontrado. Ajuste GEMINI_MODEL no .env."
    return "Não consegui falar com o modelo de IA agora. Tente de novo em instantes."


def construir_grafo(interpretador: Interpretador, checkpointer=None, agora: Callable[[], datetime] = datetime.now):
    # ---------- interpretar (única chamada ao modelo) ----------

    def no_interpretar(e: Estado) -> dict:
        reset: dict[str, Any] = {"acao": None, "args": None, "previa": None, "decisao": None, "rota": None,
                                 "pendencias": [], "resposta": None}
        antigas = e["messages"][:-MAX_HISTORICO]
        limpeza = [RemoveMessage(id=m.id) for m in antigas if m.id]

        with SessionLocal() as db:
            contexto = montar_contexto(db, agora(), e.get("remetente_id"))
        try:
            nome, args = interpretador(mensagens_para_modelo(contexto, e["messages"]))
        except Exception as err:  # rede, cota, chave inválida...
            log.exception("Falha ao chamar o modelo")
            return {**reset, "messages": limpeza, **_saida(_erro_modelo(err), no_historico=False)}

        saida: dict[str, Any] = {}
        if nome == "Responder" or nome not in POR_NOME:
            saida = _saida((args or {}).get("texto") or "Não entendi. Pode reformular?")
        else:
            try:
                reset.update(acao=nome, args=POR_NOME[nome].model_validate(args).model_dump())
            except ValidationError:
                log.warning("Argumentos inválidos do modelo para %s: %s", nome, args)
                saida = _saida("Não entendi todos os dados do pedido. Pode detalhar?")
        return {**reset, **saida, "messages": limpeza + saida.get("messages", [])}

    def rota_interpretar(e: Estado) -> str:
        if e.get("acao") in CONSULTA:
            return "consultar"
        if e.get("acao") in ESCRITA:
            return "preparar"
        return END

    # ---------- consultas (só leitura, sem aprovação) ----------

    def no_consultar(e: Estado) -> dict:
        a = POR_NOME[e["acao"]].model_validate(e["args"])
        with SessionLocal() as db:
            try:
                return _saida(_consultar(db, e["acao"], a))
            except (ValueError, sv.ErroNegocio) as err:
                return _saida(f"Não deu: {_msg(err)}")

    def _consultar(db, nome: str, a) -> str:
        if nome == "ConsultarDisponibilidade":
            inicio, fim = resolver_periodo(a)
            if a.papel:
                _papel_cadastrado(db, a.papel)
            livres = disponiveis(db, inicio, fim, a.papel)
            if not livres:
                return f"Ninguém livre em {fmt.quando(inicio, fim)}."
            linhas = [f"• {x.funcionario.nome} ({x.funcionario.papel})" for x in livres]
            return f"Livres em {fmt.quando(inicio, fim)}:\n" + "\n".join(linhas)

        if nome == "ConsultarAgenda":
            f = sv.obter_funcionario(db, a.funcionario_id)
            hoje = datetime.combine(agora().date(), time.min)
            eventos = sv.agenda(db, f, hoje, hoje + timedelta(days=a.dias))
            if not eventos:
                return f"{f.nome} não tem nada nos próximos {a.dias} dias."
            linhas = []
            for ev in eventos:
                if ev["tipo"] == "tarefa":
                    st = "" if ev["status"] == "confirmada" else f" ({ev['status']})"
                    linhas.append(f"• #{ev['id']} {ev['titulo']} · {fmt.quando(ev['inicio'], ev['fim'])}{st}")
                else:
                    linhas.append(f"• ausência: {ev['titulo']} · {fmt.quando(ev['inicio'], ev['fim'])}")
            return f"Agenda de {f.nome}:\n" + "\n".join(linhas)

        if nome == "VerTarefa":
            return fmt.tarefa_detalhe(sv.obter_tarefa(db, a.tarefa_id))

        if nome == "ListarTarefas":
            tarefas = sorted(sv.listar_tarefas(db), key=lambda t: t.inicio)
            if not a.incluir_encerradas:
                tarefas = [t for t in tarefas if t.status not in sv.ENCERRADAS]
            if not tarefas:
                return "Nenhuma tarefa em andamento."
            return "\n".join(f"• #{t.id} {t.titulo} · {fmt.quando(t.inicio, t.fim)} · {t.status}" for t in tarefas)

        raise ValueError(f"consulta desconhecida: {nome}")

    def _papel_cadastrado(db, papel: str) -> str:
        papeis = {norm(f.papel): f.papel for f in sv.listar_funcionarios(db)}
        if norm(papel) not in papeis:
            raise ValueError(f"não conheço a função '{papel}'. Funções: {', '.join(sorted(papeis.values()))}")
        return papeis[norm(papel)]

    # ---------- preparar (monta a prévia, não grava) ----------

    def no_preparar(e: Estado) -> dict:
        nome = e["acao"]
        a = POR_NOME[nome].model_validate(e["args"])
        with SessionLocal() as db:
            try:
                return _preparar(db, nome, a)
            except (ValueError, ValidationError, sv.ErroNegocio) as err:
                return {"rota": "fim", **_saida(f"Não consegui montar o pedido: {_msg(err)}")}

    def _previa(texto: str, opcoes: list[list[str]], **dados) -> dict:
        previa = {"texto": texto, "opcoes": opcoes, "cancelar": "descartar", "sim": opcoes[0][0], **dados}
        # a prévia entra no histórico para o modelo entender correções ("na verdade são 3")
        return {"rota": "aprovar", "previa": previa, "messages": [AIMessage(texto)]}

    def _preparar(db, nome: str, a) -> dict:
        if nome == "CriarTarefa":
            inicio, fim = resolver_periodo(a)
            requisitos = [
                {
                    "papel": _papel_cadastrado(db, v.papel),
                    "quantidade": v.quantidade,
                    "especialidades": parsing.especialidades(v.especialidades, "nivel_min", 1),
                    "certificacoes": v.certificacoes,
                    "desejaveis": v.desejaveis,
                }
                for v in a.equipe
            ]
            dados = schemas.TarefaIn(
                titulo=a.titulo, tipo=a.tipo, local=a.local, contrato=a.contrato, descricao=a.descricao,
                inicio=inicio, fim=fim, requisitos=requisitos,
            )
            prop = sv.simular_proposta(db, dados)
            local = f" · {a.local}" if a.local else ""
            texto = f"Novo serviço: {a.titulo}\n{fmt.quando(inicio, fim)} · {a.tipo}{local}\n\n{fmt.proposta(prop)}"
            if not prop.completa:
                texto += "\n\n⚠ A equipe ficaria incompleta."
            opcoes = [["criar_confirmar", "Criar e confirmar equipe"]] if any(v.escolhidos for v in prop.vagas) else []
            opcoes += [["so_criar", "Só criar o serviço"], ["descartar", "Descartar"]]
            return _previa(texto, opcoes, dados=dados.model_dump(mode="json"))

        if nome == "AlocarTarefa":
            t = sv.obter_tarefa(db, a.tarefa_id)
            if t.status in sv.ENCERRADAS:
                raise sv.ErroNegocio(f"a tarefa #{t.id} está {t.status}")
            if t.status == "alocada":
                return {"rota": "fim", **_saida(f"A equipe já está completa.\n{fmt.tarefa_detalhe(t)}")}
            prop = propor(db, t)
            texto = f"{fmt.tarefa_resumo(t)}\n\n{fmt.proposta(prop)}"
            if not any(v.escolhidos for v in prop.vagas):
                return {"rota": "fim", **_saida(f"Não há ninguém viável para completar:\n{texto}")}
            return _previa(texto, [["confirmar", "Confirmar equipe"], ["descartar", "Descartar"]], tarefa_id=t.id)

        if nome == "RegistrarAusencia":
            f = sv.obter_funcionario(db, a.funcionario_id)
            inicio, fim = resolver_periodo(a, dia_inteiro=True)
            texto = f"Registrar ausência de {f.nome} ({a.motivo}): {fmt.quando(inicio, fim)}."
            afetadas = sv.alocacoes_confirmadas(db, f.id, inicio, fim)
            if afetadas:
                texto += "\nIsso atinge:\n" + "\n".join(
                    f"• #{x.tarefa.id} {x.tarefa.titulo} · {fmt.quando(x.tarefa.inicio, x.tarefa.fim)}" for x in afetadas
                )
                texto += "\nDepois de registrar, sugiro substitutos."
            return _previa(
                texto, [["registrar", "Registrar"], ["descartar", "Descartar"]],
                funcionario_id=f.id, inicio=inicio.isoformat(), fim=fim.isoformat(), motivo=a.motivo,
            )

        if nome == "TirarDaTarefa":
            t = sv.obter_tarefa(db, a.tarefa_id)
            f = sv.obter_funcionario(db, a.funcionario_id)
            aloc = next((x for x in t.alocacoes if x.funcionario_id == f.id and x.status == "confirmada"), None)
            if aloc is None:
                raise sv.ErroNegocio(f"{f.nome} não está confirmado(a) na tarefa #{t.id}")
            return {"rota": "substituir", "pendencias": [aloc.id]}

        raise ValueError(f"ação desconhecida: {nome}")

    def rota_preparar(e: Estado) -> str:
        return {"aprovar": "aprovar", "substituir": "substituir"}.get(e.get("rota") or "", END)

    # ---------- aprovar (pausa até o gestor decidir) ----------

    def no_aprovar(e: Estado) -> dict:
        p = e["previa"]
        decisao = interrupt({
            "id": uuid.uuid4().hex[:8], "texto": p["texto"], "opcoes": p["opcoes"],
            "cancelar": p["cancelar"], "sim": p["sim"],
        })
        return {"decisao": decisao}

    # ---------- executar ----------

    def no_executar(e: Estado) -> dict:
        nome, p, d = e["acao"], e["previa"], e.get("decisao")
        if d == p["cancelar"] or d not in {o[0] for o in p["opcoes"]}:
            return {"rota": "fim", **_saida("Ok, descartado. Nada foi gravado.")}
        with SessionLocal() as db:
            try:
                return _executar(db, nome, p, d)
            except (ValueError, ValidationError, sv.ErroNegocio) as err:
                db.rollback()
                return {"rota": "fim", **_saida(f"Não deu: {_msg(err)}")}

    def _confirmar_equipe(db, t) -> str:
        sv.gerar_proposta(db, t)
        novas = sv.confirmar(db, t) if any(x.status == "proposta" for x in t.alocacoes) else []
        texto = fmt.equipe_confirmada(t, novas)
        if t.status != "alocada":
            texto += f"\n⚠ Equipe incompleta. Quando houver gente, peça \"completa a tarefa {t.id}\"."
        return texto

    def _executar(db, nome: str, p: dict, d: str) -> dict:
        if nome == "CriarTarefa":
            t = sv.criar_tarefa(db, schemas.TarefaIn.model_validate(p["dados"]))
            if d == "criar_confirmar":
                return {"rota": "fim", **_saida(f"Serviço #{t.id} criado.\n{_confirmar_equipe(db, t)}")}
            return {"rota": "fim", **_saida(f"Serviço #{t.id} criado, sem equipe por enquanto.")}

        if nome == "AlocarTarefa":
            return {"rota": "fim", **_saida(_confirmar_equipe(db, sv.obter_tarefa(db, p["tarefa_id"])))}

        if nome == "RegistrarAusencia":
            f = sv.obter_funcionario(db, p["funcionario_id"])
            _, afetadas = sv.criar_indisponibilidade(
                db, f, schemas.IndisponibilidadeIn(inicio=p["inicio"], fim=p["fim"], motivo=p["motivo"])
            )
            texto = f"Ausência de {f.nome} registrada."
            if afetadas:
                texto += f" Atinge {len(afetadas)} serviço(s); vamos ver os substitutos."
            return {"rota": "fim", "pendencias": [x.id for x in afetadas], **_saida(texto)}

        raise ValueError(f"ação desconhecida: {nome}")

    # ---------- substituir (uma pendência por vez) ----------

    def no_substituir(e: Estado) -> dict:
        pend = list(e.get("pendencias") or [])
        if not pend:
            return {}
        aloc_id, resto = pend[0], pend[1:]

        with SessionLocal() as db:
            aloc = db.get(Alocacao, aloc_id)
            if aloc is None or aloc.status != "confirmada" or aloc.tarefa.status in sv.ENCERRADAS:
                return {"pendencias": resto}
            nome, tarefa_id = aloc.funcionario_nome, aloc.tarefa_id
            sub, vaga = sv.sugerir_substituto(db, aloc)
            texto = f"{nome} está em {fmt.tarefa_resumo(aloc.tarefa)}"
            if sub:
                texto += f"\n\nSugestão de substituto:\n✔ {fmt.candidato(sub)}"
                if vaga and vaga.alternativas:
                    texto += "\nOutras opções: " + ", ".join(
                        f"{x.funcionario.nome} [{x.score:g}]" for x in vaga.alternativas[:2]
                    )
                opcoes = [[f"trocar:{sub.funcionario.id}", f"Trocar por {sub.funcionario.nome}"]]
            else:
                texto += "\n\nNenhum substituto viável."
                for x in (vaga.rejeitados if vaga else [])[:3]:
                    texto += f"\n· {fmt.rejeitado(x)}"
                opcoes = []
            opcoes += [["retirar", f"Só retirar {nome}"], ["manter", f"Manter {nome}"]]

        decisao = interrupt({
            "id": uuid.uuid4().hex[:8], "texto": texto, "opcoes": opcoes,
            "cancelar": "manter", "sim": opcoes[0][0] if sub else None,
        })

        with SessionLocal() as db:
            aloc = db.get(Alocacao, aloc_id)
            try:
                if decisao.startswith("trocar:"):
                    nova = sv.substituir(db, aloc, int(decisao.split(":", 1)[1]))
                    msg = f"Feito: {nova.funcionario_nome} entrou no lugar de {nome} na #{tarefa_id}."
                elif decisao == "retirar":
                    sv.cancelar_alocacao(db, aloc)
                    msg = f"{nome} saiu da #{tarefa_id}. A vaga ficou aberta."
                else:
                    msg = f"Mantido: {nome} continua na #{tarefa_id}."
            except (ValueError, sv.ErroNegocio) as err:
                db.rollback()
                msg = f"Não deu: {_msg(err)}"
        return {"pendencias": resto, **_saida(msg)}

    def rota_pendencias(e: Estado) -> str:
        return "substituir" if e.get("pendencias") else END

    g = StateGraph(Estado)
    g.add_node("interpretar", no_interpretar)
    g.add_node("consultar", no_consultar)
    g.add_node("preparar", no_preparar)
    g.add_node("aprovar", no_aprovar)
    g.add_node("executar", no_executar)
    g.add_node("substituir", no_substituir)
    g.add_edge(START, "interpretar")
    g.add_conditional_edges("interpretar", rota_interpretar, ["consultar", "preparar", END])
    g.add_edge("consultar", END)
    g.add_conditional_edges("preparar", rota_preparar, ["aprovar", "substituir", END])
    g.add_edge("aprovar", "executar")
    g.add_conditional_edges("executar", rota_pendencias, ["substituir", END])
    g.add_conditional_edges("substituir", rota_pendencias, ["substituir", END])
    return g.compile(checkpointer=checkpointer)


# ---------- interface usada pelo bot, pela CLI e pelos testes ----------

SIM = {"sim", "s", "ok", "confirma", "confirmar", "confirmado", "pode", "isso", "manda", "bora", "beleza"}
NAO = {"nao", "n", "cancela", "cancelar", "descarta", "descartar", "deixa", "esquece"}


@dataclass
class Resultado:
    mensagens: list[str]
    pendente: dict | None  # {"id", "texto", "opcoes": [[valor, rótulo]], "cancelar", "sim"}


def resposta_rapida(texto: str, pendente: dict) -> str | None:
    """'sim', 'não' ou o número da opção, digitados em vez de apertar o botão."""
    t = norm(texto).strip(" .!")
    if t in SIM and pendente.get("sim"):
        return pendente["sim"]
    if t in NAO:
        return pendente["cancelar"]
    if t.isdigit() and 1 <= int(t) <= len(pendente["opcoes"]):
        return pendente["opcoes"][int(t) - 1][0]
    return None


class Agente:
    def __init__(self, grafo):
        self.grafo = grafo

    @staticmethod
    def _cfg(conversa: str) -> dict:
        return {"configurable": {"thread_id": str(conversa)}}

    def pendente(self, conversa: str) -> dict | None:
        estado = self.grafo.get_state(self._cfg(conversa))
        for tarefa in estado.tasks:
            for itr in tarefa.interrupts:
                return itr.value
        return None

    def _rodar(self, entrada, conversa: str) -> Resultado:
        mensagens, pendente = [], None
        for atualizacao in self.grafo.stream(entrada, self._cfg(conversa), stream_mode="updates"):
            for no, dados in atualizacao.items():
                if no == "__interrupt__":
                    pendente = dados[0].value
                elif isinstance(dados, dict) and dados.get("resposta"):
                    mensagens.append(dados["resposta"])
        return Resultado(mensagens, pendente)

    def enviar(self, conversa: str, texto: str, remetente_id: int | None = None) -> Resultado:
        avisos = []
        atual = self.pendente(conversa)
        if atual:
            rapida = resposta_rapida(texto, atual)
            if rapida is not None:
                return self._rodar(Command(resume=rapida), conversa)
            if self.descartar(conversa):
                avisos.append("(O pedido anterior, que esperava confirmação, foi descartado.)")
        r = self._rodar({"messages": [HumanMessage(texto)], "remetente_id": remetente_id}, conversa)
        r.mensagens = avisos + r.mensagens
        return r

    def decidir(self, conversa: str, pendente_id: str, valor: str) -> Resultado | None:
        """Resposta a um botão. None se o botão é de um pedido que já foi resolvido."""
        atual = self.pendente(conversa)
        if not atual or atual["id"] != pendente_id:
            return None
        return self._rodar(Command(resume=valor), conversa)

    def descartar(self, conversa: str) -> bool:
        houve = False
        for _ in range(50):
            atual = self.pendente(conversa)
            if not atual:
                break
            self._rodar(Command(resume=atual["cancelar"]), conversa)
            houve = True
        return houve


def criar_agente(interpretador: Interpretador | None = None, checkpointer=None) -> Agente:
    if interpretador is None:
        from .llm import criar_interpretador

        interpretador = criar_interpretador()
    if checkpointer is None:
        from langgraph.checkpoint.sqlite import SqliteSaver

        checkpointer = SqliteSaver(sqlite3.connect(config.AGENTE_DB, check_same_thread=False))
    return Agente(construir_grafo(interpretador, checkpointer))
