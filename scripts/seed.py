"""Dados de exemplo para testar o painel.

Uso:  python -m scripts.seed           (só roda com o banco vazio)
      python -m scripts.seed --reset   (APAGA tudo e recria)
"""

import sys
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select

from app import schemas
from app import services as sv
from app.db import Base, SessionLocal, engine, init_db
from app.models import Funcionario


def main() -> None:
    if "--reset" in sys.argv:
        Base.metadata.drop_all(engine)
    init_db()
    db = SessionLocal()
    if db.scalar(select(func.count()).select_from(Funcionario)):
        print("O banco já tem dados. Use --reset para apagar e recriar.")
        return

    hoje = date.today()
    seg = hoje + timedelta(days=(7 - hoje.weekday()) % 7 or 7)  # próxima segunda-feira

    def em(dias: int, hora: int, minuto: int = 0) -> datetime:
        return datetime.combine(seg + timedelta(days=dias), time(hora, minuto))

    valido = (hoje + timedelta(days=365)).isoformat()
    vence_logo = (seg + timedelta(days=1)).isoformat()  # vence na terça da semana que vem
    vencido = (hoje - timedelta(days=30)).isoformat()

    pessoas = [
        ("Ana Souza", "engenheiro", 12, True, 44, [("Elétrica", 5), ("Subestações", 4), ("Gestão de obras", 4)],
         [("NR10", valido), ("NR35", valido), ("CREA", None)], "Coordenou retrofit de 3 subestações 138kV."),
        ("Bruno Lima", "engenheiro", 6, True, 44, [("Elétrica", 4), ("Automação", 4)],
         [("NR10", valido), ("NR35", vence_logo), ("CREA", None)], "Projetos de automação industrial e CLP."),
        ("Carla Mendes", "engenheiro", 9, False, 40, [("Civil", 5), ("Gestão de obras", 3)],
         [("NR35", valido), ("CREA", None)], "Fundações e estruturas metálicas. Não viaja."),
        ("Diego Rocha", "tecnico", 8, True, 44, [("Elétrica", 4), ("SPDA", 3)],
         [("NR10", valido), ("NR35", valido)], "Instalações de média tensão e SPDA."),
        ("Eduardo Alves", "tecnico", 4, True, 44, [("Elétrica", 3), ("Automação", 4)],
         [("NR10", valido)], "Comissionamento de painéis e CLP."),
        ("Fernanda Costa", "tecnico", 11, True, 44, [("Elétrica", 5), ("Subestações", 4)],
         [("NR10", valido), ("NR35", valido)], "Manutenção de subestações e ensaios."),
        ("Gabriel Nunes", "tecnico", 1, True, 44, [("Elétrica", 2)],
         [("NR10", valido)], "Técnico júnior."),
        ("Helena Prado", "tecnico", 5, True, 44, [("Refrigeração", 4), ("Elétrica", 3)],
         [("NR10", vencido)], "Climatização e refrigeração industrial."),
        ("Igor Santos", "tecnico", 7, True, 30, [("Elétrica", 4), ("SPDA", 4)],
         [("NR10", valido), ("NR35", valido)], "SPDA e aterramento. Jornada de 30h."),
        ("Juliana Reis", "tecnico de seguranca", 6, True, 44, [("Segurança do trabalho", 5)],
         [("NR10", valido), ("NR35", valido)], "Técnica de segurança, APR e PT."),
    ]
    ids = {}
    for nome, papel, anos, viaja, carga, esp, cert, resumo in pessoas:
        f = sv.criar_funcionario(db, schemas.FuncionarioIn(
            nome=nome, papel=papel, anos_experiencia=anos, disponivel_viagem=viaja,
            carga_horaria_semanal=carga, resumo=resumo, base="Belo Horizonte",
            especialidades=[{"nome": n, "nivel": nv} for n, nv in esp],
            certificacoes=[{"nome": n, "validade": v} for n, v in cert],
        ))
        ids[nome] = f.id

    sv.criar_indisponibilidade(db, db.get(Funcionario, ids["Fernanda Costa"]),
                               schemas.IndisponibilidadeIn(inicio=em(0, 0), fim=em(5, 0), motivo="férias"))
    sv.criar_indisponibilidade(db, db.get(Funcionario, ids["Gabriel Nunes"]),
                               schemas.IndisponibilidadeIn(inicio=em(3, 8), fim=em(3, 17), motivo="treinamento"))

    t1 = sv.criar_tarefa(db, schemas.TarefaIn(
        titulo="Manutenção preventiva – Subestação Norte", tipo="campo", local="Contagem/MG",
        contrato="CT-2026-014", prioridade=4, inicio=em(1, 8), fim=em(3, 17),
        descricao="Inspeção termográfica, reaperto e ensaios em transformadores.",
        requisitos=[
            {"papel": "engenheiro", "quantidade": 1,
             "especialidades": [{"nome": "Elétrica", "nivel_min": 4}, {"nome": "Subestações", "nivel_min": 3}],
             "certificacoes": ["NR10"]},
            {"papel": "tecnico", "quantidade": 2, "especialidades": [{"nome": "Elétrica", "nivel_min": 3}],
             "certificacoes": ["NR10", "NR35"], "desejaveis": ["Subestações"]},
        ],
    ))
    sv.gerar_proposta(db, t1)
    sv.confirmar(db, t1)

    sv.criar_tarefa(db, schemas.TarefaIn(
        titulo="Instalação de SPDA – Galpão Cliente X", tipo="campo", local="Betim/MG",
        contrato="CT-2026-021", prioridade=3, inicio=em(2, 8), fim=em(2, 17),
        requisitos=[
            {"papel": "tecnico", "quantidade": 2, "especialidades": [{"nome": "SPDA", "nivel_min": 3}],
             "certificacoes": ["NR35"]},
            {"papel": "tecnico de seguranca", "quantidade": 1, "certificacoes": ["NR35"]},
        ],
    ))
    sv.criar_tarefa(db, schemas.TarefaIn(
        titulo="Revisão do projeto de automação – Linha 3", tipo="interna", local="Escritório",
        prioridade=2, inicio=em(0, 9), fim=em(0, 12),
        requisitos=[{"papel": "engenheiro", "quantidade": 1,
                     "especialidades": [{"nome": "Automação", "nivel_min": 3}]}],
    ))

    print(f"Pronto: {len(pessoas)} funcionários e 3 tarefas (semana de {seg:%d/%m/%Y}).")
    db.close()


if __name__ == "__main__":
    main()
