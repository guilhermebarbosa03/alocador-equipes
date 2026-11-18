from datetime import date

from app import config

from .conftest import csrf, em


def _func(client, nome, **kw):
    corpo = {"nome": nome, "papel": "tecnico", "especialidades": [{"nome": "Elétrica", "nivel": 4}],
             "certificacoes": [{"nome": "NR10"}], **kw}
    r = client.post("/api/funcionarios", json=corpo)
    assert r.status_code == 201, r.text
    return r.json()


def _tarefa(client, **kw):
    corpo = {"titulo": "Visita", "inicio": em(1, 8).isoformat(), "fim": em(1, 17).isoformat(),
             "requisitos": [{"papel": "tecnico", "quantidade": 1, "certificacoes": ["NR10"]}], **kw}
    r = client.post("/api/tarefas", json=corpo)
    assert r.status_code == 201, r.text
    return r.json()


def test_fluxo_completo_pela_api(client):
    ana = _func(client, "Ana", anos_experiencia=8)
    _func(client, "Beto", anos_experiencia=2)
    t = _tarefa(client)

    prop = client.post(f"/api/tarefas/{t['id']}/propor").json()
    assert prop["completa"] is True
    assert prop["vagas"][0]["escolhidos"][0]["nome"] == "Ana"
    assert prop["vagas"][0]["alternativas"][0]["nome"] == "Beto"

    r = client.post(f"/api/tarefas/{t['id']}/confirmar")
    assert r.status_code == 200 and r.json()[0]["funcionario_nome"] == "Ana"
    assert client.get(f"/api/tarefas/{t['id']}").json()["status"] == "alocada"

    agenda = client.get(
        f"/api/funcionarios/{ana['id']}/agenda",
        params={"inicio": em(0, 0).isoformat(), "fim": em(7, 0).isoformat()},
    ).json()
    assert [e["tipo"] for e in agenda] == ["tarefa"]

    livres = client.get(
        "/api/funcionarios/disponiveis", params={"inicio": em(1, 9).isoformat(), "fim": em(1, 10).isoformat()}
    ).json()
    assert [x["nome"] for x in livres] == ["Beto"]

    r = client.post(
        f"/api/funcionarios/{ana['id']}/indisponibilidades",
        json={"inicio": em(1, 0).isoformat(), "fim": em(2, 0).isoformat(), "motivo": "atestado"},
    )
    assert r.status_code == 201 and r.json()["alocacoes_afetadas"][0]["tarefa_id"] == t["id"]


def test_datas_com_fuso_viram_horario_local(client):
    """Antes: '...Z' de um cliente da API ao lado de datas sem fuso do painel dava TypeError (500)."""
    _func(client, "Ana")
    t1 = _tarefa(client)  # sem fuso
    client.post(f"/api/tarefas/{t1['id']}/propor")
    client.post(f"/api/tarefas/{t1['id']}/confirmar")

    # 11:00Z = 08:00 em Brasília: mesma hora da tarefa 1, então conflita
    t2 = _tarefa(client, inicio="2026-11-17T11:00:00Z", fim="2026-11-17T15:00:00-00:00")
    assert t2["inicio"] == "2026-11-17T08:00:00" and t2["fim"] == "2026-11-17T12:00:00"
    prop = client.post(f"/api/tarefas/{t2['id']}/propor")
    assert prop.status_code == 200
    assert "já alocado" in prop.json()["vagas"][0]["rejeitados"][0]["motivos"][0]

    livres = client.get("/api/funcionarios/disponiveis",
                        params={"inicio": "2026-11-17T08:00:00-03:00", "fim": "2026-11-17T09:00:00-03:00"})
    assert livres.status_code == 200 and livres.json() == []


def test_erros_viram_json_legivel(client):
    assert client.get("/api/tarefas/999").json() == {"detail": "Tarefa #999 não encontrada"}
    r = client.post("/api/tarefas", json={"titulo": "x", "inicio": em(1, 9).isoformat(),
                                          "fim": em(1, 8).isoformat(), "requisitos": [{"papel": "tecnico"}]})
    assert r.status_code == 422
    t = _tarefa(client)
    r = client.post(f"/api/tarefas/{t['id']}/confirmar")
    assert r.status_code == 409


