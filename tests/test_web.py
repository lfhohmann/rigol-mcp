import pytest
from starlette.testclient import TestClient

from rigol_mcp import app, web
from rigol_mcp.tools_export import scope_export_memory
from rigol_mcp.tools_read import scope_screenshot


@pytest.fixture
def client(fake):
    scope_screenshot()
    app = web.TokenAuth(web.build_app(), "s3cret")
    return TestClient(app)


def test_health_is_public(client):
    assert client.get("/health").text == "ok\n"


def test_auth_required(client):
    assert client.get("/screenshots").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    assert client.get("/screenshots", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_gallery_with_query_token_sets_cookie(client):
    r = client.get("/screenshots?token=s3cret")
    assert r.status_code == 200 and "Scope screenshots" in r.text
    name = app.store.get("screenshots").name
    assert name in r.text
    # the cookie set above lets the browser load images without the token in the URL
    img = client.get(f"/screenshots/{name}")
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG")


def test_file_route_rejects_other_names(client):
    h = {"Authorization": "Bearer s3cret"}
    assert client.get("/screenshots/..%2F..%2Fetc%2Fpasswd", headers=h).status_code == 404
    assert client.get("/screenshots/latest", headers=h).status_code == 200


def test_export_download_and_sidecar(client, fake):
    fake.memory_depth = 500
    out = scope_export_memory(1, stop=True)
    h = {"Authorization": "Bearer s3cret"}
    name = out["capture"]["file"].split("/")[-1]
    assert len(client.get(f"/exports/{name}", headers=h).content) == 500
    assert client.get(f"/exports/{name[:-4]}.json", headers=h).json()["points"] == 500
    assert "Memory exports" in client.get("/screenshots", headers=h).text
