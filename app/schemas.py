from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .relogio import local
from .texto import norm


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _Intervalo(BaseModel):
    inicio: datetime
    fim: datetime

    @field_validator("inicio", "fim", mode="after")
    @classmethod
    def _horario_local(cls, v: datetime) -> datetime:
        # "2026-11-18T08:00:00Z" ou "-03:00" viram horário local sem fuso, como o resto do banco.
        # Misturar os dois tipos fazia a comparação de horários quebrar com TypeError.
        return local(v)

    @model_validator(mode="after")
    def _fim_depois_do_inicio(self):
        if self.fim <= self.inicio:
            raise ValueError("fim deve ser depois do início")
        return self


def _sem_repetir(nomes: list[str]) -> list[str]:
    vistos, out = set(), []
    for n in (x.strip() for x in nomes):
        if n and norm(n) not in vistos:
            vistos.add(norm(n))
            out.append(n)
    return out


# ---------- Funcionários ----------


class EspecialidadeIn(_Orm):
    nome: str = Field(min_length=1, max_length=80)
    nivel: int = Field(3, ge=1, le=5)


class CertificacaoIn(_Orm):
    nome: str = Field(min_length=1, max_length=80)
    validade: date | None = None


class _FuncionarioBase(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    papel: str = Field(min_length=1, max_length=60, examples=["tecnico", "engenheiro"])
    email: str | None = None
    telegram_id: str | None = Field(None, description="ID numérico do Telegram (a pessoa descobre mandando /id ao bot)")
    carga_horaria_semanal: float = Field(44, gt=0, le=80)
    horas_max_dia: float = Field(8, gt=0, le=24)
    anos_experiencia: float = Field(0, ge=0, le=60)
    disponivel_viagem: bool = True
    base: str | None = None
    resumo: str | None = None
    especialidades: list[EspecialidadeIn] = []
    certificacoes: list[CertificacaoIn] = []


class FuncionarioIn(_FuncionarioBase):
    ativo: bool | None = Field(None, description="Só na edição: true reativa, false desativa. Vazio mantém.")

    @field_validator("telegram_id", mode="before")
    @classmethod
    def _telegram_numerico(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        if not v.lstrip("-").isdigit():
            raise ValueError("use o ID numérico do Telegram (a pessoa manda /id ao bot para descobrir)")
        return v

    @model_validator(mode="after")
    def _sem_duplicadas(self):
        # Mesma especialidade duas vezes: fica o maior nível.
        esp: dict[str, EspecialidadeIn] = {}
        for e in self.especialidades:
            k = norm(e.nome)
            if k not in esp or e.nivel > esp[k].nivel:
                esp[k] = EspecialidadeIn(nome=esp[k].nome if k in esp else e.nome.strip(), nivel=e.nivel)
        self.especialidades = list(esp.values())
        # Mesma certificação duas vezes (a vencida e a renovada): fica a de validade mais longa.
        cert: dict[str, CertificacaoIn] = {}
        for c in self.certificacoes:
            k, atual = norm(c.nome), cert.get(norm(c.nome))
            if atual is None or (atual.validade is not None and (c.validade is None or c.validade > atual.validade)):
                cert[k] = CertificacaoIn(nome=atual.nome if atual else c.nome.strip(), validade=c.validade)
        self.certificacoes = list(cert.values())
        return self


class FuncionarioOut(_FuncionarioBase, _Orm):
    id: int
    ativo: bool


class FuncionarioDesativado(BaseModel):
    funcionario: FuncionarioOut
    alocacoes_futuras: list["AlocacaoOut"]


class IndisponibilidadeIn(_Intervalo):
    motivo: str = Field("outro", max_length=120, examples=["férias", "atestado", "treinamento"])


class IndisponibilidadeOut(IndisponibilidadeIn, _Orm):
    id: int
    funcionario_id: int


# ---------- Tarefas ----------


class EspecialidadeReq(BaseModel):
    nome: str = Field(min_length=1, max_length=80)
    nivel_min: int = Field(1, ge=1, le=5)


class RequisitoIn(BaseModel):
    id: int | None = Field(None, description="Só na edição: id da vaga existente. Vazio cria uma vaga nova.")
    papel: str = Field(min_length=1, max_length=60)
    quantidade: int = Field(1, ge=1, le=50)
    especialidades: list[EspecialidadeReq] = []
    desejaveis: list[str] = []
    certificacoes: list[str] = []
    anos_experiencia_min: float = Field(0, ge=0)

    @model_validator(mode="after")
    def _sem_duplicadas(self):
        esp: dict[str, EspecialidadeReq] = {}
        for e in self.especialidades:
            k = norm(e.nome)
            if k not in esp or e.nivel_min > esp[k].nivel_min:
                esp[k] = EspecialidadeReq(nome=esp[k].nome if k in esp else e.nome.strip(), nivel_min=e.nivel_min)
        self.especialidades = list(esp.values())
        self.certificacoes = _sem_repetir(self.certificacoes)
        exigidas = set(esp)
        self.desejaveis = [d for d in _sem_repetir(self.desejaveis) if norm(d) not in exigidas]
        return self


class RequisitoOut(RequisitoIn, _Orm):
    id: int


class TarefaIn(_Intervalo):
    titulo: str = Field(min_length=1, max_length=160)
    descricao: str | None = None
    tipo: Literal["interna", "campo"] = "interna"
    local: str | None = None
    horas_por_pessoa: float | None = Field(None, gt=0)
    prioridade: int = Field(3, ge=1, le=5)
    contrato: str | None = None
    requisitos: list[RequisitoIn] = Field(min_length=1)


class TarefaOut(_Orm):
    id: int
    titulo: str
    descricao: str | None
    tipo: str
    local: str | None
    inicio: datetime
    fim: datetime
    horas_por_pessoa: float | None
    prioridade: int
    contrato: str | None
    status: str
    requisitos: list[RequisitoOut]


class RemarcarIn(_Intervalo):
    pass


class AlocacaoOut(_Orm):
    id: int
    tarefa_id: int
    requisito_id: int | None
    funcionario_id: int
    funcionario_nome: str
    status: str
    score: float | None
    justificativa: dict[str, Any] | None


class TarefaAlterada(BaseModel):
    """Resultado de editar ou remarcar: quem já estava confirmado e agora viola alguma regra."""

    tarefa: TarefaOut
    problemas: dict[str, list[str]]


class Escolha(BaseModel):
    requisito_id: int
    funcionario_id: int


class ConfirmarIn(BaseModel):
    """Sem `escolhas`, confirma a proposta atual. Com `escolhas`, confirma exatamente essas pessoas."""

    escolhas: list[Escolha] | None = None
    forcar: bool = Field(False, description="Confirma mesmo violando regras (fica registrado na auditoria)")
    motivo: str | None = Field(None, max_length=300, description="Por que forçar. Fica na auditoria.")


# ---------- Respostas do motor ----------


class CandidatoOut(BaseModel):
    funcionario_id: int
    nome: str
    papel: str
    score: float | None = None
    resumo: str | None = None
    detalhes: dict[str, Any] | None = None
    motivos: list[str] | None = None
    perfil_ok: bool | None = None  # nos rejeitados: True = tem o perfil, só está ocupado
    remanejar_de: list[int] | None = None  # tarefas de prioridade baixa que, se liberadas, resolvem


class VagaOut(BaseModel):
    requisito_id: int | None
    papel: str
    quantidade: int
    ja_confirmados: list[int]
    escolhidos: list[CandidatoOut]
    alternativas: list[CandidatoOut]
    rejeitados: list[CandidatoOut]
    faltam: int


class PropostaOut(BaseModel):
    tarefa_id: int | None
    completa: bool
    vagas: list[VagaOut]


class EventoAgenda(BaseModel):
    tipo: Literal["tarefa", "indisponibilidade"]
    id: int
    titulo: str
    inicio: datetime
    fim: datetime
    status: str | None = None


class IndisponibilidadeCriada(BaseModel):
    indisponibilidade: IndisponibilidadeOut
    alocacoes_afetadas: list[AlocacaoOut]


class Vencimento(BaseModel):
    funcionario_id: int
    nome: str
    certificacao: str
    validade: date
    dias: int  # negativo = já venceu


# ---------- Contratos e auditoria ----------


class ContratoIn(BaseModel):
    codigo: str = Field(min_length=1, max_length=120, examples=["CT-2026-014"])
    cliente: str | None = None
    certificacoes: list[str] = Field([], description="Obrigatórias para toda a equipe das tarefas do contrato")
    anos_experiencia_min: float = Field(0, ge=0, le=60)
    exige_tecnico_seguranca: bool = False
    observacoes: str | None = None
    texto: str | None = Field(None, description="Cláusulas do contrato, para o agente consultar")

    @field_validator("codigo")
    @classmethod
    def _codigo(cls, v: str) -> str:
        return v.strip().upper()

    @field_validator("certificacoes")
    @classmethod
    def _certs(cls, v: list[str]) -> list[str]:
        return _sem_repetir(v)


class ContratoOut(ContratoIn, _Orm):
    id: int
    atualizado_em: datetime


class AuditoriaOut(_Orm):
    id: int
    quando: datetime
    acao: str
    autor: str
    tarefa_id: int | None
    funcionario_id: int | None
    resumo: str
    detalhes: dict[str, Any] | None


FuncionarioDesativado.model_rebuild()
