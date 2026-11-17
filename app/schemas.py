from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Orm(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class _Intervalo(BaseModel):
    inicio: datetime
    fim: datetime

    @model_validator(mode="after")
    def _fim_depois_do_inicio(self):
        if self.fim <= self.inicio:
            raise ValueError("fim deve ser depois do início")
        return self


# ---------- Funcionários ----------


class EspecialidadeIn(_Orm):
    nome: str = Field(min_length=1, max_length=80)
    nivel: int = Field(3, ge=1, le=5)


class CertificacaoIn(_Orm):
    nome: str = Field(min_length=1, max_length=80)
    validade: date | None = None


class FuncionarioIn(BaseModel):
    nome: str = Field(min_length=1, max_length=120)
    papel: str = Field(min_length=1, max_length=60, examples=["tecnico", "engenheiro"])
    email: str | None = None
    telegram_id: str | None = None
    carga_horaria_semanal: float = Field(44, gt=0, le=80)
    horas_max_dia: float = Field(8, gt=0, le=24)
    anos_experiencia: float = Field(0, ge=0, le=60)
    disponivel_viagem: bool = True
    base: str | None = None
    resumo: str | None = None
    especialidades: list[EspecialidadeIn] = []
    certificacoes: list[CertificacaoIn] = []


class FuncionarioOut(FuncionarioIn, _Orm):
    id: int
    ativo: bool


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
    papel: str = Field(min_length=1, max_length=60)
    quantidade: int = Field(1, ge=1, le=50)
    especialidades: list[EspecialidadeReq] = []
    desejaveis: list[str] = []
    certificacoes: list[str] = []
    anos_experiencia_min: float = Field(0, ge=0)


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


class AlocacaoOut(_Orm):
    id: int
    tarefa_id: int
    requisito_id: int | None
    funcionario_id: int
    funcionario_nome: str
    status: str
    score: float | None
    justificativa: dict[str, Any] | None


class Escolha(BaseModel):
    requisito_id: int
    funcionario_id: int


class ConfirmarIn(BaseModel):
    """Sem `escolhas`, confirma a proposta atual. Com `escolhas`, confirma exatamente essas pessoas."""

    escolhas: list[Escolha] | None = None
    forcar: bool = Field(False, description="Confirma mesmo violando regras (fica registrado na justificativa)")


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
