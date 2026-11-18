"""Textos que o agente manda. Feitos em código (não pelo modelo) para economizar cota e não inventar nada."""

from __future__ import annotations

from ..allocation import Avaliacao, Proposta, Vaga
from ..models import Alocacao, Tarefa
from ..texto import quando  # noqa: F401  (grafo.py e bot.py usam fmt.quando)


def exigencias(r) -> str:
    partes = [f"{e['nome']}≥{e.get('nivel_min', 1)}" for e in (r.especialidades or [])]
    partes += list(r.certificacoes or [])
    if r.anos_experiencia_min:
        partes.append(f"≥{r.anos_experiencia_min:g} anos")
    if r.desejaveis:
        partes.append("desejável: " + ", ".join(r.desejaveis))
    return f" ({', '.join(partes)})" if partes else ""


def candidato(a: Avaliacao) -> str:
    return f"{a.funcionario.nome} [{a.score:g}] {a.detalhes.get('resumo', '')}"


def rejeitado(a: Avaliacao) -> str:
    """Quem tem o perfil mas está ocupado aparece marcado: é a informação mais útil para o gestor."""
    marca = " (tem o perfil)" if a.perfil_ok else ""
    if a.remanejar_de:
        tarefas = ", ".join(f"#{i}" for i in a.remanejar_de)
        marca = f" (remanejável: liberando {tarefas}, de prioridade menor)"
    return f"{a.funcionario.nome}{marca}: {a.motivos[0]}"


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
    linhas = [vaga(v) for v in p.vagas]
    c = p.contrato
    if c is not None and (c.certificacoes or c.anos_experiencia_min):
        regras = list(c.certificacoes or [])
        if c.anos_experiencia_min:
            regras.append(f"≥{c.anos_experiencia_min:g} anos")
        linhas.append(f"Contrato {c.codigo} exige de todos: {', '.join(regras)}")
    return "\n".join(linhas)


def tarefa_resumo(t: Tarefa) -> str:
    local = f" · {t.local}" if t.local else ""
    return f"#{t.id} {t.titulo}\n{quando(t.inicio, t.fim)} · {t.tipo}{local}"


def tarefa_detalhe(t: Tarefa) -> str:
    linhas = [tarefa_resumo(t), f"Status: {t.status} · prioridade {t.prioridade}"]
    if t.contrato:
        linhas.append(f"Contrato: {t.contrato}")
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
        "• tirar ou trocar alguém: \"tira o Igor da tarefa 1\", \"coloca o Beto no lugar da Ana na 1\"\n"
        "• completar equipe: \"aloca gente na tarefa 2\"\n"
        "• remarcar, concluir ou cancelar: \"passa a tarefa 2 pra quinta\", \"conclui a tarefa 3\"\n"
        "• consultar: \"quem está livre sexta?\", \"agenda da Ana\", \"quais NRs vencem este mês?\",\n"
        "  \"o que o contrato CT-2026-014 diz sobre EPI?\"\n"
        "Pode mandar áudio também. Tudo que grava pede sua confirmação antes."
    )


def ajuda_equipe(nome: str) -> str:
    return (
        f"Oi, {nome.split()[0]}! Comigo você pode:\n"
        "• ver sua agenda: \"minha agenda\"\n"
        "• ver detalhes de um serviço: \"detalhes da minha tarefa de amanhã\"\n"
        "• avisar uma ausência: \"estou de atestado hoje\" (vai para o gestor aprovar)\n"
        "Pode mandar áudio também."
    )
