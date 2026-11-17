from datetime import datetime

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.orm import Session

from . import schemas
from . import services as sv
from .allocation import disponiveis as calc_disponiveis
from .db import get_db

router = APIRouter()


# ---------- Funcionários ----------


@router.get("/funcionarios", response_model=list[schemas.FuncionarioOut], tags=["funcionários"])
def listar_funcionarios(incluir_inativos: bool = False, papel: str | None = None, db: Session = Depends(get_db)):
    return sv.listar_funcionarios(db, incluir_inativos, papel)


@router.post(
    "/funcionarios", response_model=schemas.FuncionarioOut, status_code=status.HTTP_201_CREATED, tags=["funcionários"]
)
def criar_funcionario(dados: schemas.FuncionarioIn, db: Session = Depends(get_db)):
    return sv.criar_funcionario(db, dados)


@router.get("/funcionarios/disponiveis", response_model=list[schemas.CandidatoOut], tags=["funcionários"])
def funcionarios_disponiveis(
    inicio: datetime, fim: datetime, papel: str | None = None, db: Session = Depends(get_db)
):
    """Quem está livre no intervalo (sem ausência, sem conflito e com carga sobrando)."""
    if fim <= inicio:
        raise sv.ErroNegocio("fim deve ser depois do início")
    return [a.to_dict() for a in calc_disponiveis(db, inicio, fim, papel)]


@router.get("/funcionarios/{funcionario_id}", response_model=schemas.FuncionarioOut, tags=["funcionários"])
def obter_funcionario(funcionario_id: int, db: Session = Depends(get_db)):
    return sv.obter_funcionario(db, funcionario_id)


@router.put("/funcionarios/{funcionario_id}", response_model=schemas.FuncionarioOut, tags=["funcionários"])
def atualizar_funcionario(funcionario_id: int, dados: schemas.FuncionarioIn, db: Session = Depends(get_db)):
    return sv.atualizar_funcionario(db, sv.obter_funcionario(db, funcionario_id), dados)


@router.delete("/funcionarios/{funcionario_id}", response_model=schemas.FuncionarioOut, tags=["funcionários"])
def desativar_funcionario(funcionario_id: int, db: Session = Depends(get_db)):
    """Desativa (não apaga) para manter o histórico de alocações."""
    return sv.desativar_funcionario(db, sv.obter_funcionario(db, funcionario_id))


@router.get(
    "/funcionarios/{funcionario_id}/indisponibilidades",
    response_model=list[schemas.IndisponibilidadeOut],
    tags=["disponibilidade"],
)
def listar_indisponibilidades(funcionario_id: int, db: Session = Depends(get_db)):
    f = sv.obter_funcionario(db, funcionario_id)
    return sorted(f.indisponibilidades, key=lambda i: i.inicio)


@router.post(
    "/funcionarios/{funcionario_id}/indisponibilidades",
    response_model=schemas.IndisponibilidadeCriada,
    status_code=status.HTTP_201_CREATED,
    tags=["disponibilidade"],
)
def criar_indisponibilidade(
    funcionario_id: int, dados: schemas.IndisponibilidadeIn, db: Session = Depends(get_db)
):
    """Registra ausência e devolve as alocações confirmadas que ela atinge."""
    ind, afetadas = sv.criar_indisponibilidade(db, sv.obter_funcionario(db, funcionario_id), dados)
    return {"indisponibilidade": ind, "alocacoes_afetadas": afetadas}


@router.delete(
    "/indisponibilidades/{indisponibilidade_id}", status_code=status.HTTP_204_NO_CONTENT, tags=["disponibilidade"]
)
def remover_indisponibilidade(indisponibilidade_id: int, db: Session = Depends(get_db)):
    sv.remover_indisponibilidade(db, indisponibilidade_id)


@router.get(
    "/funcionarios/{funcionario_id}/agenda", response_model=list[schemas.EventoAgenda], tags=["disponibilidade"]
)
def agenda(
    funcionario_id: int,
    inicio: datetime = Query(...),
    fim: datetime = Query(...),
    db: Session = Depends(get_db),
):
    return sv.agenda(db, sv.obter_funcionario(db, funcionario_id), inicio, fim)


# ---------- Tarefas ----------


@router.get("/tarefas", response_model=list[schemas.TarefaOut], tags=["tarefas"])
def listar_tarefas(status: str | None = None, db: Session = Depends(get_db)):
    return sv.listar_tarefas(db, status)


@router.post("/tarefas", response_model=schemas.TarefaOut, status_code=status.HTTP_201_CREATED, tags=["tarefas"])
def criar_tarefa(dados: schemas.TarefaIn, db: Session = Depends(get_db)):
    return sv.criar_tarefa(db, dados)


@router.get("/tarefas/{tarefa_id}", response_model=schemas.TarefaOut, tags=["tarefas"])
def obter_tarefa(tarefa_id: int, db: Session = Depends(get_db)):
    return sv.obter_tarefa(db, tarefa_id)


@router.get("/tarefas/{tarefa_id}/alocacoes", response_model=list[schemas.AlocacaoOut], tags=["alocação"])
def listar_alocacoes(tarefa_id: int, incluir_canceladas: bool = False, db: Session = Depends(get_db)):
    t = sv.obter_tarefa(db, tarefa_id)
    return [a for a in t.alocacoes if incluir_canceladas or a.status != "cancelada"]


@router.post("/tarefas/{tarefa_id}/propor", response_model=schemas.PropostaOut, tags=["alocação"])
def propor(tarefa_id: int, db: Session = Depends(get_db)):
    """Roda o motor: escolhe os melhores por vaga, lista alternativas e explica cada rejeição."""
    return sv.gerar_proposta(db, sv.obter_tarefa(db, tarefa_id)).to_dict()


@router.post("/tarefas/{tarefa_id}/confirmar", response_model=list[schemas.AlocacaoOut], tags=["alocação"])
def confirmar(tarefa_id: int, dados: schemas.ConfirmarIn | None = None, db: Session = Depends(get_db)):
    dados = dados or schemas.ConfirmarIn()
    return sv.confirmar(db, sv.obter_tarefa(db, tarefa_id), dados.escolhas, dados.forcar)


@router.post("/tarefas/{tarefa_id}/concluir", response_model=schemas.TarefaOut, tags=["tarefas"])
def concluir(tarefa_id: int, db: Session = Depends(get_db)):
    return sv.encerrar_tarefa(db, sv.obter_tarefa(db, tarefa_id), "concluida")


@router.post("/tarefas/{tarefa_id}/cancelar", response_model=schemas.TarefaOut, tags=["tarefas"])
def cancelar(tarefa_id: int, db: Session = Depends(get_db)):
    return sv.encerrar_tarefa(db, sv.obter_tarefa(db, tarefa_id), "cancelada")


@router.delete("/alocacoes/{alocacao_id}", response_model=schemas.AlocacaoOut, tags=["alocação"])
def cancelar_alocacao(alocacao_id: int, db: Session = Depends(get_db)):
    return sv.cancelar_alocacao(db, sv.obter_alocacao(db, alocacao_id))
