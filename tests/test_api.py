from .conftest import em


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


def test_erros_viram_json_legivel(client):
    assert client.get("/api/tarefas/999").json() == {"detail": "Tarefa #999 não encontrada"}
    r = client.post("/api/tarefas", json={"titulo": "x", "inicio": em(1, 9).isoformat(),
                                          "fim": em(1, 8).isoformat(), "requisitos": [{"papel": "tecnico"}]})
    assert r.status_code == 422
    t = _tarefa(client)
    r = client.post(f"/api/tarefas/{t['id']}/confirmar")
    assert r.status_code == 409


def test_paginas_do_painel(client):
    f = _func(client, "Ana")
    t = _tarefa(client)
    client.post(f"/api/tarefas/{t['id']}/propor")
    for url in ("/", "/funcionarios", f"/funcionarios/{f['id']}", "/tarefas/nova", f"/tarefas/{t['id']}"):
        r = client.get(url)
        assert r.status_code == 200, (url, r.text[:500])
    assert "Ana" in client.get(f"/tarefas/{t['id']}").text


def test_formularios_do_painel(client):
    r = client.post("/funcionarios", data={
        "nome": "Caio", "papel": "tecnico", "especialidades": "Elétrica:4, SPDA",
        "certificacoes": "NR10:2027-05-01, NR35", "disponivel_viagem": "on",
    }, follow_redirects=False)
    assert r.status_code == 303
    caio = client.get("/api/funcionarios").json()[0]
    assert {(e["nome"], e["nivel"]) for e in caio["especialidades"]} == {("Elétrica", 4), ("SPDA", 3)}

    r = client.post("/tarefas", data={
        "titulo": "SPDA galpão", "tipo": "campo", "inicio": "2026-11-17T08:00", "fim": "2026-11-17T17:00",
        "req_papel_0": "tecnico", "req_qtd_0": "1", "req_esp_0": "SPDA:2", "req_cert_0": "NR35",
    }, follow_redirects=False)
    assert r.status_code == 303
    tid = int(r.headers["location"].split("/")[2].split("?")[0])
    assert client.post(f"/tarefas/{tid}/propor", follow_redirects=False).status_code == 303
    assert client.post(f"/tarefas/{tid}/confirmar", follow_redirects=False).status_code == 303
    assert client.get(f"/api/tarefas/{tid}").json()["status"] == "alocada"

    r = client.post("/funcionarios", data={"nome": "", "papel": "tecnico"})
    assert r.status_code == 400
