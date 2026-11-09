"""Bearer check for the public MCP path.

The comparison hashes both sides first so a different length does not return
early, then uses hmac.compare_digest on the digests.
"""

from __future__ import annotations

import hashlib
import hmac


def bearer_matches(authorization_header: str, expected: str) -> bool:
    if not expected:
        presented = ""
    else:
        scheme, separator, rest = authorization_header.partition(" ")
        if separator != " " or scheme.lower() != "bearer" or not rest or rest != rest.strip() or " " in rest:
            presented = ""
        else:
            presented = rest
    actual = hashlib.sha256(presented.encode("utf-8")).digest()
    wanted = hashlib.sha256(expected.encode("utf-8")).digest()
    return hmac.compare_digest(actual, wanted)


def host_allowed(host_header: str, public_host: str) -> bool:
    host = host_header.strip().lower()
    if not host or host.startswith("["):
        name = host
        if host.startswith("[") and "]" in host:
            name = host[1 : host.index("]")]
        return name == "::1"
    name = host.split(":", 1)[0]
    allowed = {public_host.strip().lower(), "127.0.0.1", "localhost"}
    return name in allowed