def test_desativar_avisa_alocacoes_futuras_e_reativar(client):
    ana = _func(client, "Ana")
    t = _tarefa(client)
    client.post(f"/api/tarefas/{t['id']}/propor")
    client.post(f"/api/tarefas/{t['id']}/confirmar")

    r = client.delete(f"/api/funcionarios/{ana['id']}")
    assert r.status_code == 409 and "Ana" in r.json()["problemas"]
    assert client.get(f"/api/funcionarios/{ana['id']}").json()["ativo"] is True

    r = client.delete(f"/api/funcionarios/{ana['id']}", params={"forcar": True})
    assert r.status_code == 200 and r.json()["alocacoes_futuras"][0]["tarefa_id"] == t["id"]
    assert client.get(f"/api/funcionarios/{ana['id']}").json()["ativo"] is False

    r = client.post(f"/api/funcionarios/{ana['id']}/reativar")
    assert r.status_code == 200 and r.json()["ativo"] is True

    corpo = {"nome": "Ana", "papel": "tecnico", "ativo": False}
    assert client.put(f"/api/funcionarios/{ana['id']}", json=corpo).json()["ativo"] is False
    assert client.put(f"/api/funcionarios/{ana['id']}", json={**corpo, "ativo": True}).json()["ativo"] is True


def test_nao_cancela_alocacao_de_tarefa_concluida(client):
    _func(client, "Ana")
    t = _tarefa(client)
    client.post(f"/api/tarefas/{t['id']}/propor")
    [aloc] = client.post(f"/api/tarefas/{t['id']}/confirmar").json()
    client.post(f"/api/tarefas/{t['id']}/concluir")
    r = client.delete(f"/api/alocacoes/{aloc['id']}")
    assert r.status_code == 409 and "concluida" in r.json()["detail"]


def test_editar_e_remarcar_pela_api(client):
    _func(client, "Ana")
    t = _tarefa(client)
    client.post(f"/api/tarefas/{t['id']}/propor")
    client.post(f"/api/tarefas/{t['id']}/confirmar")
    vaga = t["requisitos"][0]

    corpo = {"titulo": "Visita longa", "inicio": em(1, 8).isoformat(), "fim": em(1, 17).isoformat(),
             "requisitos": [{**vaga, "quantidade": 2}, {"papel": "tecnico", "quantidade": 1}]}
    r = client.put(f"/api/tarefas/{t['id']}", json=corpo)
    assert r.status_code == 200, r.text
    assert r.json()["tarefa"]["titulo"] == "Visita longa" and len(r.json()["tarefa"]["requisitos"]) == 2
    assert r.json()["tarefa"]["status"] == "parcial" and r.json()["problemas"] == {}

    # não dá para remover a vaga em que a Ana está confirmada
    r = client.put(f"/api/tarefas/{t['id']}", json={**corpo, "requisitos": [{"papel": "tecnico", "quantidade": 1}]})
    assert r.status_code == 409 and "remover a vaga" in r.json()["detail"]

    r = client.post(f"/api/tarefas/{t['id']}/remarcar", json={"inicio": em(3, 8).isoformat(), "fim": em(3, 12).isoformat()})
    assert r.status_code == 200 and r.json()["tarefa"]["inicio"] == em(3, 8).isoformat()


def test_filtros_de_tarefas_e_vencimentos(client):
    _func(client, "Ana", certificacoes=[{"nome": "NR35", "validade": "2026-11-20"}])
    a = _tarefa(client, titulo="Campo", tipo="campo", contrato="CT-1")
    b = _tarefa(client, titulo="Interna", inicio=em(10, 8).isoformat(), fim=em(10, 17).isoformat())
    client.post(f"/api/tarefas/{b['id']}/cancelar")

    ids = lambda **p: [x["id"] for x in client.get("/api/tarefas", params=p).json()]  # noqa: E731
    assert ids(status="ativas") == [a["id"]]
    assert ids(tipo="campo") == [a["id"]]
    assert ids(contrato="ct-1") == [a["id"]]
    assert ids(de=em(5, 0).date().isoformat()) == [b["id"]]

    venc = client.get("/api/certificacoes/vencimentos", params={"dias": 30}).json()
    assert [(v["nome"], v["certificacao"], v["dias"]) for v in venc] == [("Ana", "NR35", 5)]


