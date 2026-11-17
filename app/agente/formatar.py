"""Textos que o agente manda. Feitos em código (não pelo modelo) para economizar cota e não inventar nada."""

from __future__ import annotations

from datetime import datetime, time, timedelta

from ..allocation import Avaliacao, Proposta, Vaga
from ..models import Alocacao, Tarefa

DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def quando(inicio: datetime, fim: datetime) -> str:
    def dia(d: datetime) -> str:
        return f"{DIAS[d.weekday()]} {d:%d/%m}"

    if inicio.date() == fim.date():
        return f"{dia(inicio)}, {inicio:%H:%M}–{fim:%H:%M}"
    if inicio.time() == fim.time() == time.min:  # dias inteiros (ausências)
        ultimo = fim - timedelta(days=1)
        if ultimo.date() == inicio.date():
            return f"{dia(inicio)} (dia inteiro)"
        return f"{dia(inicio)} a {dia(ultimo)} (dias inteiros)"
    return f"{dia(inicio)} {inicio:%H:%M} → {dia(fim)} {fim:%H:%M}"


def exigencias(r) -> str:
    partes = [f"{e['nome']}≥{e.get('nivel_min', 1)}" for e in (r.especialidades or [])]
    partes += list(r.certificacoes or [])
    if r.desejaveis:
        partes.append("desejável: " + ", ".join(r.desejaveis))
    return f" ({', '.join(partes)})" if partes else ""


def candidato(a: Avaliacao) -> str:
    return f"{a.funcionario.nome} [{a.score:g}] {a.detalhes.get('resumo', '')}"


def rejeitado(a: Avaliacao) -> str:
    """Quem tem o perfil mas está ocupado aparece marcado: é a informação mais útil para o gestor."""
    return f"{a.funcionario.nome}{' (tem o perfil)' if a.perfil_ok else ''}: {a.motivos[0]}"


def vaga(v: Vaga, mostrar_alternativas: bool = True) -> str:
    r = v.requisito
    linhas = [f"{r.quantidade}× {r.papel}{exigencias(r)}"]
    if v.ja_confirmados:
        linhas.append(f"  ✔ já confirmados: {len(v.ja_confirmados)}")
    for a in v.escolhidos:
        linhas.append(f"  ✔ {candidato(a)}")
    if v.faltam:
        linhas.append(f"  ⚠ faltam {v.faltam}")
        for a in v.rejeitados[:3]:
            linhas.append(f"    · {rejeitado(a)}")
        resto = v.rejeitados[3:]
        if resto:
            tipo = "rejeitados (veja no painel)" if any(a.perfil_ok for a in resto) else "sem o perfil exigido"
            linhas.append(f"    · e mais {len(resto)} {tipo}")
    elif mostrar_alternativas and v.alternativas:
        linhas.append("  alternativas: " + ", ".join(f"{a.funcionario.nome} [{a.score:g}]" for a in v.alternativas))
    return "\n".join(linhas)


def proposta(p: Proposta) -> str:
    return "\n".join(vaga(v) for v in p.vagas)


def tarefa_resumo(t: Tarefa) -> str:
    local = f" · {t.local}" if t.local else ""
    return f"#{t.id} {t.titulo}\n{quando(t.inicio, t.fim)} · {t.tipo}{local}"


def tarefa_detalhe(t: Tarefa) -> str:
    linhas = [tarefa_resumo(t), f"Status: {t.status}"]
    for r in t.requisitos:
        aloc = [a for a in t.alocacoes if a.requisito_id == r.id and a.status in ("confirmada", "proposta")]
        nomes = ", ".join(f"{a.funcionario_nome}{'' if a.status == 'confirmada' else ' (proposto)'}" for a in aloc)
        linhas.append(f"{r.quantidade}× {r.papel}{exigencias(r)}: {nomes or 'ninguém'}")
    return "\n".join(linhas)


def equipe_confirmada(t: Tarefa, novas: list[Alocacao]) -> str:
    nomes = ", ".join(a.funcionario_nome for a in novas) or "ninguém"
    return f"Equipe confirmada em #{t.id}: {nomes}. Status: {t.status}."


def ajuda() -> str:
    return (
        "Posso:\n"
        "• criar serviço e montar equipe: \"preciso de 2 técnicos com NR35 pra quarta em Betim\"\n"
        "• registrar ausência: \"Diego está de atestado amanhã\"\n"
        "• tirar alguém de um serviço: \"tira o Igor da tarefa 1\"\n"
        "• completar equipe: \"aloca gente na tarefa 2\"\n"
        "• consultar: \"quem está livre sexta?\", \"agenda da Ana\", \"tarefas em andamento\"\n"
        "Tudo que grava pede sua confirmação antes."
    )
