"""HTTP mode: streamable-HTTP MCP endpoint, token auth, and a screenshot gallery."""

from __future__ import annotations

import hmac
import html
import os
from urllib.parse import parse_qs

from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from . import app, server  # noqa: F401  (server registers the tools)

COOKIE = "rigol_token"
PUBLIC_PATHS = {"/health"}


class TokenAuth:
    """ASGI middleware. Accepts the token as 'Authorization: Bearer <token>', as the
    rigol_token cookie, or (GET only, for browsers) as ?token=<token>."""

    def __init__(self, app, token: str | None):
        self.app = app
        self.token = token

    def _ok(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate.encode(), self.token.encode())

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not self.token or scope["path"] in PUBLIC_PATHS:
            return await self.app(scope, receive, send)
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers") or []}
        auth = headers.get("authorization", "")
        bearer = auth[7:] if auth.lower().startswith("bearer ") else None
        cookie = None
        for part in headers.get("cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                cookie = value
        query = None
        if scope["method"] == "GET":
            query = (parse_qs(scope.get("query_string", b"").decode()).get("token") or [None])[0]
        if self._ok(bearer) or self._ok(cookie) or self._ok(query):
            return await self.app(scope, receive, send)
        await PlainTextResponse("unauthorized: missing or wrong token\n", status_code=401)(scope, receive, send)


def _with_cookie(request: Request, response: Response) -> Response:
    token = request.query_params.get("token")
    if token:  # already validated by TokenAuth; remember it so <img> loads work
        response.set_cookie(COOKIE, token, httponly=True, samesite="strict", max_age=30 * 86400)
    return response


GALLERY_CSS = """
:root { color-scheme: light dark; --bg:#f6f6f4; --card:#fff; --fg:#1d1d1b; --dim:#6b6b66; --line:#ddddd8; }
@media (prefers-color-scheme: dark) { :root { --bg:#141413; --card:#1f1f1d; --fg:#ececea; --dim:#9a9a94; --line:#33332f; } }
body { margin:0; padding:24px 16px; background:var(--bg); color:var(--fg); font:15px/1.45 system-ui, sans-serif; }
header { max-width:1200px; margin:0 auto 20px; display:flex; flex-wrap:wrap; gap:8px 20px; align-items:baseline; }
h1 { font-size:20px; margin:0; } header span, header a { color:var(--dim); }
.grid { max-width:1200px; margin:0 auto; display:grid; gap:16px; grid-template-columns:repeat(auto-fill, minmax(320px, 1fr)); }
figure { margin:0; background:var(--card); border:1px solid var(--line); border-radius:8px; overflow:hidden; }
figure img { display:block; width:100%; height:auto; }
figcaption { padding:8px 12px; display:flex; justify-content:space-between; color:var(--dim); font-size:13px; }
figcaption a { color:inherit; } .latest { outline:2px solid #3b82f6; }
p.empty { text-align:center; color:var(--dim); }
"""


def _gallery_html() -> str:
    caps = app.store.list("screenshots")
    waves = app.store.list("waveforms", 20)
    exports = app.store.list("exports", 20)
    cards = []
    for i, cap in enumerate(caps):
        ts = html.escape(cap.timestamp.replace("T", " ").replace("Z", " UTC"))
        name = html.escape(cap.name)
        cards.append(
            f'<figure class="{"latest" if i == 0 else ""}"><a href="screenshots/{name}" target="_blank">'
            f'<img loading="lazy" src="screenshots/{name}" alt="Screenshot {ts}"></a>'
            f'<figcaption><span>{ts}</span><a href="screenshots/{name}" download>download</a></figcaption></figure>'
        )
    wave_links = " · ".join(
        f'<a href="waveforms/{html.escape(w.name)}" download>CH{w.channel} {html.escape(w.timestamp[11:19])}</a>'
        for w in waves
    )
    export_links = " · ".join(
        f'<a href="exports/{html.escape(e.name)}" download>CH{e.channel} {html.escape(e.timestamp[11:19])} '
        f'({e.path.stat().st_size / 1e6:.1f} MB)</a>'
        for e in exports
    )
    keep = app.cfg.keep_screenshots or "all"
    body = "".join(cards) or '<p class="empty">No screenshots yet. Ask your agent to take one.</p>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Scope screenshots</title>
<style>{GALLERY_CSS}</style></head><body>
<header><h1>Scope screenshots</h1><span>{len(caps)} saved, keeping the last {keep}</span>
<a href="screenshots">refresh</a></header>
<div class="grid">{body}</div>
{f'<header style="margin-top:24px"><span>Recent waveforms: {wave_links}</span></header>' if waves else ''}
{f'<header><span>Memory exports: {export_links}</span></header>' if exports else ''}
</body></html>"""


def build_app():
    """The ASGI app: MCP at /mcp, gallery at /screenshots, files, and /health."""
    mcp = app.mcp

    @mcp.custom_route("/health", methods=["GET"])
    async def health(_request: Request) -> Response:
        return PlainTextResponse("ok\n")

    @mcp.custom_route("/", methods=["GET"])
    async def index(request: Request) -> Response:
        return _with_cookie(request, HTMLResponse('<meta http-equiv="refresh" content="0; url=screenshots">'))

    @mcp.custom_route("/screenshots", methods=["GET"])
    async def gallery(request: Request) -> Response:
        return _with_cookie(request, HTMLResponse(_gallery_html()))

    @mcp.custom_route("/screenshots/{name}", methods=["GET"])
    async def screenshot_file(request: Request) -> Response:
        return _file("screenshots", request.path_params["name"])

    @mcp.custom_route("/waveforms/{name}", methods=["GET"])
    async def waveform_file(request: Request) -> Response:
        return _file("waveforms", request.path_params["name"])

    @mcp.custom_route("/exports/{name}", methods=["GET"])
    async def export_file(request: Request) -> Response:
        name = request.path_params["name"]
        if name.endswith(".json"):  # metadata sidecar of an export
            try:
                cap = app.store.get("exports", name[:-5] + ".bin")
            except FileNotFoundError:
                return PlainTextResponse("not found\n", status_code=404)
            return FileResponse(cap.sidecar, headers={"Cache-Control": "no-store"})
        return _file("exports", name)

    return mcp.streamable_http_app()


def _file(kind: str, name: str) -> Response:
    try:
        cap = app.store.get(kind, name)  # only names that match the capture pattern
    except FileNotFoundError:
        return PlainTextResponse("not found\n", status_code=404)
    return FileResponse(cap.path, headers={"Cache-Control": "no-store"})


def serve(bind: str, port: int) -> None:
    import uvicorn

    token = os.environ.get("RIGOL_MCP_TOKEN", "")
    if not token and not os.environ.get("RIGOL_ALLOW_NO_AUTH"):
        raise SystemExit(
            "RIGOL_MCP_TOKEN is not set. Set a token (e.g. `openssl rand -hex 24`), or set "
            "RIGOL_ALLOW_NO_AUTH=1 to run without authentication on a trusted network."
        )
    app.mcp.settings.host = bind
    app.mcp.settings.port = port
    app.mcp.settings.stateless_http = True
    # FastMCP restricts Host headers to localhost by default. This server is meant to be
    # reached by LAN/VPN addresses and is protected by the token instead.
    app.mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    uvicorn.run(TokenAuth(build_app(), token or None), host=bind, port=port, log_level="info")
