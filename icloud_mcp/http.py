"""HTTPS transport for CalDAV and CardDAV. Redirects are handled by the caller."""

from __future__ import annotations

import ssl
import urllib.error
import urllib.request

from icloud_mcp.errors import ServiceError

MAX_RESPONSE_BYTES = 4_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def _read_limited(source, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = source.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ServiceError(
                "iCloud sent a response larger than the connector will read. Narrow the date range or the search."
            )
        chunks.append(chunk)
    return b"".join(chunks)


def _header_map(headers) -> dict[str, str]:
    mapped: dict[str, str] = {}
    if headers is None:
        return mapped
    for key, value in headers.items():
        mapped[key.lower()] = value
    return mapped


def urllib_transport(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout: float,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, _header_map(response.headers), _read_limited(response, MAX_RESPONSE_BYTES)
    except urllib.error.HTTPError as exc:
        try:
            payload = _read_limited(exc, MAX_RESPONSE_BYTES)
        except ServiceError:
            raise
        except Exception:
            payload = b""
        return exc.code, _header_map(exc.headers), payload
    except urllib.error.URLError as exc:
        reason = getattr(exc, "reason", exc)
        raise ServiceError(f"Could not reach iCloud ({reason}).") from None
    except TimeoutError as exc:
        raise ServiceError("iCloud did not answer before the timeout.") from exc
