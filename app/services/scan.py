"""
scan.py — lightweight content scans of project manifests.

We grep the Dockerfile / docker-compose.yml text (per the product decision to scan
the manifest file only, not the build context) for two things:
  - WebSocket usage  → flips an endpoint's `websocket` flag (nginx Upgrade headers).
  - the EXPOSE'd port → becomes a dockerfile project's `container_port`.
"""

import re

_WS_RE = re.compile(r"websocket|ws://|socket\.io", re.IGNORECASE)
_EXPOSE_RE = re.compile(r"^\s*EXPOSE\s+(.+)$", re.IGNORECASE | re.MULTILINE)


def uses_websocket(text: str) -> bool:
    """True if the given manifest text looks like it serves WebSockets."""
    return bool(text and _WS_RE.search(text))


def exposed_port(text: str) -> int | None:
    """First numeric port declared by an EXPOSE instruction, or None.

    Handles `EXPOSE 8080`, `EXPOSE 80/tcp`, and `EXPOSE 80 443` (first wins).
    Ports given via build args/env (`EXPOSE ${PORT}`) can't be resolved statically
    and yield None, as do Dockerfiles with no EXPOSE at all."""
    if not text:
        return None
    for m in _EXPOSE_RE.finditer(text):
        for token in m.group(1).split():
            port = token.split("/", 1)[0]   # strip /tcp, /udp protocol suffix
            if port.isdigit():
                return int(port)
    return None
