"""
scan.py — lightweight content scans of project manifests.

We grep the Dockerfile / docker-compose.yml text (per the product decision to scan
the manifest file only, not the build context) for two things:
  - WebSocket usage  → flips an endpoint's `websocket` flag (nginx Upgrade headers).
  - the EXPOSE'd port → becomes a dockerfile project's `container_port`.

The EXPOSE scan is stage-aware: we build with no `--target`, so the image that actually
runs is the Dockerfile's *last* stage, and only that stage's EXPOSE (or one inherited
from a stage it is FROM) describes it. Taking the first EXPOSE in the file instead would
pick up a dev/builder stage's port and publish the container on a port nothing listens on.
"""

import re

_WS_RE = re.compile(r"websocket|ws://|socket\.io", re.IGNORECASE)
_COMMENT_RE = re.compile(r"^\s*#")
_FROM_RE = re.compile(r"^FROM\s+(.+)$", re.IGNORECASE)
_EXPOSE_RE = re.compile(r"^EXPOSE\s+(.+)$", re.IGNORECASE)


def uses_websocket(text: str) -> bool:
    """True if the given manifest text looks like it serves WebSockets."""
    return bool(text and _WS_RE.search(text))


def _instructions(text: str) -> list[str]:
    """Dockerfile text → one string per logical instruction.

    Drops full-line comments (which also covers parser directives) and joins backslash
    line-continuations, so `EXPOSE 8080 \\` + `     9090` arrives as one instruction."""
    logical: list[str] = []
    pending = ""
    for raw in text.splitlines():
        if _COMMENT_RE.match(raw):
            continue            # comments are stripped even mid-continuation
        line = raw.strip()
        if pending:
            line = pending + " " + line
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1].rstrip()
            continue
        if line:
            logical.append(line)
    if pending:
        logical.append(pending)
    return logical


def _stage_ports(arg: str) -> list[int]:
    """Numeric ports declared by one EXPOSE instruction's argument list.

    Handles `EXPOSE 80/tcp` and `EXPOSE 80 443`; `${PORT}` and other build-arg forms
    can't be resolved statically and are skipped."""
    ports = []
    for token in arg.split():
        port = token.split("/", 1)[0]   # strip /tcp, /udp protocol suffix
        if port.isdigit():
            ports.append(int(port))
    return ports


def _parse_stages(text: str) -> list[dict]:
    """Dockerfile text → its build stages, in file order.

    Each stage is `{name, base, ports}`: the lowercased `AS` label (or None), the
    lowercased FROM argument, and the numeric ports it EXPOSEs."""
    stages: list[dict] = []
    for line in _instructions(text):
        m = _FROM_RE.match(line)
        if m:
            tokens = [t for t in m.group(1).split() if not t.startswith("--")]
            base = tokens[0].lower() if tokens else ""
            name = None
            if len(tokens) >= 3 and tokens[-2].lower() == "as":
                name = tokens[-1].lower()
            stages.append({"name": name, "base": base, "ports": []})
            continue
        m = _EXPOSE_RE.match(line)
        if m and stages:
            stages[-1]["ports"].extend(_stage_ports(m.group(1)))
    return stages


def exposed_port(text: str) -> int | None:
    """First numeric port EXPOSEd by the stage `docker build` actually produces, or None.

    That stage is the last one in the file (we never pass `--target`). If it EXPOSEs
    nothing itself we follow its FROM up the chain of earlier named stages, since a stage
    inherits its parent's EXPOSE. Earlier sibling stages (a `dev` or `builder` target that
    is not on that chain) are ignored.

    Handles `EXPOSE 8080`, `EXPOSE 80/tcp`, and `EXPOSE 80 443` (first wins within a
    stage). Ports given via build args/env (`EXPOSE ${PORT}`) can't be resolved statically
    and yield None, as do Dockerfiles with no EXPOSE at all — and a port inherited from a
    *base image* rather than a stage (`FROM nginx:alpine`) is likewise invisible here."""
    if not text:
        return None
    stages = _parse_stages(text)
    if not stages:
        return None

    # Only stages declared earlier can be referenced, so resolve names against a prefix of
    # the list; that ordering also makes a `FROM a AS b` / `FROM b AS a` cycle terminate.
    index = len(stages) - 1
    seen: set[int] = set()
    while index is not None and index not in seen:
        seen.add(index)
        stage = stages[index]
        if stage["ports"]:
            return stage["ports"][0]
        index = next(
            (i for i in range(index) if stages[i]["name"] == stage["base"]),
            None,
        )
    return None