def test_contrato_pela_api_vale_para_toda_a_equipe(client):
    _func(client, "Ana")  # só NR10
    r = client.post("/api/contratos", json={"codigo": "ct-9", "certificacoes": ["NR35"]})
    assert r.status_code == 201 and r.json()["codigo"] == "CT-9"
    t = _tarefa(client, contrato="CT-9")
    prop = client.post(f"/api/tarefas/{t['id']}/propor").json()
    motivos = prop["vagas"][0]["rejeitados"][0]["motivos"]
    assert any("NR35" in m and "contrato CT-9" in m for m in motivos)


def test_auditoria_registra_forcado_com_motivo(client):
    _func(client, "Ana", certificacoes=[])
    t = _tarefa(client)
    vaga = t["requisitos"][0]["id"]
    ana = client.get("/api/funcionarios").json()[0]["id"]
    r = client.post(f"/api/tarefas/{t['id']}/confirmar", json={
        "escolhas": [{"requisito_id": vaga, "funcionario_id": ana}], "forcar": True, "motivo": "cliente pediu a Ana",
    })
    assert r.status_code == 200 and r.json()[0]["justificativa"]["motivo_forcar"] == "cliente pediu a Ana"
    [reg] = client.get("/api/auditoria", params={"acao": "forcar"}).json()
    assert reg["autor"] == "api" and "cliente pediu a Ana" in reg["resumo"] and reg["detalhes"]["violacoes"]


def test_token_da_api(client, monkeypatch):
    monkeypatch.setattr(config, "API_TOKEN", "segredo")
    assert client.get("/api/tarefas").status_code == 401
    assert client.get("/api/tarefas", headers={"Authorization": "Bearer errado"}).status_code == 401
    assert client.get("/api/tarefas", headers={"Authorization": "Bearer segredo"}).status_code == 200


