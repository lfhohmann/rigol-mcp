import pytest

from fake_scope import FakeScope
from rigol_mcp import app, server  # noqa: F401  (server registers all tools)
from rigol_mcp.sessions import registry
from rigol_mcp.share import shared


@pytest.fixture
def fake(tmp_path):
    registry.reset()
    shared.clear()
    app.session_var.set("stdio")
    app.client_var.set("stdio")
    scope = FakeScope()
    app.configure(app.Config(host="127.0.0.1", port=scope.port, data_dir=tmp_path, keep_screenshots=3, keep_exports=2))
    yield scope
    scope.close()
