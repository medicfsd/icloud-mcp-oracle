"""Streamable HTTP app. It listens on localhost. Caddy publishes /mcp/icloud.

Bearer auth is checked here, in constant time, before the MCP SDK sees the
request. DNS-rebinding checks inside the SDK are off because this process is
bound to 127.0.0.1 and the Host header is checked below. A cloud MCP client
may send an Origin we cannot list in advance; the bearer token is the gate.
"""

from __future__ import annotations

import logging

import uvicorn
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

from icloud_mcp.auth import bearer_matches, host_allowed
from icloud_mcp.config import BIND, PATH, Config
from icloud_mcp.dav import DavClient
from icloud_mcp.mail import MailClient
from icloud_mcp.tools import build_server

log = logging.getLogger("icloud_mcp")


class BearerMiddleware:
    def __init__(self, app: ASGIApp, config: Config) -> None:
        self.app = app
        self.config = config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in scope.get("headers", [])}
        if not host_allowed(headers.get("host", ""), self.config.public_host):
            await Response("Misdirected request", status_code=421)(scope, receive, send)
            return
        if not bearer_matches(headers.get("authorization", ""), self.config.bearer):
            client = scope.get("client") or ("?", 0)
            log.info("rejected MCP request from %s", client[0])
            await Response(
                "Unauthorized",
                status_code=401,
                headers={"WWW-Authenticate": 'Bearer realm="icloud-mcp"'},
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


def create_app(config: Config, mail: MailClient | None = None, dav: DavClient | None = None) -> BearerMiddleware:
    mail = mail or MailClient(config)
    dav = dav or DavClient(config.user, config.credential, timeout=config.timeout)
    mcp = build_server(mail, dav)
    inner = mcp.streamable_http_app(
        streamable_http_path=PATH,
        json_response=True,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return BearerMiddleware(inner, config)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        config = Config.from_env()
    except Exception as exc:
        raise SystemExit(str(exc)) from None
    log.info("icloud mcp listening on %s:%s%s", BIND, config.port, PATH)
    uvicorn.run(
        create_app(config),
        host=BIND,
        port=config.port,
        access_log=False,
        log_level="info",
        server_header=False,
    )


if __name__ == "__main__":
    main()