def test_paginas_do_painel(client):
    f = _func(client, "Ana")
    t = _tarefa(client)
    client.post(f"/api/tarefas/{t['id']}/propor")
    client.post("/api/contratos", json={"codigo": "CT-1", "texto": "Cláusula 1. EPI obrigatório."})
    for url in ("/", "/funcionarios", f"/funcionarios/{f['id']}", "/tarefas/nova", f"/tarefas/{t['id']}",
                "/tarefas", "/tarefas?status=todas&tipo=campo", f"/tarefas/{t['id']}/editar", f"/tarefas/{t['id']}/os",
                "/semana", "/semana?inicio=2026-11-16&papel=tecnico", "/contratos", "/contratos/1", "/auditoria"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    assert "Ana" in client.get(f"/tarefas/{t['id']}").text
    assert "Ana" in client.get("/semana?inicio=2026-11-16").text
    erro = client.get("/tarefas/999")
    assert erro.status_code == 404 and "<nav>" in erro.text  # erro com o layout do painel


def test_formularios_do_painel(client):
    token = csrf(client)
    r = client.post("/funcionarios", data={
        "nome": "Caio", "papel": "tecnico", "especialidades": "Elétrica:4, SPDA",
        "certificacoes": "NR10:2027-05-01, NR35", "disponivel_viagem": "on", "csrf": token,
    }, follow_redirects=False)
    assert r.status_code == 303
    caio = client.get("/api/funcionarios").json()[0]
    assert {(e["nome"], e["nivel"]) for e in caio["especialidades"]} == {("Elétrica", 4), ("SPDA", 3)}

    r = client.post("/tarefas", data={
        "titulo": "SPDA galpão", "tipo": "campo", "inicio": "2026-11-17T08:00", "fim": "2026-11-17T17:00",
        "req_papel_0": "tecnico", "req_qtd_0": "1", "req_esp_0": "SPDA:2", "req_cert_0": "NR35",
        "req_papel_7": "tecnico", "req_qtd_7": "1",  # linha extra criada pelo botão "+ Adicionar função"
        "csrf": token,
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[2])
    assert len(client.get(f"/api/tarefas/{tid}").json()["requisitos"]) == 2
    assert client.post(f"/tarefas/{tid}/propor", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert client.post(f"/tarefas/{tid}/confirmar", data={"csrf": token}, follow_redirects=False).status_code == 303
    assert client.get(f"/api/tarefas/{tid}").json()["status"] == "parcial"

    r = client.post("/funcionarios", data={"nome": "", "papel": "tecnico", "csrf": token})
    assert r.status_code == 400


def test_post_sem_token_csrf_e_bloqueado(client):
    assert client.post("/funcionarios", data={"nome": "X", "papel": "tecnico"}).status_code == 403
    token = csrf(client)
    r = client.post("/funcionarios", data={"nome": "X", "papel": "tecnico", "csrf": token},
                    headers={"Origin": "https://site-malicioso.example"})
    assert r.status_code == 403
    assert client.get("/api/funcionarios").json() == []


def test_aviso_de_tela_vai_em_cookie_e_some_no_f5(client):
    token = csrf(client)
    r = client.post("/funcionarios", data={"nome": "Caio", "papel": "tecnico", "csrf": token}, follow_redirects=False)
    assert "?" not in r.headers["location"] and "flash=" in r.headers["set-cookie"]
    pagina = client.get(r.headers["location"])
    assert "cadastrado(a)" in pagina.text
    assert "cadastrado(a)" not in client.get(r.headers["location"]).text  # F5


def test_erro_de_validacao_nao_remarca_o_checkbox(client):
    token = csrf(client)
    r = client.post("/funcionarios", data={"nome": "Caio", "papel": "tecnico", "especialidades": "Elétrica:9",
                                           "csrf": token})
    assert r.status_code == 400 and "nível inválido" in r.text
    assert 'name="disponivel_viagem" checked' not in r.text and 'value="Caio"' in r.text


def test_editar_cadastro_com_erro_mantem_o_que_foi_digitado(client):
    f = _func(client, "Ana")
    token = csrf(client)
    r = client.post(f"/funcionarios/{f['id']}", data={"nome": "Ana Maria", "papel": "tecnico",
                                                       "telegram_id": "@ana", "csrf": token})
    assert r.status_code == 400 and "ID numérico" in r.text and 'value="Ana Maria"' in r.text
    assert client.get(f"/api/funcionarios/{f['id']}").json()["nome"] == "Ana"


def test_login_do_painel(client, monkeypatch):
    monkeypatch.setattr(config, "PAINEL_SENHA", "s3nha")
    r = client.get("/tarefas", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/login?proximo=/tarefas")
    token = csrf(client)
    assert client.post("/login", data={"senha": "errada", "csrf": token}).status_code == 401
    r = client.post("/login", data={"senha": "s3nha", "proximo": "/tarefas", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/tarefas"
    assert client.get("/tarefas").status_code == 200
    assert client.get("/saude").status_code == 200


def test_agenda_ics_com_token(client):
    from app.seguranca import token_calendario

    f = _func(client, "Ana")
    t = _tarefa(client, local="Betim, MG")
    client.post(f"/api/tarefas/{t['id']}/propor")
    client.post(f"/api/tarefas/{t['id']}/confirmar")
    assert client.get(f"/funcionarios/{f['id']}/agenda.ics?token=errado").status_code == 403
    r = client.get(f"/funcionarios/{f['id']}/agenda.ics?token={token_calendario(f['id'])}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/calendar")
    assert "DTSTART:20261117T110000Z" in r.text  # 08:00 em Brasília
    assert "LOCATION:Betim\\, MG" in r.text and "STATUS:CONFIRMED" in r.text


def test_exportar_csv(client):
    _func(client, "Ana")
    t = _tarefa(client, titulo="Visita; com ponto e vírgula")
    client.post(f"/api/tarefas/{t['id']}/propor")
    client.post(f"/api/tarefas/{t['id']}/confirmar")
    r = client.get("/tarefas/exportar.csv?status=todas")
    assert r.status_code == 200 and r.text.startswith("﻿id;titulo")
    assert '"Visita; com ponto e vírgula"' in r.text and "Ana" in r.text


def test_saude_confere_o_banco(client):
    assert client.get("/saude").json() == {"ok": True}


def test_data_param_invalida_nao_quebra(client):
    assert client.get("/semana?inicio=ontem").status_code == 200
    assert client.get("/tarefas?de=xx").status_code == 200
    assert date.fromisoformat("2026-11-16")  # sanity
