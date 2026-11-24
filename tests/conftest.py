import pytest

from fake_scope import FakeScope
from rigol_mcp import server


@pytest.fixture
def fake(tmp_path):
    scope = FakeScope()
    server.configure(server.Config(host="127.0.0.1", port=scope.port, data_dir=tmp_path, keep_screenshots=3))
    yield scope
    scope.close()
