import pytest
from starlette.testclient import TestClient

from rigol_mcp import server, web


@pytest.fixture
def client(fake):
    server.scope_screenshot()
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
    name = server.store.get("screenshots").name
    assert name in r.text
    # the cookie set above lets the browser load images without the token in the URL
    img = client.get(f"/screenshots/{name}")
    assert img.status_code == 200 and img.content.startswith(b"\x89PNG")


def test_file_route_rejects_other_names(client):
    h = {"Authorization": "Bearer s3cret"}
    assert client.get("/screenshots/..%2F..%2Fetc%2Fpasswd", headers=h).status_code == 404
    assert client.get("/screenshots/latest", headers=h).status_code == 200
