def _type(client, name, properties, *, required=None, searchable=False):
    return client.post("/api/entity-types", json={
        "name": name,
        "embed": searchable,
        "json_schema": {
            "type": "object",
            "required": required or [],
            "properties": properties,
        },
    })


def test_plainlist_task_and_grocery_contract(client, app):
    calls = []
    app.extensions["hub"].embedder.embed_documents = lambda documents: calls.append(documents)
    assert _type(client, "task", {
        "title": {"type": "string"},
        "state": {"type": "string", "enum": ["open", "done"]},
        "due_date": {"type": "string", "format": "date-time"},
        "tags": {"type": "array", "items": {"type": "string"}},
    }, required=["title"]).status_code == 201
    assert _type(client, "grocery_item", {
        "list_id": {"type": "string", "pattern": "^[0-9a-f]{32}$"},
        "name": {"type": "string"},
    }, required=["list_id", "name"]).status_code == 201
    list_id = "b" * 32
    grocery = client.post("/api/entities", json={
        "type": "grocery_item", "data": {"list_id": list_id, "name": "Milk"},
    })
    assert grocery.status_code == 201
    assert client.post("/api/entities/query", json={
        "type": "grocery_item",
        "filters": [{"field": "list_id", "op": "eq", "value": list_id}],
    }).get_json()["items"][0]["id"] == grocery.get_json()["entity"]["id"]

    for index in range(201):
        response = client.post("/api/entities", json={
            "type": "task",
            "data": {
                "title": f"Task {index}", "state": "open",
                "due_date": "2026-10-18T12:00:00.000Z", "tags": ["one"],
            },
        })
        assert response.status_code == 201, response.get_json()

    first = client.get("/api/entities?type=task&status=current&limit=1").get_json()["items"][0]
    patched = client.patch(f"/api/entities/{first['id']}", json={
        "state": None, "tags": ["two", "three"],
    })
    assert patched.status_code == 200
    assert "state" not in patched.get_json()["entity"]["data"]
    assert patched.get_json()["entity"]["data"]["tags"] == ["two", "three"]

    result = client.post("/api/entities/query", json={
        "type": "task", "filters": [{"field": "title", "op": "eq", "value": "Task 1"}],
        "limit": 500, "offset": 1,
    })
    assert result.status_code == 200
    assert result.get_json()["items"] == []
    assert result.get_json()["total"] == 1
    paged = client.post("/api/entities/query", json={
        "type": "task", "filters": [{"field": "state", "op": "eq", "value": "open"}],
        "limit": 500,
    }).get_json()
    assert len(paged["items"]) == 200
    assert paged["total"] == 200

    assert client.post(f"/api/entities/{first['id']}/archive", json={}).status_code == 200
    assert client.get("/api/entities?type=task&status=current,archived").get_json()["total"] == 201
    assert client.get("/api/entities?type=task&status=unknown").status_code == 400
    assert calls == []
    assert not app.extensions["hub"].needs_reindex
