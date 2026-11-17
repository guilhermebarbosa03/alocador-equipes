"""Motor de alocação determinístico (sem LLM).

1. Regras rígidas: quem não passa é rejeitado, sempre com o motivo registrado.
2. Pontuação: os candidatos viáveis são ordenados por aderência, experiência e folga de carga.
3. Escolha: vagas mais difíceis de preencher são resolvidas primeiro, sem repetir pessoas.

O agente (fase 2) chama este módulo como ferramenta; ele não decide restrições sozinho.
"""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Alocacao, Funcionario, Indisponibilidade, Tarefa

HORAS_DIA_PADRAO = 8.0
EPS = 1e-6

PESOS = {"aderencia": 40, "desejaveis": 15, "experiencia": 20, "folga_carga": 25}


def norm(texto: str | None) -> str:
    """Normaliza nomes para comparação: sem acento, minúsculo, espaços únicos."""
    s = unicodedata.normalize("NFKD", texto or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return " ".join(s.lower().split())


def sobrepoe(a_ini: datetime, a_fim: datetime, b_ini: datetime, b_fim: datetime) -> bool:
    return a_ini < b_fim and b_ini < a_fim


def segunda_da_semana(d: date) -> date:
    return d - timedelta(days=d.weekday())


def dias_do_intervalo(inicio: datetime, fim: datetime) -> list[date]:
    ultimo = (fim - timedelta(microseconds=1)).date() if fim > inicio else inicio.date()
    dias, d = [], inicio.date()
    while d <= ultimo:
        dias.append(d)
        d += timedelta(days=1)
    return dias


def horas_por_dia(inicio: datetime, fim: datetime, horas_por_pessoa: float | None = None) -> dict[date, float]:
    """Distribui as horas que cada pessoa gasta na tarefa pelos dias do intervalo.

    - Um dia só, sem horas informadas: duração, limitada a 8h (desconta almoço de 08h-17h).
    - Vários dias: as horas vão para os dias úteis; sem horas informadas, 8h por dia útil.
    """
    dias = dias_do_intervalo(inicio, fim)
    if len(dias) == 1:
        total = horas_por_pessoa
        if total is None:
            total = min((fim - inicio).total_seconds() / 3600, HORAS_DIA_PADRAO)
        return {dias[0]: total}

    uteis = [d for d in dias if d.weekday() < 5] or dias
    total = horas_por_pessoa if horas_por_pessoa is not None else HORAS_DIA_PADRAO * len(uteis)
    por_dia = total / len(uteis)
    return {d: por_dia for d in uteis}


def fmt_data(d: date | datetime) -> str:
    return d.strftime("%d/%m/%Y %H:%M") if isinstance(d, datetime) else d.strftime("%d/%m/%Y")


@dataclass
class Janela:
    inicio: datetime
    fim: datetime
    horas_por_pessoa: float | None = None
    tipo: str = "interna"
    tarefa_id: int | None = None

    @classmethod
    def de(cls, tarefa: Tarefa) -> "Janela":
        return cls(tarefa.inicio, tarefa.fim, tarefa.horas_por_pessoa, tarefa.tipo, tarefa.id)


@dataclass
class RequisitoSpec:
    """Mesmos campos de models.Requisito, para avaliar sem gravar no banco."""

    papel: str
    quantidade: int = 1
    especialidades: list = field(default_factory=list)
    desejaveis: list = field(default_factory=list)
    certificacoes: list = field(default_factory=list)
    anos_experiencia_min: float = 0
    id: int | None = None


@dataclass
class Compromissos:
    """Agenda já confirmada de um funcionário, fora a tarefa que está sendo avaliada."""

    intervalos: list[tuple[datetime, datetime, int, str]] = field(default_factory=list)
    horas_dia: dict[date, float] = field(default_factory=lambda: defaultdict(float))
    indisponibilidades: list[Indisponibilidade] = field(default_factory=list)

    def horas_semana(self, segunda: date) -> float:
        return sum(self.horas_dia.get(segunda + timedelta(days=i), 0.0) for i in range(7))


def carregar_compromissos(db: Session, janela: Janela, funcionario_ids: list[int]) -> dict[int, Compromissos]:
    comps: dict[int, Compromissos] = defaultdict(Compromissos)
    if not funcionario_ids:
        return comps

    dias = dias_do_intervalo(janela.inicio, janela.fim)
    jan_ini = datetime.combine(segunda_da_semana(dias[0]), time.min)
    jan_fim = datetime.combine(segunda_da_semana(dias[-1]) + timedelta(days=7), time.min)

    q = (
        select(Alocacao.funcionario_id, Tarefa)
        .join(Tarefa, Alocacao.tarefa_id == Tarefa.id)
        .where(
            Alocacao.status == "confirmada",
            Alocacao.funcionario_id.in_(funcionario_ids),
            Tarefa.status != "cancelada",
            Tarefa.inicio < jan_fim,
            Tarefa.fim > jan_ini,
        )
    )
    if janela.tarefa_id is not None:
        q = q.where(Tarefa.id != janela.tarefa_id)

    for fid, t in db.execute(q).all():
        c = comps[fid]
        c.intervalos.append((t.inicio, t.fim, t.id, t.titulo))
        for d, h in horas_por_dia(t.inicio, t.fim, t.horas_por_pessoa).items():
            c.horas_dia[d] += h

    indis = db.scalars(
        select(Indisponibilidade).where(
            Indisponibilidade.funcionario_id.in_(funcionario_ids),
            Indisponibilidade.inicio < janela.fim,
            Indisponibilidade.fim > janela.inicio,
        )
    ).all()
    for ind in indis:
        comps[ind.funcionario_id].indisponibilidades.append(ind)
    return comps


@dataclass
class Avaliacao:
    funcionario: Funcionario
    motivos: list[str]
    score: float
    detalhes: dict[str, Any]
    perfil_ok: bool = True  # falhou só por agenda (ausência, conflito, carga)

    @property
    def viavel(self) -> bool:
        return not self.motivos

    def to_dict(self) -> dict[str, Any]:
        f = self.funcionario
        out = {"funcionario_id": f.id, "nome": f.nome, "papel": f.papel}
        if self.viavel:
            out.update(score=self.score, detalhes=self.detalhes, resumo=self.detalhes.get("resumo", ""))
        else:
            out["motivos"] = self.motivos
            out["perfil_ok"] = self.perfil_ok
        return out


def avaliar(f: Funcionario, req, janela: Janela, comp: Compromissos) -> Avaliacao:
    motivos: list[str] = []

    if not f.ativo:
        motivos.append("cadastro inativo")
    if janela.tipo == "campo" and not f.disponivel_viagem:
        motivos.append("não está disponível para campo/viagem")
    if req.anos_experiencia_min and f.anos_experiencia < req.anos_experiencia_min:
        motivos.append(f"{f.anos_experiencia:g} anos de experiência (mínimo {req.anos_experiencia_min:g})")

    skills = {norm(e.nome): e.nivel for e in f.especialidades}
    for item in req.especialidades or []:
        nome, nivel_min = item["nome"], int(item.get("nivel_min", 1))
        nivel = skills.get(norm(nome))
        if nivel is None:
            motivos.append(f"não tem a especialidade '{nome}'")
        elif nivel < nivel_min:
            motivos.append(f"'{nome}' nível {nivel} (mínimo {nivel_min})")

    certs = {norm(c.nome): c.validade for c in f.certificacoes}
    for nome in req.certificacoes or []:
        k = norm(nome)
        if k not in certs:
            motivos.append(f"não tem a certificação '{nome}'")
        elif certs[k] is not None and certs[k] < janela.fim.date():
            motivos.append(f"certificação '{nome}' vence em {fmt_data(certs[k])}, antes do fim da tarefa")

    perfil_ok = not motivos  # daqui para baixo, só motivos de agenda
    for ind in comp.indisponibilidades:
        if sobrepoe(ind.inicio, ind.fim, janela.inicio, janela.fim):
            motivos.append(f"indisponível ({ind.motivo}) de {fmt_data(ind.inicio)} a {fmt_data(ind.fim)}")

    for ini, fim, tid, titulo in comp.intervalos:
        if sobrepoe(ini, fim, janela.inicio, janela.fim):
            motivos.append(f"já alocado na tarefa #{tid} '{titulo}' no mesmo horário")

    novas = horas_por_dia(janela.inicio, janela.fim, janela.horas_por_pessoa)
    for d, h in sorted(novas.items()):
        total = comp.horas_dia.get(d, 0.0) + h
        if total > f.horas_max_dia + EPS:
            motivos.append(f"passa da jornada diária em {fmt_data(d)} ({total:g}h de {f.horas_max_dia:g}h)")
            break

    ocupacao = 0.0
    for seg in sorted({segunda_da_semana(d) for d in novas}):
        nova_semana = sum(h for d, h in novas.items() if segunda_da_semana(d) == seg)
        total = comp.horas_semana(seg) + nova_semana
        ocupacao = max(ocupacao, total / f.carga_horaria_semanal)
        if total > f.carga_horaria_semanal + EPS:
            motivos.append(
                f"passa da carga semanal na semana de {fmt_data(seg)} ({total:g}h de {f.carga_horaria_semanal:g}h)"
            )

    # Pontuação (0 a 100). Calculada mesmo para rejeitados, mas só é usada nos viáveis.
    req_sk = req.especialidades or []
    aderencia = (
        sum(min(skills.get(norm(i["nome"]), 0), 5) / 5 for i in req_sk) / len(req_sk) if req_sk else 0.5
    )
    des = req.desejaveis or []
    desejaveis = sum(min(skills.get(norm(n), 0), 5) / 5 for n in des) / len(des) if des else 0.5
    experiencia = min(f.anos_experiencia / 10, 1.0)
    folga = max(0.0, 1 - ocupacao)
    score = (
        PESOS["aderencia"] * aderencia
        + PESOS["desejaveis"] * desejaveis
        + PESOS["experiencia"] * experiencia
        + PESOS["folga_carga"] * folga
    )

    partes = [f"{i['nome']} nv{skills[norm(i['nome'])]}" for i in req_sk if norm(i["nome"]) in skills]
    partes += [f"{n} nv{skills[norm(n)]} (desejável)" for n in des if norm(n) in skills]
    partes.append(f"{f.anos_experiencia:g} anos de exp.")
    partes.append(f"semana {round(ocupacao * 100)}% ocupada com esta tarefa")

    detalhes = {
        "aderencia": round(aderencia, 2),
        "desejaveis": round(desejaveis, 2),
        "experiencia": round(experiencia, 2),
        "folga_carga": round(folga, 2),
        "ocupacao_semana_pct": round(ocupacao * 100),
        "resumo": " · ".join(partes),
    }
    return Avaliacao(f, motivos, round(score, 1), detalhes, perfil_ok)


@dataclass
class Vaga:
    requisito: Any
    ja_confirmados: list[int]
    escolhidos: list[Avaliacao]
    alternativas: list[Avaliacao]
    rejeitados: list[Avaliacao]

    @property
    def faltam(self) -> int:
        return max(0, self.requisito.quantidade - len(self.ja_confirmados) - len(self.escolhidos))

    def to_dict(self) -> dict[str, Any]:
        r = self.requisito
        return {
            "requisito_id": r.id,
            "papel": r.papel,
            "quantidade": r.quantidade,
            "ja_confirmados": self.ja_confirmados,
            "escolhidos": [a.to_dict() for a in self.escolhidos],
            "alternativas": [a.to_dict() for a in self.alternativas],
            "rejeitados": [a.to_dict() for a in self.rejeitados],
            "faltam": self.faltam,
        }


@dataclass
class Proposta:
    tarefa_id: int | None
    vagas: list[Vaga]

    @property
    def completa(self) -> bool:
        return all(v.faltam == 0 for v in self.vagas)

    def to_dict(self) -> dict[str, Any]:
        return {"tarefa_id": self.tarefa_id, "completa": self.completa, "vagas": [v.to_dict() for v in self.vagas]}


def propor(
    db: Session,
    tarefa: Tarefa,
    max_alternativas: int = 3,
    excluir_funcionarios: Iterable[int] = (),
    ignorar_alocacoes: Iterable[int] = (),
) -> Proposta:
    """`excluir_funcionarios` e `ignorar_alocacoes` servem para procurar substituto de alguém já alocado."""
    excluir, ignorar = set(excluir_funcionarios), set(ignorar_alocacoes)
    funcionarios = [
        f for f in db.scalars(select(Funcionario).where(Funcionario.ativo.is_(True))).all() if f.id not in excluir
    ]
    janela = Janela.de(tarefa)
    comps = carregar_compromissos(db, janela, [f.id for f in funcionarios])

    confirmados_por_req: dict[int | None, list[int]] = defaultdict(list)
    if tarefa.id is not None:
        for a in db.scalars(
            select(Alocacao).where(Alocacao.tarefa_id == tarefa.id, Alocacao.status == "confirmada")
        ):
            if a.id not in ignorar:
                confirmados_por_req[a.requisito_id].append(a.funcionario_id)
    usados = {fid for ids in confirmados_por_req.values() for fid in ids}

    avaliacoes = {}
    for req in tarefa.requisitos:
        candidatos = [f for f in funcionarios if norm(f.papel) == norm(req.papel) and f.id not in usados]
        avaliacoes[id(req)] = [avaliar(f, req, janela, comps[f.id]) for f in candidatos]

    def folga_vaga(req) -> int:
        restantes = req.quantidade - len(confirmados_por_req[req.id])
        return sum(a.viavel for a in avaliacoes[id(req)]) - restantes

    vagas: dict[int, Vaga] = {}
    for req in sorted(tarefa.requisitos, key=folga_vaga):  # vaga mais difícil primeiro
        restantes = max(0, req.quantidade - len(confirmados_por_req[req.id]))
        viaveis = sorted(
            (a for a in avaliacoes[id(req)] if a.viavel and a.funcionario.id not in usados),
            key=lambda a: (-a.score, a.funcionario.nome),
        )
        escolhidos = viaveis[:restantes]
        usados.update(a.funcionario.id for a in escolhidos)
        vagas[id(req)] = Vaga(
            requisito=req,
            ja_confirmados=confirmados_por_req[req.id],
            escolhidos=escolhidos,
            alternativas=viaveis[restantes : restantes + max_alternativas],
            rejeitados=sorted(
                (a for a in avaliacoes[id(req)] if not a.viavel),
                key=lambda a: (not a.perfil_ok, len(a.motivos), -a.score, a.funcionario.nome),
            ),
        )

    return Proposta(tarefa.id, [vagas[id(r)] for r in tarefa.requisitos])


def disponiveis(db: Session, inicio: datetime, fim: datetime, papel: str | None = None) -> list[Avaliacao]:
    """Quem está livre num intervalo (sem exigências de especialidade)."""
    q = select(Funcionario).where(Funcionario.ativo.is_(True))
    funcionarios = [f for f in db.scalars(q).all() if papel is None or norm(f.papel) == norm(papel)]
    janela = Janela(inicio, fim)
    comps = carregar_compromissos(db, janela, [f.id for f in funcionarios])
    out = []
    for f in funcionarios:
        a = avaliar(f, RequisitoSpec(papel=f.papel), janela, comps[f.id])
        if a.viavel:
            out.append(a)
    return sorted(out, key=lambda a: (a.detalhes["ocupacao_semana_pct"], a.funcionario.nome))
