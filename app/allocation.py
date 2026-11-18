"""Motor de alocação determinístico (sem LLM).

1. Regras rígidas: quem não passa é rejeitado, sempre com o motivo registrado.
2. Pontuação: os candidatos viáveis são ordenados por aderência, experiência, folga de carga e proximidade.
3. Escolha: fluxo de custo mínimo. Primeiro preenche o maior número possível de vagas; entre as
   equipes que fazem isso, fica a de maior pontuação somada. A mesma pessoa não ocupa duas vagas.

O agente (fase 2) chama este módulo como ferramenta; ele não decide restrições sozinho.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from functools import cached_property
from typing import Any, Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from . import config
from .models import Alocacao, Contrato, Funcionario, Indisponibilidade, Tarefa
from .texto import mesma_cidade, norm

__all__ = ["norm"]  # reexportado: vários módulos importam norm daqui

HORAS_DIA_PADRAO = 8.0
JORNADA_INICIO = time(8)
JORNADA_FIM = time(17)
EPS = 1e-6

PESOS = {"aderencia": 40, "desejaveis": 15, "experiencia": 15, "folga_carga": 25, "proximidade": 5}
PRIORIDADE_REMANEJO = 4  # tarefas a partir desta prioridade sugerem tirar gente de tarefas menos importantes


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


def _horas(a: datetime, b: datetime) -> float:
    return max(0.0, (b - a).total_seconds() / 3600)


def horas_por_dia(inicio: datetime, fim: datetime, horas_por_pessoa: float | None = None) -> dict[date, float]:
    """Horas que cada pessoa gasta na tarefa, dia a dia.

    - Um dia só: a duração, limitada a 8h (08h–17h = 8h, descontando o almoço), ou as horas informadas.
    - Vários dias: em cada dia conta o trecho dentro da jornada (08h–17h), até 8h. Assim, sair segunda
      às 15h conta 2h na segunda. Um trecho todo fora da jornada (plantão que vira a madrugada) conta
      pela duração real: 22h–02h são 2h + 2h, não dois dias cheios.
    - Fim de semana só entra se a tarefa começa ou termina nele (parada de sexta a domingo). Uma tarefa
      de sexta a segunda não conta sábado e domingo.
    - Com horas por pessoa informadas, o total é repartido na proporção das horas de cada dia.
    """
    dias = dias_do_intervalo(inicio, fim)
    if len(dias) == 1:
        total = horas_por_pessoa if horas_por_pessoa is not None else min(_horas(inicio, fim), HORAS_DIA_PADRAO)
        return {dias[0]: total}

    com_fds = dias[0].weekday() >= 5 or dias[-1].weekday() >= 5
    considerados = [d for d in dias if com_fds or d.weekday() < 5] or dias
    pesos: dict[date, float] = {}
    for d in considerados:
        ini_d = max(inicio, datetime.combine(d, time.min))
        fim_d = min(fim, datetime.combine(d + timedelta(days=1), time.min))
        na_jornada = _horas(max(ini_d, datetime.combine(d, JORNADA_INICIO)), min(fim_d, datetime.combine(d, JORNADA_FIM)))
        h = min(na_jornada if na_jornada > 0 else _horas(ini_d, fim_d), HORAS_DIA_PADRAO)
        if h > EPS:
            pesos[d] = h
    if not pesos:
        pesos = {d: HORAS_DIA_PADRAO for d in considerados}
    if horas_por_pessoa is None:
        return pesos
    soma = sum(pesos.values())
    return {d: horas_por_pessoa * h / soma for d, h in pesos.items()}


def fmt_data(d: date | datetime) -> str:
    return d.strftime("%d/%m/%Y %H:%M") if isinstance(d, datetime) else d.strftime("%d/%m/%Y")


def _h(valor: float) -> str:
    return f"{round(valor, 1):g}"


@dataclass
class Janela:
    inicio: datetime
    fim: datetime
    horas_por_pessoa: float | None = None
    tipo: str = "interna"
    tarefa_id: int | None = None
    local: str | None = None
    prioridade: int = 3

    @classmethod
    def de(cls, tarefa: Tarefa) -> "Janela":
        return cls(
            tarefa.inicio, tarefa.fim, tarefa.horas_por_pessoa, tarefa.tipo, tarefa.id,
            tarefa.local, tarefa.prioridade or 3,
        )


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
    contrato: str | None = None  # código do contrato que acrescentou exigências
    certs_contrato: frozenset = frozenset()  # certificações que vieram só do contrato (nomes normalizados)


def contrato_por_codigo(db: Session, codigo: str | None) -> Contrato | None:
    if not codigo or not codigo.strip():
        return None
    return db.scalar(select(Contrato).where(func.upper(Contrato.codigo) == codigo.strip().upper()))


def efetivo(req, contrato: Contrato | None):
    """A vaga com as exigências do contrato somadas (certificações e experiência mínima)."""
    if contrato is None or not (contrato.certificacoes or contrato.anos_experiencia_min):
        return req
    certs = list(req.certificacoes or [])
    proprias = {norm(c) for c in certs}
    extras = [c for c in contrato.certificacoes or [] if norm(c) not in proprias]
    return RequisitoSpec(
        papel=req.papel, quantidade=req.quantidade, especialidades=list(req.especialidades or []),
        desejaveis=list(req.desejaveis or []), certificacoes=certs + extras,
        anos_experiencia_min=max(req.anos_experiencia_min or 0, contrato.anos_experiencia_min or 0),
        id=req.id, contrato=contrato.codigo, certs_contrato=frozenset(norm(c) for c in extras),
    )


@dataclass
class Compromisso:
    inicio: datetime
    fim: datetime
    tarefa_id: int
    titulo: str
    prioridade: int = 3
    tipo: str = "interna"
    local: str | None = None
    horas: dict[date, float] = field(default_factory=dict)

    @classmethod
    def de(cls, t: Tarefa) -> "Compromisso":
        return cls(t.inicio, t.fim, t.id, t.titulo, t.prioridade or 3, t.tipo, t.local,
                   horas_por_dia(t.inicio, t.fim, t.horas_por_pessoa))


@dataclass
class Compromissos:
    """Agenda já confirmada de um funcionário, fora a tarefa que está sendo avaliada."""

    tarefas: list[Compromisso] = field(default_factory=list)
    indisponibilidades: list[Indisponibilidade] = field(default_factory=list)

    @cached_property
    def horas_dia(self) -> dict[date, float]:
        out: dict[date, float] = defaultdict(float)
        for c in self.tarefas:
            for d, h in c.horas.items():
                out[d] += h
        return out

    def horas_semana(self, segunda: date) -> float:
        return sum(self.horas_dia.get(segunda + timedelta(days=i), 0.0) for i in range(7))

    def sem(self, tarefa_ids: set[int]) -> "Compromissos":
        return Compromissos([c for c in self.tarefas if c.tarefa_id not in tarefa_ids], self.indisponibilidades)


def carregar_compromissos(db: Session, janela: Janela, funcionario_ids: list[int]) -> dict[int, Compromissos]:
    comps: dict[int, Compromissos] = defaultdict(Compromissos)
    if not funcionario_ids:
        return comps

    dias = dias_do_intervalo(janela.inicio, janela.fim)
    # semanas inteiras (carga semanal) e um dia de folga de cada lado (descanso entre jornadas)
    jan_ini = min(datetime.combine(segunda_da_semana(dias[0]), time.min), janela.inicio - timedelta(days=1))
    jan_fim = max(datetime.combine(segunda_da_semana(dias[-1]) + timedelta(days=7), time.min),
                  janela.fim + timedelta(days=1))

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
        .order_by(Tarefa.inicio)
    )
    if janela.tarefa_id is not None:
        q = q.where(Tarefa.id != janela.tarefa_id)

    for fid, t in db.execute(q).all():
        comps[fid].tarefas.append(Compromisso.de(t))

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
    remanejar_de: list[int] = field(default_factory=list)  # tarefas de prioridade baixa que, liberadas, resolvem

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
            if self.remanejar_de:
                out["remanejar_de"] = self.remanejar_de
        return out


def _motivos_de_agenda(f: Funcionario, janela: Janela, comp: Compromissos, novas: dict[date, float]) -> tuple[list[str], float]:
    motivos: list[str] = []
    for ind in comp.indisponibilidades:
        if sobrepoe(ind.inicio, ind.fim, janela.inicio, janela.fim):
            motivos.append(f"indisponível ({ind.motivo}) de {fmt_data(ind.inicio)} a {fmt_data(ind.fim)}")

    for c in comp.tarefas:
        if sobrepoe(c.inicio, c.fim, janela.inicio, janela.fim):
            motivos.append(f"já alocado na tarefa #{c.tarefa_id} '{c.titulo}' no mesmo horário")

    if config.DESCANSO_MIN_HORAS > 0:
        minimo = timedelta(hours=config.DESCANSO_MIN_HORAS)
        ultimo_dia = (janela.fim - timedelta(microseconds=1)).date()
        for c in comp.tarefas:
            if c.fim <= janela.inicio and (c.fim - timedelta(microseconds=1)).date() < janela.inicio.date():
                folga = janela.inicio - c.fim
            elif janela.fim <= c.inicio and ultimo_dia < c.inicio.date():
                folga = c.inicio - janela.fim
            else:
                continue
            if folga < minimo:
                motivos.append(
                    f"só {_h(folga.total_seconds() / 3600)}h de descanso entre jornadas com a tarefa "
                    f"#{c.tarefa_id} '{c.titulo}' (mínimo {_h(config.DESCANSO_MIN_HORAS)}h)"
                )

    if config.DESLOCAMENTO_MIN_MINUTOS > 0 and janela.tipo == "campo" and janela.local:
        minimo = timedelta(minutes=config.DESLOCAMENTO_MIN_MINUTOS)
        for c in comp.tarefas:
            if c.tipo != "campo" or not c.local or mesma_cidade(c.local, janela.local):
                continue
            if sobrepoe(c.inicio, c.fim, janela.inicio, janela.fim):
                continue  # já é conflito de horário
            intervalo = janela.inicio - c.fim if c.fim <= janela.inicio else c.inicio - janela.fim
            if intervalo < minimo:
                motivos.append(
                    f"sem tempo de deslocamento entre {c.local} (tarefa #{c.tarefa_id}) e {janela.local} "
                    f"(mínimo {config.DESLOCAMENTO_MIN_MINUTOS:g} min)"
                )

    for d, h in sorted(novas.items()):
        total = comp.horas_dia.get(d, 0.0) + h
        if total > f.horas_max_dia + EPS:
            motivos.append(f"passa da jornada diária em {fmt_data(d)} ({_h(total)}h de {_h(f.horas_max_dia)}h)")
            break

    ocupacao = 0.0
    for seg in sorted({segunda_da_semana(d) for d in novas}):
        nova_semana = sum(h for d, h in novas.items() if segunda_da_semana(d) == seg)
        total = comp.horas_semana(seg) + nova_semana
        ocupacao = max(ocupacao, total / f.carga_horaria_semanal)
        if total > f.carga_horaria_semanal + EPS:
            motivos.append(
                f"passa da carga semanal na semana de {fmt_data(seg)} ({_h(total)}h de {_h(f.carga_horaria_semanal)}h)"
            )
    return motivos, ocupacao


def avaliar(f: Funcionario, req, janela: Janela, comp: Compromissos, remanejar: bool = True) -> Avaliacao:
    motivos: list[str] = []
    do_contrato = getattr(req, "certs_contrato", frozenset())
    contrato = getattr(req, "contrato", None)

    if not f.ativo:
        motivos.append("cadastro inativo")
    if janela.tipo == "campo" and not f.disponivel_viagem:
        motivos.append("não está disponível para campo/viagem")
    if req.anos_experiencia_min and f.anos_experiencia < req.anos_experiencia_min:
        motivos.append(f"{f.anos_experiencia:g} anos de experiência (mínimo {req.anos_experiencia_min:g})")

    skills: dict[str, int] = {}
    for e in f.especialidades:
        skills[norm(e.nome)] = max(e.nivel, skills.get(norm(e.nome), 0))
    for item in req.especialidades or []:
        nome, nivel_min = item["nome"], int(item.get("nivel_min", 1))
        nivel = skills.get(norm(nome))
        if nivel is None:
            motivos.append(f"não tem a especialidade '{nome}'")
        elif nivel < nivel_min:
            motivos.append(f"'{nome}' nível {nivel} (mínimo {nivel_min})")

    # com a mesma certificação repetida (vencida e renovada), vale a de validade mais longa
    certs: dict[str, date | None] = {}
    for c in f.certificacoes:
        k = norm(c.nome)
        if k not in certs or (certs[k] is not None and (c.validade is None or c.validade > certs[k])):
            certs[k] = c.validade
    for nome in req.certificacoes or []:
        k = norm(nome)
        origem = f" (exigência do contrato {contrato})" if k in do_contrato else ""
        if k not in certs:
            motivos.append(f"não tem a certificação '{nome}'{origem}")
        elif certs[k] is not None and certs[k] < janela.fim.date():
            motivos.append(f"certificação '{nome}' vence em {fmt_data(certs[k])}, antes do fim da tarefa{origem}")

    perfil_ok = not motivos  # daqui para baixo, só motivos de agenda
    novas = horas_por_dia(janela.inicio, janela.fim, janela.horas_por_pessoa)
    agenda, ocupacao = _motivos_de_agenda(f, janela, comp, novas)
    motivos += agenda

    remanejar_de: list[int] = []
    if remanejar and perfil_ok and agenda and janela.prioridade >= PRIORIDADE_REMANEJO:
        menos_importantes = {
            c.tarefa_id for c in comp.tarefas
            if c.prioridade <= janela.prioridade - 2
            and sobrepoe(c.inicio, c.fim, janela.inicio - timedelta(days=1), janela.fim + timedelta(days=1))
        }
        if menos_importantes and avaliar(f, req, janela, comp.sem(menos_importantes), remanejar=False).viavel:
            remanejar_de = sorted(menos_importantes)

    # Pontuação (0 a 100). Calculada mesmo para rejeitados, mas só é usada nos viáveis.
    req_sk = req.especialidades or []
    aderencia = (
        sum(min(skills.get(norm(i["nome"]), 0), 5) / 5 for i in req_sk) / len(req_sk) if req_sk else 0.5
    )
    des = req.desejaveis or []
    desejaveis = sum(min(skills.get(norm(n), 0), 5) / 5 for n in des) / len(des) if des else 0.5
    experiencia = min(f.anos_experiencia / 10, 1.0)
    folga = max(0.0, 1 - ocupacao)
    if janela.tipo == "campo" and janela.local:
        proximidade = 1.0 if mesma_cidade(f.base, janela.local) else 0.0
    else:
        proximidade = 0.5
    score = (
        PESOS["aderencia"] * aderencia
        + PESOS["desejaveis"] * desejaveis
        + PESOS["experiencia"] * experiencia
        + PESOS["folga_carga"] * folga
        + PESOS["proximidade"] * proximidade
    )

    partes = [f"{i['nome']} nv{skills[norm(i['nome'])]}" for i in req_sk if norm(i["nome"]) in skills]
    partes += [f"{n} nv{skills[norm(n)]} (desejável)" for n in des if norm(n) in skills]
    partes.append(f"{f.anos_experiencia:g} anos de exp.")
    partes.append(f"semana {round(ocupacao * 100)}% ocupada com esta tarefa")
    if proximidade == 1.0:
        partes.append(f"base em {f.base}")

    detalhes = {
        "aderencia": round(aderencia, 2),
        "desejaveis": round(desejaveis, 2),
        "experiencia": round(experiencia, 2),
        "folga_carga": round(folga, 2),
        "proximidade": proximidade,
        "ocupacao_semana_pct": round(ocupacao * 100),
        "resumo": " · ".join(partes),
    }
    return Avaliacao(f, motivos, round(score, 1), detalhes, perfil_ok, remanejar_de)


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
    contrato: Contrato | None = None

    @property
    def completa(self) -> bool:
        return all(v.faltam == 0 for v in self.vagas)

    def to_dict(self) -> dict[str, Any]:
        return {"tarefa_id": self.tarefa_id, "completa": self.completa, "vagas": [v.to_dict() for v in self.vagas]}


def _fluxo_custo_minimo(vagas: list[tuple[int, list[Avaliacao]]]) -> list[list[Avaliacao]]:
    """Distribui pessoas pelas vagas: máximo de vagas preenchidas e, entre essas, a maior soma de pontos.

    `vagas` = [(quantas faltam, candidatos viáveis)]. Fluxo de custo mínimo com caminhos aumentantes
    mais curtos (SPFA). Para equipes de dezenas de pessoas roda em milissegundos.
    """
    pessoas: dict[int, int] = {}
    for _, cands in vagas:
        for a in cands:
            pessoas.setdefault(a.funcionario.id, len(pessoas))
    n_v, n_p = len(vagas), len(pessoas)
    fonte, sumidouro = 0, n_v + n_p + 1
    grafo: list[list[list[int]]] = [[] for _ in range(n_v + n_p + 2)]  # arestas: [destino, capacidade, custo, reversa]

    def aresta(u: int, v: int, cap: int, custo: int) -> None:
        grafo[u].append([v, cap, custo, len(grafo[v])])
        grafo[v].append([u, 0, -custo, len(grafo[u]) - 1])

    for i, (faltam, cands) in enumerate(vagas):
        if faltam > 0:
            aresta(fonte, 1 + i, faltam, 0)
        for a in sorted(cands, key=lambda a: (-a.score, a.funcionario.nome)):
            aresta(1 + i, 1 + n_v + pessoas[a.funcionario.id], 1, -round(a.score * 10))
    for j in range(n_p):
        aresta(1 + n_v + j, sumidouro, 1, 0)

    infinito = float("inf")
    while True:
        dist = [infinito] * len(grafo)
        anterior: list[tuple[int, int] | None] = [None] * len(grafo)
        na_fila = [False] * len(grafo)
        dist[fonte] = 0
        fila = deque([fonte])
        while fila:
            u = fila.popleft()
            na_fila[u] = False
            for k, (v, cap, custo, _) in enumerate(grafo[u]):
                if cap > 0 and dist[u] + custo < dist[v]:
                    dist[v] = dist[u] + custo
                    anterior[v] = (u, k)
                    if not na_fila[v]:
                        na_fila[v] = True
                        fila.append(v)
        if dist[sumidouro] == infinito:
            break
        v = sumidouro
        while v != fonte:  # cada caminho leva 1 pessoa (as arestas até o sumidouro têm capacidade 1)
            u, k = anterior[v]
            e = grafo[u][k]
            e[1] -= 1
            grafo[v][e[3]][1] += 1
            v = u

    por_id = {a.funcionario.id: a for _, cands in vagas for a in cands}
    ids_pessoas = {j: fid for fid, j in pessoas.items()}
    resultado: list[list[Avaliacao]] = []
    for i, (_, cands) in enumerate(vagas):
        validos = {a.funcionario.id: a for a in cands}
        escolhidos = []
        for v, cap, custo, _ in grafo[1 + i]:
            if 1 + n_v <= v < sumidouro and cap == 0 and custo <= 0:
                fid = ids_pessoas[v - 1 - n_v]
                escolhidos.append(validos.get(fid, por_id[fid]))
        resultado.append(sorted(escolhidos, key=lambda a: (-a.score, a.funcionario.nome)))
    return resultado


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
    contrato = contrato_por_codigo(db, tarefa.contrato)

    confirmados_por_req: dict[int | None, list[int]] = defaultdict(list)
    if tarefa.id is not None:
        for a in db.scalars(
            select(Alocacao).where(Alocacao.tarefa_id == tarefa.id, Alocacao.status == "confirmada")
        ):
            if a.id not in ignorar:
                confirmados_por_req[a.requisito_id].append(a.funcionario_id)
    confirmado_em = {fid: rid for rid, ids in confirmados_por_req.items() for fid in ids}
    papel_de = {r.id: r.papel for r in tarefa.requisitos}

    avaliacoes: dict[int, list[Avaliacao]] = {}
    for req in tarefa.requisitos:
        spec = efetivo(req, contrato)
        lista = []
        for f in funcionarios:
            if norm(f.papel) != norm(req.papel):
                continue
            if f.id in confirmado_em:
                if req.id is not None and confirmado_em[f.id] == req.id:
                    continue  # já está nesta vaga
                a = avaliar(f, spec, janela, comps[f.id])
                outra = papel_de.get(confirmado_em[f.id], "outra função")
                a = replace(a, motivos=[*a.motivos, f"já confirmado(a) nesta tarefa, na vaga de {outra}"])
            else:
                a = avaliar(f, spec, janela, comps[f.id])
            lista.append(a)
        avaliacoes[id(req)] = lista

    restantes = {id(r): max(0, r.quantidade - len(confirmados_por_req[r.id])) for r in tarefa.requisitos}
    distribuicao = _fluxo_custo_minimo(
        [(restantes[id(r)], [a for a in avaliacoes[id(r)] if a.viavel]) for r in tarefa.requisitos]
    )
    escolhidos_por_req = {id(r): esc for r, esc in zip(tarefa.requisitos, distribuicao)}
    vaga_de = {a.funcionario.id: r for r in tarefa.requisitos for a in escolhidos_por_req[id(r)]}

    vagas = []
    for req in tarefa.requisitos:
        escolhidos = escolhidos_por_req[id(req)]
        viaveis = sorted(
            (a for a in avaliacoes[id(req)] if a.viavel and a.funcionario.id not in vaga_de),
            key=lambda a: (-a.score, a.funcionario.nome),
        )
        # viável aqui, mas ficou com outra vaga desta tarefa: aparece nos rejeitados com o motivo,
        # senão a vaga diria "ninguém viável" sem mostrar ninguém
        em_outra_vaga = [
            replace(a, motivos=[f"escolhido(a) para outra vaga de {vaga_de[a.funcionario.id].papel} desta tarefa"])
            for a in avaliacoes[id(req)]
            if a.viavel and a.funcionario.id in vaga_de and vaga_de[a.funcionario.id] is not req
        ]
        vagas.append(Vaga(
            requisito=req,
            ja_confirmados=confirmados_por_req[req.id],
            escolhidos=escolhidos,
            alternativas=viaveis[:max_alternativas],
            rejeitados=sorted(
                [a for a in avaliacoes[id(req)] if not a.viavel] + em_outra_vaga,
                key=lambda a: (not a.perfil_ok, not a.remanejar_de, len(a.motivos), -a.score, a.funcionario.nome),
            ),
        ))

    return Proposta(tarefa.id, vagas, contrato)


def disponiveis(db: Session, inicio: datetime, fim: datetime, papel: str | None = None) -> list[Avaliacao]:
    """Quem está livre num intervalo: sem ausência, sem tarefa no mesmo horário e com horas sobrando
    em cada dia e semana do intervalo. Não exige que a pessoa aguente o intervalo inteiro como uma
    tarefa só: quem faz 6h por dia ou 30h por semana aparece livre se a agenda está vazia."""
    q = select(Funcionario).where(Funcionario.ativo.is_(True))
    funcionarios = [f for f in db.scalars(q).all() if papel is None or norm(f.papel) == norm(papel)]
    janela = Janela(inicio, fim)
    comps = carregar_compromissos(db, janela, [f.id for f in funcionarios])
    dias = dias_do_intervalo(inicio, fim)
    semanas = sorted({segunda_da_semana(d) for d in dias})
    out = []
    for f in funcionarios:
        c = comps[f.id]
        motivos = [
            f"indisponível ({i.motivo})" for i in c.indisponibilidades if sobrepoe(i.inicio, i.fim, inicio, fim)
        ]
        motivos += [
            f"na tarefa #{t.tarefa_id}" for t in c.tarefas if sobrepoe(t.inicio, t.fim, inicio, fim)
        ]
        motivos += [
            f"jornada de {fmt_data(d)} completa" for d in dias if c.horas_dia.get(d, 0) >= f.horas_max_dia - EPS
        ]
        ocupacao = max(c.horas_semana(s) / f.carga_horaria_semanal for s in semanas)
        if ocupacao >= 1 - EPS:
            motivos.append("carga semanal completa")
        if motivos:
            continue
        livres_semana = min(f.carga_horaria_semanal - c.horas_semana(s) for s in semanas)
        out.append(Avaliacao(
            f, [], round(100 * (1 - ocupacao), 1),
            {"ocupacao_semana_pct": round(ocupacao * 100), "horas_livres_semana": round(livres_semana, 1),
             "resumo": f"semana {round(ocupacao * 100)}% ocupada · {_h(livres_semana)}h livres"},
        ))
    return sorted(out, key=lambda a: (a.detalhes["ocupacao_semana_pct"], a.funcionario.nome))
