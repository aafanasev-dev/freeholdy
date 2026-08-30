#!/usr/bin/env python3
"""
fhcli  —  CLI for freeholdy API
Reads TOKEN and BASE_DOMAIN from .env in the same directory as this script.

Usage examples:
  fhcli health
  fhcli -p 8000 health                  # talk to a local dev server (http://localhost:8000)
  fhcli projects
  fhcli plugins
  fhcli plugin-add freeholdy-help mysite
  fhcli deploy myapp ./myapp            # deploy from a local file/folder over the HTTP API;
                                      # the project is created automatically if new,
                                      # auto-detects Dockerfile / docker-compose.yml,
                                      # provisions, then builds + runs it (streams the
                                      # deploy log). Re-run to redeploy.
  fhcli deploy myapp https://github.com/owner/repo.git   # deploy from a git repo instead
  fhcli deploy myapp ./myapp --no-follow
  fhcli stop  myapp
  fhcli exec  myapp "ls /app"
  fhcli ssl   myapp
  fhcli status myapp
  fhcli abort  myapp
  fhcli whoami                          # what this token is allowed to do
  fhcli tokens                          # list API tokens (admin only)
  fhcli token-create ci --role guest --project myapp   # a token for CI, scoped to myapp
"""

# ── Run under the project venv ────────────────────────────────────────────────
# The repo keeps ONE venv at <repo>/venv (built by configure.sh). Re-exec into its
# interpreter so `./fhcli.py` and the /usr/local/bin/fhcli symlink both work with no
# activation. __file__ is resolved FIRST, so a symlink resolves to the real checkout
# rather than to /usr/local/bin — that is what makes the symlink find the right venv.
# If that venv is absent (a workstation with the deps already importable) we carry on
# under the current interpreter.
#
# The already-in-the-venv test compares sys.prefix, NOT the executable path: a venv's
# bin/python is a symlink to the system interpreter, so resolving both sides would
# always compare equal and the re-exec would never fire. sys.prefix is the venv dir
# when running inside it and the system prefix otherwise, which is the real question
# — and it is what stops an exec loop.
import os
import sys
from pathlib import Path

_SELF = Path(__file__).resolve()
_VENV_DIR = _SELF.parent.parent / "venv"
_VENV_PY = _VENV_DIR / "bin" / "python"
if _VENV_PY.is_file() and Path(sys.prefix).resolve() != _VENV_DIR.resolve():
    os.execv(str(_VENV_PY), [str(_VENV_PY), str(_SELF), *sys.argv[1:]])

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import urllib.parse
import uuid
import zipfile
from pathlib import Path

import click
import requests
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.table import Table
from rich.text import Text

# ── Load .env from the script's own directory ──────────────────────────────────
_ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_FILE)

TOKEN       = os.getenv("TOKEN", "")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "").strip()
BASE_URL    = f"https://api.{BASE_DOMAIN}".rstrip("/") if BASE_DOMAIN else ""

console = Console()
# Status/summary lines that must not pollute a redirect of a command's real output
# (`fhcli logs app > app.log`) go here instead of `console`.
err_console = Console(stderr=True)

_POLL_INTERVAL = 0.75   # seconds between status polls
CHUNK_SIZE = 1024 * 1024   # 1 MiB pieces — stays under nginx's 1 MB default body limit


# ── Project-name validation ─────────────────────────────────────────────────────
# Mirrors the server's slug rule (schemas.py::validate_project_slug): a project name
# becomes a DNS subdomain (name.your-domain.com) and a Docker container name, so it
# must be a DNS-safe slug. We check it here too, to fail fast with an explanation and
# a suggested fix before the clone/upload work — instead of an opaque HTTP 422.
import re as _re

_SLUG_RE = _re.compile(r'^[a-z0-9][a-z0-9-]*[a-z0-9]$')


def _suggest_slug(name: str) -> str:
    """Best-effort conversion of an arbitrary name into a valid slug."""
    s = name.strip().lower()
    s = _re.sub(r'[^a-z0-9]+', '-', s)   # underscores, spaces, dots, … → hyphen
    s = _re.sub(r'-{2,}', '-', s)        # collapse runs of hyphens
    return s.strip('-')


def _validate_project_name(name: str) -> None:
    """Validate a new project name; on failure print an explicit reason + a suggestion
    and exit. A single lowercase-alphanumeric char is allowed (matches the server)."""
    if len(name) == 1 and _re.match(r'^[a-z0-9]$', name):
        return
    if _SLUG_RE.match(name):
        return

    # Figure out the most useful concrete reason to show the user.
    reasons = []
    if name != name.lower():
        reasons.append("contains uppercase letters")
    if "_" in name:
        reasons.append("contains underscores ('_')")
    if " " in name:
        reasons.append("contains spaces")
    bad = set(_re.findall(r'[^a-z0-9-]', name.lower())) - {" ", "_"}
    if bad:
        reasons.append("contains disallowed characters: " + " ".join(sorted(bad)))
    if name[:1] == "-" or name[-1:] == "-":
        reasons.append("has a leading or trailing hyphen")
    if not name:
        reasons.append("is empty")
    if not reasons:
        reasons.append("is not a valid DNS-safe slug")

    console.print(f"[bold red]Invalid project name:[/] '{name}'")
    console.print("  Project names become a subdomain ([dim]name.your-domain.com[/]) and a "
                  "Docker container name, so they must be DNS-safe:")
    console.print("  [dim]lowercase letters, digits and hyphens only — no leading/trailing hyphen.[/]")
    for r in reasons:
        console.print(f"    [red]•[/] {r}")
    suggestion = _suggest_slug(name)
    if suggestion and _SLUG_RE.match(suggestion):
        console.print(f"\n  Try: [bold green]{suggestion}[/]")
    sys.exit(1)


# ── git-URL detection ─────────────────────────────────────────────────────────────
# Mirrors the server's schemas.validate_git_url so `deploy` can dispatch a SOURCE argument
# to the git route (POST /git/add) vs a local upload without a round-trip.

_GIT_HTTPS_RE = _re.compile(r"^https?://[^\s]+$")
_GIT_SSH_RE   = _re.compile(r"^ssh://[^\s]+$")
_GIT_SCP_RE   = _re.compile(r"^[^\s/@]+@[^\s/:]+:[^\s]+$")   # git@github.com:owner/repo.git


def _looks_like_git_url(s: str) -> bool:
    """True if SOURCE looks like a git clone URL (http(s)://, ssh://, or git@host:path)."""
    s = s.strip()
    return bool(_GIT_HTTPS_RE.match(s) or _GIT_SSH_RE.match(s) or _GIT_SCP_RE.match(s))


# ── HTTP helpers ───────────────────────────────────────────────────────────────

def _headers() -> dict:
    if not TOKEN:
        console.print("[bold red]Error:[/] TOKEN is not set in cli/.env")
        sys.exit(1)
    return {"Authorization": f"Bearer {TOKEN}"}


def _url(path: str) -> str:
    if not BASE_URL:
        console.print("[bold red]Error:[/] BASE_DOMAIN is not set in cli/.env")
        sys.exit(1)
    return f"{BASE_URL}/{path.lstrip('/')}"


def _get(path: str) -> dict:
    try:
        r = requests.get(_url(path), headers=_headers(), timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _post(path: str, json: dict | None = None, files: dict | list | None = None) -> dict:
    try:
        kwargs: dict = {"headers": _headers(), "timeout": 30}
        if files:
            kwargs["files"] = files
        else:
            kwargs["json"] = json or {}
        r = requests.post(_url(path), **kwargs)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _post_raw(path: str, data: bytes, params: dict) -> dict:
    """POST a raw octet-stream body (one upload chunk) with query params."""
    try:
        r = requests.post(
            _url(path),
            headers={**_headers(), "Content-Type": "application/octet-stream"},
            params=params,
            data=data,
            timeout=120,
        )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _get_bytes(path: str, params: dict) -> bytes:
    """GET one raw octet-stream piece (a slice of a staged volume archive)."""
    try:
        r = requests.get(_url(path), headers=_headers(), params=params, timeout=120)
        r.raise_for_status()
        return r.content
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _put(path: str, json: dict | None = None) -> dict:
    try:
        r = requests.put(_url(path), headers=_headers(), json=json or {}, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _delete(path: str, params: dict | None = None) -> dict:
    try:
        r = requests.delete(_url(path), headers=_headers(), params=params, timeout=60)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _post_or_409(path: str, json: dict | None = None) -> dict | None:
    """Like _post, but returns None on HTTP 409 instead of exiting — used by
    plugin-add to resume an interrupted interactive install."""
    try:
        r = requests.post(_url(path), headers=_headers(), json=json or {}, timeout=30)
        if r.status_code == 409:
            return None
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection error:[/] cannot reach {BASE_URL}")
        sys.exit(1)
    except requests.exceptions.HTTPError as e:
        _print_http_error(e.response)
        sys.exit(1)


def _print_http_error(response: requests.Response):
    try:
        detail = response.json().get("detail", response.text)
    except Exception:
        detail = response.text
    console.print(f"[bold red]HTTP {response.status_code}:[/] {detail}")
    # A 403 is authorization, not a bad request — the token is valid but the route is out of
    # its reach (a guest hitting an admin-only endpoint, or a project outside its scope).
    if response.status_code == 403:
        console.print("[dim]         run [bold]fhcli whoami[/] to see what this token may do[/]")


# ── Job polling ────────────────────────────────────────────────────────────────

def _poll_status_path(status_path: str, *, show_logs: bool = True) -> dict:
    """Poll a job-status endpoint until the job finishes, streaming new log lines.
    Returns the final status dict."""
    printed_len = 0

    while True:
        data = _get(status_path)
        status = data.get("status", "no_job")

        if show_logs:
            logs = data.get("logs", "")
            new_text = logs[printed_len:]
            if new_text:
                console.print(new_text, end="", highlight=False)
                printed_len = len(logs)

        if status != "running":
            return data

        time.sleep(_POLL_INTERVAL)


def _poll_job(
    project: str,
    *,
    show_logs: bool = True,
    log_panel_title: str = "Output",
) -> dict:
    """Poll GET /projects/{project}/status until the job finishes."""
    return _poll_status_path(
        f"/projects/{project}/status",
        show_logs=show_logs,
    )


# ── Install session ────────────────────────────────────────────────────────────

def _ws_url(ws_path: str) -> str:
    # BASE_URL is module state mutated by -p/--port — resolve it lazily, like _url().
    if not BASE_URL:
        console.print("[bold red]Error:[/] BASE_DOMAIN is not set in cli/.env")
        sys.exit(1)
    return BASE_URL.replace("https://", "wss://").replace("http://", "ws://") + ws_path


async def _install_ws(ws_path: str) -> int:
    """Watch (and, for interactive plugins, drive) an install over a WebSocket.

    Protocol: we send {"type":"auth","token":…} first, then {"type":"stdin","data":…}
    for every line the user types; the server streams {"type":"stdout","data":…} (install.sh
    output and then the docker build) and finishes with {"type":"exit","code":N} reporting
    the build result. The server-side pty has echo disabled, so the terminal's own
    (canonical-mode) echo is the only one the user sees. For non-interactive plugins nothing
    is typed — the build just streams. Returns the exit code (1 on protocol/connection errors)."""
    import websockets

    ws_url = _ws_url(ws_path)

    loop = asyncio.get_running_loop()
    stdin_queue: asyncio.Queue = asyncio.Queue()

    # A daemon thread feeds stdin lines into the loop. (Not run_in_executor: a worker
    # blocked on stdin would hang asyncio.run() at executor shutdown.)
    def _feed_stdin():
        for line in sys.stdin:
            loop.call_soon_threadsafe(stdin_queue.put_nowait, line)
    threading.Thread(target=_feed_stdin, daemon=True).start()

    try:
        async with websockets.connect(ws_url) as ws:
            await ws.send(json.dumps({"type": "auth", "token": TOKEN}))

            async def _writer():
                while True:
                    line = await stdin_queue.get()
                    await ws.send(json.dumps({"type": "stdin", "data": line}))
            writer_task = asyncio.ensure_future(_writer())

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "stdout":
                        sys.stdout.write(msg.get("data", ""))
                        sys.stdout.flush()
                    elif msg.get("type") == "exit":
                        return int(msg.get("code", 1))
                    elif msg.get("type") == "error":
                        console.print(f"\n[bold red]Error:[/] {msg.get('message', 'unknown error')}")
                        return 1
                # Connection closed without an exit frame.
                console.print("\n[bold red]Connection closed unexpectedly[/]")
                return 1
            finally:
                writer_task.cancel()
    except OSError:
        console.print(f"[bold red]Connection error:[/] cannot reach {ws_url}")
        return 1


async def _exec_session(ws_path: str) -> int:
    """Bridge the local terminal to an interactive `docker exec -it` shell over a WebSocket.

    Sends {"type":"auth",…} then raw {"type":"stdin","data":…} keystrokes and
    {"type":"resize",…} on SIGWINCH; the server streams {"type":"stdout","data":…} and
    finishes with {"type":"exit","code":N}. Local stdin is put in raw mode (when it is a
    tty) so keystrokes pass straight through and the container's shell owns echo/editing."""
    import signal
    import termios
    import tty
    import websockets

    ws_url = _ws_url(ws_path)
    loop = asyncio.get_running_loop()
    fd = sys.stdin.fileno()
    is_tty = sys.stdin.isatty()
    old_attrs = termios.tcgetattr(fd) if is_tty else None
    stdin_q: asyncio.Queue = asyncio.Queue()

    # A daemon thread reads raw bytes from stdin into the loop. (Not run_in_executor: a
    # worker blocked on os.read would hang asyncio.run() at executor shutdown.)
    def _feed_stdin():
        while True:
            try:
                data = os.read(fd, 1024)
            except OSError:
                data = b""
            loop.call_soon_threadsafe(stdin_q.put_nowait, data)
            if not data:
                return

    try:
        async with websockets.connect(ws_url, max_size=None) as ws:
            await ws.send(json.dumps({"type": "auth", "token": TOKEN}))
            if is_tty:
                tty.setraw(fd)
            threading.Thread(target=_feed_stdin, daemon=True).start()

            async def _send_resize():
                try:
                    cols, rows = os.get_terminal_size()
                except OSError:
                    return
                await ws.send(json.dumps({"type": "resize", "rows": rows, "cols": cols}))

            async def _writer():
                while True:
                    data = await stdin_q.get()
                    if not data:
                        return
                    await ws.send(json.dumps({"type": "stdin", "data": data.decode(errors="replace")}))
            writer_task = asyncio.ensure_future(_writer())

            if is_tty:
                loop.add_signal_handler(
                    signal.SIGWINCH,
                    lambda: asyncio.ensure_future(_send_resize()),
                )
                await _send_resize()

            try:
                async for raw in ws:
                    msg = json.loads(raw)
                    if msg.get("type") == "stdout":
                        sys.stdout.write(msg.get("data", ""))
                        sys.stdout.flush()
                    elif msg.get("type") == "exit":
                        return int(msg.get("code", 1))
                    elif msg.get("type") == "error":
                        console.print(f"\r\n[bold red]Error:[/] {msg.get('message', 'unknown error')}")
                        return 1
                console.print("\r\n[bold red]Connection closed unexpectedly[/]")
                return 1
            finally:
                writer_task.cancel()
                if is_tty:
                    loop.remove_signal_handler(signal.SIGWINCH)
    except OSError:
        console.print(f"[bold red]Connection error:[/] cannot reach {ws_url}")
        return 1
    finally:
        if old_attrs is not None:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_attrs)


def _print_job_result(data: dict, success_msg: str = "", fail_msg: str = ""):
    """Print a coloured summary line after a job finishes."""
    status = data.get("status", "error")
    exit_code = data.get("exit_code")

    if status == "done":
        icon = "[green]✓[/]"
        msg = success_msg or f"Done (exit 0)"
    elif status == "aborted":
        icon = "[yellow]⚠[/]"
        msg = "Aborted by user"
    else:
        icon = "[red]✗[/]"
        msg = fail_msg or f"Failed (exit {exit_code})"

    console.print(f"\n{icon} {msg}")


# ── Status colouring ───────────────────────────────────────────────────────────

_STATUS_STYLE = {
    "running":   "bold green",
    "exited":    "yellow",
    "no_image":  "dim",
    "not_found": "dim",
    "error":     "bold red",
}

def _fmt_size(n) -> str:
    """Bytes as a short human string; '—' when the size is unknown."""
    if n is None:
        return "—"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _status_text(status: str) -> Text:
    style = _STATUS_STYLE.get(status, "white")
    icons = {"running": "▶ ", "exited": "■ ", "no_image": "○ ", "not_found": "○ ", "error": "✗ "}
    return Text(icons.get(status, "") + status, style=style)


# ── CLI root ───────────────────────────────────────────────────────────────────

@click.group()
@click.option("-p", "--port", type=int, default=None,
              help="Target a local API at http://localhost:PORT instead of "
                   "the BASE_DOMAIN from .env (for a dev server).")
def cli(port: int | None):
    """freeholdy CLI  —  deploy pet projects on your_domain.com"""
    if port is not None:
        global BASE_URL
        BASE_URL = f"http://localhost:{port}"


# ── health ─────────────────────────────────────────────────────────────────────

@cli.command()
def health():
    """Check API reachability."""
    data = _get("/health")
    console.print(f"[green]✓[/] API is [bold]{data.get('status', '?')}[/]  ({BASE_URL})")


@cli.command()
def version():
    """Show the API version and release stage."""
    data = _get("/version")
    console.print(f"[bold]{data.get('version', '?')}[/] [dim]({data.get('type', '?')})[/]")
    if data.get("description"):
        console.print(f"[yellow]{data['description']}[/]")


# ── projects ───────────────────────────────────────────────────────────────────

@cli.command("projects")
def list_projects():
    """List all projects with container/service status."""
    data = _get("/projects")

    if not data:
        console.print("[dim]No projects yet. Use [bold]fhcli deploy NAME PATH-OR-GIT-URL[/] to add one.[/]")
        return

    for project in data:
        ptype = project.get("type", "?")
        type_style = "magenta" if ptype == "system" else "dim"
        is_compose = project.get("deploy_mode") == "compose"
        mode_chip = "  [green]· compose[/]" if is_compose else ""
        table = Table(
            box=box.ROUNDED,
            title=f"[bold cyan]{project['name']}[/]  [{type_style}]· {ptype}[/]{mode_chip}",
            title_justify="left",
            show_header=True,
            header_style="bold",
        )
        col0 = "Service" if is_compose else "Container"
        table.add_column(col0,         style="cyan",  min_width=10)
        table.add_column("Subdomain",  style="blue",  min_width=32)
        table.add_column("Port",       justify="right", min_width=6)
        table.add_column("Container",  min_width=16)
        table.add_column("SSL",        min_width=5)
        table.add_column("Status",     min_width=14)

        # dockerfile mode: a single `container` object; compose mode: a list of `services`.
        if is_compose:
            rows = [(s["name"], s) for s in project.get("services", [])]
        else:
            c = project.get("container")
            rows = [(project["name"], c)] if c else []

        for label, info in rows:
            ssl_icon = "[green]✓[/]" if info.get("ssl_enabled") else "[dim]✗[/]"
            domain = info.get("subdomain") or "[dim]—[/]"
            if info.get("custom_domain"):
                domain += "  [magenta]· custom[/]"
            table.add_row(
                label,
                domain,
                str(info.get("local_port") or "—"),
                info.get("container_name") or "[dim]—[/]",
                ssl_icon,
                _status_text(info.get("container_status", "not_found")),
            )

        console.print(table)
        # Volumes are the project's data, so they get their own line rather than a column.
        # Sizes in the project list come from a cache filled in the background — a "pending"
        # one resolves on the next call (or immediately via `fhcli volumes NAME`).
        for vol in project.get("volumes", []):
            size = ("[dim]sizing…[/]" if vol.get("size_status") == "pending"
                    else _fmt_size(vol.get("size_bytes")))
            used_by = ", ".join(vol.get("services") or []) or "—"
            flags = "".join([
                "  [yellow]· external[/]" if vol.get("external") else "",
                "  [dim]· not created[/]" if not vol.get("exists", True) else "",
            ])
            console.print(
                f"  [magenta]▤[/] [bold]{vol['label']}[/]  [dim]{vol['name']}[/]  "
                f"{size}  [dim]· {used_by}[/]{flags}"
            )


# ── plugins ──────────────────────────────────────────────────────────────────────

@cli.command("plugins")
def list_plugins():
    """List available plugins from the catalog (includes system plugins)."""
    data = _get("/plugins")

    if not data:
        console.print("[dim]No plugins available.[/]")
        return

    table = Table(
        box=box.ROUNDED,
        title="[bold cyan]Available plugins[/]",
        title_justify="left",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Plugin",      style="cyan", min_width=12)
    table.add_column("Part",        style="blue", min_width=14)
    table.add_column("install.sh",  justify="center")
    table.add_column("Type")
    table.add_column("Description", style="dim")

    for p in data:
        install_icon = "[green]✓[/]" if p.get("has_install") else "[dim]–[/]"
        ptype = p.get("type", "plugin")
        ptype_text = f"[magenta]{ptype}[/]" if ptype == "system" else f"[green]{ptype}[/]"
        part_text = "compose" if p.get("deploy_mode") == "compose" else f":{p['container_port']}"
        table.add_row(
            p["name"],
            part_text,
            install_icon,
            ptype_text,
            p.get("description", ""),
        )

    console.print(table)


# ── plugin-add ─────────────────────────────────────────────────────────────────

@cli.command("plugin-add")
@click.argument("plugin")
@click.argument("project")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Stream provision logs live (default: on). Use --no-follow to fire-and-forget.",
)
def plugin_add(plugin: str, project: str, follow: bool):
    """Create a new project from a plugin, then build + run its container.

    Runs the plugin's install.sh (if any), builds the image, and starts the
    container — all as one background job on the server.

    \b
    Examples:
      fhcli plugin-add freeholdy-help mysite
      fhcli plugin-add freeholdy-help mysite --no-follow
    """
    _validate_project_name(project)
    console.print(f"Installing plugin [cyan]{plugin}[/] as project [bold cyan]{project}[/]…")
    with console.status("Creating project (certbot runs during creation)…"):
        data = _post_or_409(f"/plugins/{plugin}/add", json={"project_name": project})

    if data is None:
        # 409: the project already exists. For an interactive plugin that's the retry
        # path — the install session is re-runnable until it succeeds — so reconnect.
        plugins = {p["name"]: p for p in _get("/plugins")}
        info = plugins.get(plugin)
        if not (info and info.get("interactive")):
            console.print(f"[bold red]HTTP 409:[/] Project '{project}' already exists")
            sys.exit(1)
        console.print(f"[yellow]Project '{project}' already exists — resuming interactive install[/]")
        is_compose = info.get("deploy_mode") == "compose"
        data = {
            "job": {"status": "waiting_interactive"},
            "ws_path": f"/plugins/{plugin}/install/{project}",
        }
    else:
        proj = data["project"]
        console.print(f"\n[bold green]✓ {data.get('message', 'Project created')}[/]\n")

        table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
        table.add_column("Part")
        table.add_column("Subdomain", style="blue")
        table.add_column("Local port", justify="right")
        table.add_column("SSL")
        is_compose = proj.get("deploy_mode") == "compose"
        if is_compose:
            entries = proj.get("services", [])
        else:
            c = proj.get("container") or {}
            entries = [c] if c else []
        for part in entries:
            ssl_icon = "[green]✓[/]" if part.get("ssl_enabled") else "[yellow]pending[/]"
            table.add_row(
                part.get("name", "container"),
                part.get("subdomain", ""),
                str(part.get("local_port", "")),
                ssl_icon,
            )
        console.print(table)

    ws_path = data.get("ws_path") or f"/plugins/{plugin}/install/{project}"
    interactive = data.get("job", {}).get("status") == "waiting_interactive"

    # Interactive plugins must run install.sh over the WebSocket now (it needs a user on
    # stdin); the build streams over the same socket afterwards. Non-interactive plugins
    # already have their build running on the server — --no-follow leaves it in the
    # background, otherwise we connect and stream the build log live.
    if not interactive and not follow:
        hint = (f"fhcli compose-status {project}" if is_compose
                else f"fhcli status {project}")
        console.print(f"\n[green]✓[/] Provisioning started. Check progress with: [bold]{hint}[/]")
        return

    if interactive:
        console.print("\n[dim]─── interactive install (answer the prompts, Enter to send) ───[/]")
    else:
        console.print("\n[dim]─── provision output (build → run) ───[/]")

    code = asyncio.run(_install_ws(ws_path))
    if code != 0:
        console.print(
            f"\n[red]✗ install failed (exit {code})[/] — re-run "
            f"[bold]fhcli plugin-add {plugin} {project}[/] to retry, "
            f"or [bold]fhcli delete {project}[/] to start over"
        )
        sys.exit(1)
    console.print("\n[green]✓[/] Plugin installed and running")


# ── git key ────────────────────────────────────────────────────────────────────

@cli.command("get-git-key")
def get_git_key():
    """Fetch the server's GitHub SSH public key (creates it on first use).

    Add the printed key to GitHub so the server can clone your private repositories
    over SSH (git@github.com:owner/repo.git). Needed only for private repos.
    """
    data = _get("/git/key")
    pubkey = data.get("public_key", "")
    if data.get("created"):
        console.print("[green]✓[/] Generated a new GitHub SSH key on the server.\n")
    else:
        console.print("[green]✓[/] Server already has a GitHub SSH key.\n")

    console.print("[bold]Public key[/] (copy the whole line):")
    console.print(f"[cyan]{pubkey}[/]\n")

    instructions = data.get("instructions", "")
    if instructions:
        console.print(instructions)


@cli.command("redeploy")
@click.argument("project")
@click.option("--follow/--no-follow", default=True,
              help="Stream the deploy log until it completes (default: on).")
def redeploy(project: str, follow: bool):
    """Rebuild PROJECT from the git repo it was deployed from.

    Re-clones the URL and branch the server recorded at the last git deploy — you do not
    pass them again — then cuts a fresh blue/green version, exactly like `deploy`. Only
    works for projects deployed from git; a folder-deployed project has no origin to pull.

    This is the deploy a **guest** token gets: it can rebuild its own project from its own
    repo, but cannot repoint it or upload arbitrary files.

    \b
    Examples:
      fhcli redeploy myapp
      fhcli redeploy myapp --no-follow
    """
    console.print(f"Redeploying [cyan]{project}[/] from its git origin…")
    data = _post(f"/projects/{project}/redeploy")

    ws_path = data.get("ws_path")
    if not follow or not ws_path:
        console.print(f"[green]✓[/] {data.get('message', 'Redeploy launched')}")
        console.print(f"  Watch with: [bold]fhcli status {project}[/]")
        return

    code = asyncio.run(_install_ws(ws_path))
    if code == 0:
        console.print(f"\n[green]✓[/] Redeployed '{project}'")
    else:
        console.print(f"\n[red]✗[/] Redeploy failed (exit {code})")
        sys.exit(1)


# ── tokens & roles ────────────────────────────────────────────────────────────
# Tokens carry a role. `admin` is the default and can do everything; `guest` is scoped to
# one or more projects and may only redeploy (from each project's stored git origin),
# restart, read logs/status, manage env, list versions and roll back — see /tokens.

def _print_token_table(tokens: list):
    table = Table(box=box.ROUNDED, show_header=True, header_style="bold")
    table.add_column("ID",      justify="right", min_width=3)
    table.add_column("Name",    style="cyan", min_width=16)
    table.add_column("Role",    min_width=7)
    table.add_column("Projects", style="blue", min_width=18)
    table.add_column("Active",  min_width=6)
    table.add_column("Created", style="dim", min_width=19)
    for t in tokens:
        role = "[magenta]guest[/]" if t.get("role") == "guest" else "[green]admin[/]"
        table.add_row(
            str(t.get("id", "?")),
            t.get("name", "—"),
            role,
            ", ".join(t.get("projects") or []) or "[dim]—[/]",
            "[green]yes[/]" if t.get("active") else "[dim]no[/]",
            (t.get("created_at") or "")[:19],
        )
    console.print(table)


@cli.command("whoami")
def whoami():
    """Show what the configured token is allowed to do."""
    data = _get("/tokens/me")
    role = data.get("role", "?")
    console.print(f"Token [bold cyan]{data.get('name', '?')}[/] (id {data.get('id', '?')})")
    if role == "guest":
        projects = data.get("projects") or []
        console.print(f"  Role:    [magenta]guest[/] — limited to {len(projects)} project(s)")
        for name in projects:
            console.print(f"           [bold]{name}[/]")
        if not projects:
            console.print("           [dim](none — this token cannot act on anything)[/]")
        console.print("  Allowed: [dim]redeploy, restart, logs, status, abort, env, versions, rollback,[/]")
        console.print("           [dim]manual backups of those projects (backup / backups /[/]")
        console.print("           [dim]backup-download / backup-delete)[/]")
        console.print("  Denied:  [dim]backup schedules (backup-config), destinations and every[/]")
        console.print("           [dim]db-backup* command — those are the operator's[/]")
    else:
        console.print("  Role:    [green]admin[/] — full API access")


@cli.command("tokens")
def list_tokens():
    """List API tokens (admin only). Revoked tokens are shown too."""
    data = _get("/tokens")
    if not data:
        console.print("[dim]No tokens yet. Mint one with [bold]fhcli token-create NAME[/].[/]")
        return
    _print_token_table(data)


@cli.command("token-create")
@click.argument("name")
@click.option("--role", type=click.Choice(["admin", "guest"]), default="admin",
              show_default=True, help="admin = full access; guest = one project only.")
@click.option("--project", "-P", multiple=True,
              help="Project a guest token may act on (required with --role guest; repeat "
                   "for several).")
def token_create(name: str, role: str, project: tuple):
    """Mint an API token called NAME (admin only). The token is shown ONCE.

    \b
    Examples:
      fhcli token-create my-laptop
      fhcli token-create gitlab-ci --role guest --project myapp
      fhcli token-create ci --role guest -P frontend -P backend

    A guest token is meant to be handed to a third party (a CI/CD runner): it can redeploy
    its projects from their own git origins, restart them, read their logs, manage their env
    and roll them back — and nothing else, on no other project.
    """
    projects = list(project)
    if role == "guest" and not projects:
        console.print("[bold red]Error:[/] a guest token needs at least one [bold]--project NAME[/].")
        sys.exit(1)
    if role == "admin" and projects:
        console.print("[bold red]Error:[/] an admin token cannot be scoped to projects.")
        sys.exit(1)

    body = {"name": name, "role": role, "projects": projects}
    data = _post("/tokens", json=body)

    scope = (f"guest token for [bold]{', '.join(data.get('projects') or [])}[/]"
             if role == "guest" else "admin token")
    console.print(f"[green]✓[/] Created {scope} '[bold cyan]{name}[/]' (id {data.get('id')})\n")
    console.print(Panel(data.get("token", ""), title="[bold]token — shown only once[/]",
                        border_style="yellow"))
    if role == "guest":
        console.print("\n[dim]Give the third party these two lines (e.g. as CI secrets):[/]")
        console.print(f"  TOKEN={data.get('token')}")
        console.print(f"  BASE_DOMAIN={BASE_DOMAIN or '<your-domain>'}")
    console.print("\n[yellow]⚠[/]  Save it now — it is stored hashed and cannot be shown again.")


@cli.command("token-projects")
@click.argument("token_id", type=int)
@click.option("--project", "-P", multiple=True,
              help="Project the token may act on. Repeat for several; pass none to clear "
                   "the scope entirely.")
def token_projects(token_id: int, project: tuple):
    """Replace which projects guest token TOKEN_ID may act on (admin only).

    The token itself is unchanged, so a CI runner keeps the secret it already has — only
    what it reaches changes.

    \b
    Examples:
      fhcli token-projects 4 -P frontend -P backend
      fhcli token-projects 4              # scope it to nothing
    """
    data = _put(f"/tokens/{token_id}/projects", json={"projects": list(project)})
    names = data.get("projects") or []
    console.print(f"[green]✓[/] Token '[bold cyan]{data.get('name')}[/]' (id {data.get('id')}) now covers "
                  + (f"[bold]{', '.join(names)}[/]" if names else "[dim]nothing[/]"))


@cli.command("token-revoke")
@click.argument("token_id", type=int)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
def token_revoke(token_id: int, yes: bool):
    """Revoke the token with id TOKEN_ID (admin only).

    \b
    Example:
      fhcli token-revoke 3
    """
    if not yes:
        console.print(f"[bold yellow]Warning:[/] token [bold]{token_id}[/] will stop working immediately.")
        click.confirm("Are you sure?", abort=True)

    data = _delete(f"/tokens/{token_id}")
    console.print(f"[green]✓[/] Revoked token '[bold cyan]{data.get('name')}[/]' (id {data.get('id')})")


# ── compose lifecycle ─────────────────────────────────────────────────────────────

def _compose_lifecycle(project: str, action: str, follow: bool, verb: str):
    """Shared driver for compose build/up/down — POST then optionally stream logs."""
    console.print(f"Running [bold]docker compose {verb}[/] for [cyan]{project}[/]…")
    data = _post(f"/projects/{project}/compose/{action}")

    if data.get("status") == "no_job":
        console.print(f"[red]✗[/] {data.get('message', 'Unknown error')}")
        sys.exit(1)

    if not follow:
        console.print(
            f"[green]✓[/] compose {verb} started. "
            f"Check progress with: [bold]fhcli compose-status {project}[/]"
        )
        return

    console.print(f"[dim]─── compose {verb} output ───────────────────────[/]")
    result = _poll_status_path(f"/projects/{project}/compose/status", show_logs=True)
    _print_job_result(result, success_msg=f"compose {verb} succeeded", fail_msg=f"compose {verb} failed")
    if result["status"] != "done":
        sys.exit(1)


@cli.command("compose-down")
@click.argument("project")
@click.option("--follow/--no-follow", default=True, help="Stream logs live (default: on).")
def compose_down(project: str, follow: bool):
    """Stop a compose project (docker compose down)."""
    _compose_lifecycle(project, "down", follow, "down")


@cli.command("compose-status")
@click.argument("project")
def compose_status(project: str):
    """Show the last docker compose operation's status and logs."""
    data = _get(f"/projects/{project}/compose/status")
    status = data.get("status", "no_job")
    op = data.get("operation", "—")
    console.print(f"[bold]Operation:[/] {op}   [bold]Status:[/] {status}")
    logs = data.get("logs", "")
    if logs:
        console.print(Panel(logs.strip(), title="output", border_style="dim"))


# ── deploy ─────────────────────────────────────────────────────────────────────

def _print_provisioned(data: dict):
    """Render the detected deploy mode + endpoints after an upload provisions a project."""
    proj = data.get("project") or {}
    mode = data.get("deploy_mode", "?")
    name = proj.get("name", "")
    console.print(f"\n[bold]Deploy mode:[/] [green]{mode}[/]")

    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    table.add_column("Service" if mode == "compose" else "Container", style="cyan")
    table.add_column("Subdomain", style="blue")
    table.add_column("Port", justify="right")
    table.add_column("SSL")
    if mode == "compose":
        rows = [(s["name"], s) for s in proj.get("services", [])]
    else:
        c = proj.get("container") or {}
        rows = [(name, c)] if c else []
    for label, info in rows:
        ssl_icon = "[green]✓[/]" if info.get("ssl_enabled") else "[yellow]pending[/]"
        table.add_row(label, info.get("subdomain") or "—",
                      str(info.get("local_port") or "—"), ssl_icon)
    console.print(table)


def _collect_files(path: str, dest: str) -> list[tuple[str, str]]:
    """Expand a file/dir into (local_path, rel_path) pairs; rel_path includes the dest prefix."""
    if os.path.isdir(path):
        paths = [os.path.join(root, n) for root, _dirs, names in os.walk(path) for n in names]
        base = path
    else:
        paths = [path]
        base = os.path.dirname(path) or "."

    prefix = dest.strip("/")
    pairs: list[tuple[str, str]] = []
    for p in paths:
        rel = os.path.relpath(p, base).replace(os.sep, "/")
        if prefix:
            rel = f"{prefix}/{rel}"
        pairs.append((p, rel))
    return pairs


def _chunked_upload(project: str, file_pairs: list[tuple[str, str]],
                    env: str | None = None) -> dict:
    """Zip the files, send the archive in CHUNK_SIZE pieces with a progress bar, and ask
    the server to reassemble + unzip + provision. Returns the completion response dict.

    Each (local_path, rel_path) pair's rel_path becomes the zip entry name, so the server
    rebuilds the same tree (the dest prefix from `_collect_files` is already baked in).

    `env` is dotenv text stored before provisioning, so the first container start already
    has those variables."""
    upload_id = uuid.uuid4().hex
    tmp = tempfile.NamedTemporaryFile(prefix="fhcli-upload-", suffix=".zip", delete=False)
    tmp.close()
    try:
        with zipfile.ZipFile(tmp.name, "w", zipfile.ZIP_DEFLATED) as zf:
            for local, rel in file_pairs:
                zf.write(local, arcname=rel)

        total = os.path.getsize(tmp.name)
        with Progress(
            TextColumn("[cyan]{task.description}[/]"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("sending", total=total)
            with open(tmp.name, "rb") as fh:
                offset = 0
                while True:
                    chunk = fh.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    _post_raw(
                        f"/projects/{project}/upload/chunk",
                        data=chunk,
                        params={"upload_id": upload_id, "offset": offset},
                    )
                    offset += len(chunk)
                    progress.update(task, advance=len(chunk))

        with console.status("Reassembling + provisioning (certbot may run if a manifest is detected)…"):
            body = {"upload_id": upload_id, "total_size": total}
            if env:
                body["env"] = env
            return _post(f"/projects/{project}/upload/complete", json=body)
    finally:
        os.unlink(tmp.name)


def _report_upload(data: dict) -> None:
    """Print an upload response: message, file list, and the provision summary if any."""
    console.print(f"[green]✓[/] {data.get('message', 'uploaded')}")
    for rel in data.get("files", [])[:50]:
        console.print(f"  [dim]{rel}[/]")
    if data.get("count", 0) > 50:
        console.print(f"  [dim]… and {data['count'] - 50} more[/]")
    if data.get("provisioned"):
        _print_provisioned(data)


def _print_git_summary(data: dict) -> None:
    """Print the endpoints table after a git deploy provisions a project."""
    proj = data["project"]
    console.print(f"\n[bold green]✓ {data.get('message', 'Project deployed')}[/]\n")
    table = Table(box=box.SIMPLE_HEAD, show_header=True, header_style="bold")
    table.add_column("Part")
    table.add_column("Subdomain", style="blue")
    table.add_column("Local port", justify="right")
    table.add_column("SSL")
    is_compose = proj.get("deploy_mode") == "compose"
    entries = proj.get("services", []) if is_compose else ([proj.get("container")] if proj.get("container") else [])
    for part in entries:
        ssl_icon = "[green]✓[/]" if part.get("ssl_enabled") else "[yellow]pending[/]"
        table.add_row(part.get("name", "container"), part.get("subdomain", ""),
                      str(part.get("local_port", "")), ssl_icon)
    console.print(table)


def _deploy_git(name: str, git_url: str, branch: str | None, follow: bool,
                env: str | None = None) -> None:
    """Deploy from a git repo: POST /git/add (clone + provision + build/run), then stream."""
    console.print(f"Cloning [cyan]{git_url}[/] into project [bold cyan]{name}[/]…")
    body = {"name": name, "git_url": git_url, "branch": branch}
    if env:
        body["env"] = env
    with console.status("Cloning + provisioning (certbot runs during deploy)…"):
        data = _post("/git/add", json=body)
    _print_git_summary(data)

    ws_path = data.get("ws_path") or f"/git/deploy/{name}"
    is_compose = data["project"].get("deploy_mode") == "compose"
    if not follow:
        hint = f"fhcli compose-status {name}" if is_compose else f"fhcli status {name}"
        console.print(f"\n[green]✓[/] Deploy started. Check progress with: [bold]{hint}[/]")
        return

    console.print("\n[dim]─── deploy output (build → run) ───[/]")
    code = asyncio.run(_install_ws(ws_path))
    if code != 0:
        console.print(
            f"\n[red]✗ deploy failed (exit {code})[/] — fix and re-run "
            f"[bold]fhcli deploy {name} {git_url}[/], or [bold]fhcli remove {name}[/] to start over"
        )
        sys.exit(1)
    console.print("\n[green]✓[/] Project deployed and running")


def _deploy_path(project: str, path: str, dest: str, follow: bool,
                 env: str | None = None) -> None:
    """Deploy from a local file/folder: chunk-upload (provision + build/run), then stream."""
    file_pairs = _collect_files(path, dest)
    if not file_pairs:
        console.print(f"[yellow]⚠[/] No files found under [cyan]{path}[/]")
        return

    console.print(f"Uploading [bold]{len(file_pairs)}[/] file(s) → [cyan]{project}[/]…")
    data = _chunked_upload(project, file_pairs, env)
    _report_upload(data)

    ws_path = data.get("ws_path")
    if not data.get("provisioned") or not ws_path:
        return  # plain file sync — no manifest, nothing built

    if not follow:
        is_compose = data.get("deploy_mode") == "compose"
        hint = f"fhcli compose-status {project}" if is_compose else f"fhcli status {project}"
        console.print(f"\n[green]✓[/] Deploy started. Check progress with: [bold]{hint}[/]")
        return

    console.print("\n[dim]─── deploy output (build → run) ───[/]")
    code = asyncio.run(_install_ws(ws_path))
    if code != 0:
        console.print(
            f"\n[red]✗ deploy failed (exit {code})[/] — fix and re-run "
            f"[bold]fhcli deploy {project} {path}[/], or [bold]fhcli remove {project}[/] to start over"
        )
        sys.exit(1)
    console.print("\n[green]✓[/] Project deployed and running")


@cli.command("deploy")
@click.argument("name")
@click.argument("source", metavar="LOCAL_PATH_OR_GIT_URL")
@click.option("--branch", "-b", default=None,
              help="Git only: branch/ref to clone (default: the repo's default branch).")
@click.option("--dest", "-d", default="", metavar="REMOTE_DIR",
              help="Local upload only: sub-directory inside the project to upload into "
                   "(default: project root).")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Stream the build + run log live when a manifest is provisioned (default: on).",
)
@click.option("--env", "env_file", type=click.Path(), default=None, metavar="FILE",
              help="dotenv file to store before the first container start "
                   "(\"-\" reads stdin). Compose: shared by every service.")
def deploy(name: str, source: str, branch: str | None, dest: str, follow: bool,
           env_file: str | None):
    """Deploy a project from a local file/folder or a git repo. Creates it if new, redeploys
    if it already exists (a fresh blue/green version).

    SOURCE is either a local path (a file or a directory, sent recursively with the tree
    preserved) or a git clone URL (https://…, ssh://…, or git@host:path). A git URL is
    cloned server-side; a local path is zipped and streamed to the server in 1 MiB pieces
    (with a progress bar).

    Either way the server scans the project root for a manifest — a docker-compose.yml
    selects compose mode (it wins over a Dockerfile), a bare Dockerfile selects dockerfile
    mode (its EXPOSE'd port becomes the container port) — wires up nginx + SSL, then builds
    and starts the container/stack automatically, streaming the build log live. There is no
    separate create step. A local upload with no manifest is a plain file sync (nothing is
    built).

    With --env, the file's variables are stored *before* the project is provisioned, so the
    very first container this deploy starts already has them — no restart needed.

    \b
    Examples:
      fhcli deploy myapp ./myapp                              # a project folder
      fhcli deploy myapp ./Dockerfile                         # a single file
      fhcli deploy myapp ./assets --dest static               # into a sub-directory
      fhcli deploy myapp https://github.com/owner/repo.git    # from a git repo
      fhcli deploy myapp git@github.com:owner/repo.git -b dev # a private repo + branch
      fhcli deploy myapp ./myapp --env prod.env               # env set before first start
    """
    _validate_project_name(name)
    env = _read_env_file(env_file) if env_file else None

    if _looks_like_git_url(source):
        _deploy_git(name, source, branch, follow, env)
        return

    if not os.path.exists(source):
        console.print(
            f"[bold red]Error:[/] '{source}' is neither an existing local path nor a git "
            f"clone URL (https://…, ssh://…, or git@host:path)."
        )
        sys.exit(1)
    _deploy_path(name, source, dest, follow, env)


# Build + run is launched automatically by `fhcli deploy` (the server streams it back over
# the deploy WebSocket); re-run `fhcli deploy` to redeploy. The former `create` / `upload` /
# `git-add` (and `build` / `start` / `compose-build` / `compose-up`) commands are gone.


# ── stop ───────────────────────────────────────────────────────────────────────

@cli.command("stop")
@click.argument("project")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Wait for the operation to complete (default: on).",
)
def stop_container(project: str, follow: bool):
    """Stop the project's container.

    \b
    Example:
      fhcli stop myapp
    """
    console.print(f"Stopping container for [cyan]{project}[/]…")
    data = _post(f"/projects/{project}/stop")

    if data.get("status") == "no_job":
        console.print(f"[red]✗[/] {data.get('message', 'Unknown error')}")
        sys.exit(1)

    if not follow:
        console.print(
            f"[green]✓[/] Stop issued. "
            f"Check with: [bold]fhcli status {project}[/]"
        )
        return

    result = _poll_job(project, show_logs=False)
    _print_job_result(result, success_msg="Container stopped", fail_msg="Stop failed")

    if result["status"] != "done":
        sys.exit(1)


# ── versions (blue/green backups) ────────────────────────────────────────────────

_VERSION_STYLE = {"active": "bold green", "inactive": "yellow", "archived": "dim"}
_VERSION_ICON = {"active": "▶ ", "inactive": "■ ", "archived": "○ "}


def _version_status_text(status: str) -> Text:
    return Text(_VERSION_ICON.get(status, "") + status, style=_VERSION_STYLE.get(status, "white"))


@cli.command("versions")
@click.argument("project")
def list_versions(project: str):
    """List a project's blue/green versions (active / inactive / archived).

    Dockerfile projects keep the previous version as a stopped container (inactive) and
    older ones as retained images (archived). Compose projects keep active + archived
    only — each archived version retains its per-service images and a file snapshot.

    \b
    Example:
      fhcli versions myapp
    """
    data = _get(f"/projects/{project}/versions")
    counts = data.get("counts", {})
    table = Table(
        box=box.ROUNDED,
        title=f"[bold cyan]{data['project']}[/]  [dim]· backup limit {data['backup_limit']} · "
              f"{counts.get('active', 0)} active / {counts.get('inactive', 0)} inactive / "
              f"{counts.get('archived', 0)} archived[/]",
        title_justify="left",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Version",   justify="right", min_width=7)
    table.add_column("State",     min_width=12)
    table.add_column("Port",      justify="right", min_width=6)
    table.add_column("Container", min_width=16)
    table.add_column("Runtime",   min_width=12)
    table.add_column("Created",   style="dim", min_width=19)

    versions = data.get("versions", [])
    if not versions:
        console.print(f"[dim]No versions yet for '{project}'. Deploy it first with "
                      f"[bold]fhcli deploy[/].[/]")
        return
    for v in versions:
        table.add_row(
            f"v{v['version']}",
            _version_status_text(v.get("status", "?")),
            str(v.get("local_port") or "—"),
            v.get("container_name") or "[dim]—[/]",
            _status_text(v.get("container_status", "not_found")),
            (v.get("created_at") or "")[:19].replace("T", " "),
        )
    console.print(table)


@cli.command("rollback")
@click.argument("project")
@click.argument("version", type=int)
@click.option("--follow/--no-follow", default=True,
              help="Stream the rollback log until it completes (default: on).")
@click.option("--restore-data", is_flag=True, default=False,
              help="Also restore this version's volume contents and env from a backup "
                   "archive. Off by default — a plain rollback leaves data alone.")
@click.option("--backup", "backup_id", type=int, default=None, metavar="ID",
              help="Which archive to restore data from (default: the newest one of this "
                   "version). See `fhcli backups PROJECT`.")
def rollback(project: str, version: int, follow: bool, restore_data: bool,
             backup_id: int | None):
    """Roll back PROJECT to an earlier VERSION (an inactive or archived one).

    Compose projects: the current stack is downed, the version's file snapshot restored,
    and its retained images brought back up.

    Volume data is NOT rolled back unless you pass --restore-data, which puts the version's
    volumes and env back from a backup archive once the containers are up.

    \b
    Examples:
      fhcli rollback myapp 2
      fhcli rollback myapp 2 --restore-data
    """
    console.print(f"Rolling back [cyan]{project}[/] to [bold]v{version}[/]"
                  + (" [dim](restoring its data too)[/]" if restore_data else "") + "…")
    body = {"version": version, "restore_data": restore_data}
    if backup_id is not None:
        body["backup_id"] = backup_id
    data = _post(f"/projects/{project}/rollback", json=body)

    ws_path = data.get("ws_path")
    if not follow or not ws_path:
        console.print(f"[green]✓[/] {data.get('message', 'Rollback launched')}")
        console.print(f"  Watch with: [bold]fhcli status {project}[/]")
        return

    code = asyncio.run(_install_ws(ws_path))
    if code == 0:
        console.print(f"\n[green]✓[/] Rolled back '{project}' to v{version}")
    else:
        console.print(f"\n[red]✗[/] Rollback failed (exit {code})")
        sys.exit(1)


@cli.command("set-backup-limit")
@click.argument("project")
@click.argument("limit", type=int)
def set_backup_limit(project: str, limit: int):
    """Set how many archived versions to keep for PROJECT (oldest pruned immediately).

    \b
    Example:
      fhcli set-backup-limit myapp 3
    """
    if limit < 1:
        console.print("[bold red]Error:[/] limit must be >= 1")
        sys.exit(1)
    data = _put(f"/projects/{project}/backup-limit", json={"limit": limit})
    counts = data.get("counts", {})
    console.print(
        f"[green]✓[/] Backup limit for [cyan]{project}[/] set to [bold]{data['backup_limit']}[/]  "
        f"[dim]({counts.get('active', 0)} active / {counts.get('inactive', 0)} inactive / "
        f"{counts.get('archived', 0)} archived)[/]"
    )


# ── backups ────────────────────────────────────────────────────────────────────
#
# A backup is one self-contained tar: the version's image(s), the project's volumes, its env
# files and its project tree. `backup-upload` imports one as a NEW ARCHIVED VERSION, so
# activating a backup is just `fhcli rollback` — there is no separate "activate" verb.

def _backup_path(project: str, *suffix: str) -> str:
    """Project-scoped backup routes; the database's live under /backups/database."""
    return "/".join([f"/projects/{project}/backups", *suffix])


def _db_backup_path(*suffix: str) -> str:
    return "/".join(["/backups/database", *suffix])


def _chunked_backup_download(path: str, dest: str, total: int) -> None:
    """Pull an archive in CHUNK_SIZE pieces. Unlike a volume download there is no staging
    step to clean up — the archive is already a file on the server."""
    with Progress(
        TextColumn("[cyan]{task.description}[/]"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("receiving", total=total or None)
        with open(dest, "wb") as out:
            offset = 0
            while True:
                piece = _get_bytes(path, params={"offset": offset, "length": CHUNK_SIZE})
                if not piece:
                    break
                out.write(piece)
                offset += len(piece)
                progress.update(task, advance=len(piece))
                if total and offset >= total:
                    break


def _chunked_backup_upload(project: str, src: str) -> dict:
    """Send an archive in CHUNK_SIZE pieces, then ask the server to import it."""
    upload_id = uuid.uuid4().hex
    total = os.path.getsize(src)
    with Progress(
        TextColumn("[cyan]{task.description}[/]"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("sending", total=total)
        with open(src, "rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                _post_raw(_backup_path(project, "upload", "chunk"), data=chunk,
                          params={"upload_id": upload_id, "offset": offset})
                offset += len(chunk)
                progress.update(task, advance=len(chunk))
    return _post(_backup_path(project, "upload", "complete"),
                 json={"upload_id": upload_id, "total_size": total})


_BACKUP_STATUS_STYLE = {"ok": "bold green", "creating": "cyan", "error": "bold red"}
_REMOTE_STATUS_STYLE = {"ok": "green", "pending": "cyan", "error": "bold red", "none": "dim"}


def _backup_table(data: dict, title: str) -> Table:
    table = Table(box=box.ROUNDED, title=f"[bold cyan]{title}[/]  [dim]· "
                                        f"{len(data.get('backups', []))} archive(s) · "
                                        f"{_fmt_size(data.get('total_bytes'))} total[/]",
                  title_justify="left", show_header=True, header_style="bold")
    table.add_column("ID",       justify="right", min_width=4)
    table.add_column("Created",  style="dim",     min_width=19)
    table.add_column("Version",  justify="right", min_width=7)
    table.add_column("Kind",     min_width=9)
    table.add_column("Size",     justify="right", min_width=9)
    table.add_column("Contents", min_width=12)
    table.add_column("Status",   min_width=9)
    table.add_column("Remote",   min_width=10)
    for b in data.get("backups", []):
        # A database archive has only the one payload, so the I/V/E/P letters would just be
        # three dots and a misleading "project files" P.
        contents = "DB" if b.get("project") is None else "".join([
            "I" if b.get("has_images") else "·",
            "V" if b.get("has_volumes") else "·",
            "E" if b.get("has_env") else "·",
            "P" if b.get("has_project") else "·",
        ])
        kind = b.get("kind", "manual") + (" ←" if b.get("imported") else "")
        table.add_row(
            str(b["id"]),
            (b.get("created_at") or "")[:19].replace("T", " "),
            f"v{b['version']}" if b.get("version") else "—",
            kind,
            _fmt_size(b.get("size_bytes")),
            contents,
            Text(b.get("status", "?"), style=_BACKUP_STATUS_STYLE.get(b.get("status"), "white")),
            Text(b.get("remote_status", "none"),
                 style=_REMOTE_STATUS_STYLE.get(b.get("remote_status"), "dim")),
        )
    return table


def _print_backup_list(data: dict, title: str, empty_hint: str) -> None:
    if not data.get("backups"):
        console.print(f"[dim]No backups yet — {empty_hint}[/]")
        return
    console.print(_backup_table(data, title))
    if any(b.get("project") is not None for b in data.get("backups", [])):
        console.print("[dim]Contents: I=images V=volumes E=env P=project files · "
                      "'←' marks an imported archive[/]")
    for b in data.get("backups", []):
        if b.get("status") == "error" or b.get("remote_status") == "error":
            console.print(f"[red]![/] backup {b['id']}: {b.get('message', '')[:200]}")


def _print_backup_config(cfg: dict, scope: str) -> None:
    lines = [
        f"  enabled        : {'[green]yes[/]' if cfg.get('enabled') else '[dim]no[/]'}",
        f"  schedule       : {cfg.get('schedule_cron') or '[dim]none[/]'}",
        f"  on deploy      : {'yes' if cfg.get('on_deploy') else '[dim]no[/]'}",
        f"  keep locally   : {cfg.get('keep_local')}",
        f"  keep remotely  : {cfg.get('keep_remote') or '[dim]all[/]'}",
        f"  destination    : {cfg.get('target_name') or '[dim]none (local only)[/]'}",
        f"  include volumes: {'yes' if cfg.get('include_volumes') else '[dim]no[/]'}",
    ]
    if cfg.get("last_run_at"):
        lines.append(f"  last run       : {cfg['last_run_at'][:19].replace('T', ' ')}"
                     f"  [dim]{cfg.get('last_message', '')[:80]}[/]")
    console.print(f"[bold cyan]Backup settings — {scope}[/]")
    console.print("\n".join(lines))


def _follow_backup(project: str | None, data: dict) -> None:
    """Stream a backup job to completion. A project backup has its own WebSocket; the
    database's has none (there is no project to scope it to), so that one polls the list."""
    ws_path = data.get("ws_path")
    if ws_path:
        code = asyncio.run(_install_ws(ws_path))
        if code == 0:
            console.print("\n[green]✓[/] Backup complete")
        else:
            console.print(f"\n[red]✗[/] Backup failed (exit {code})")
            sys.exit(1)
        return
    path = _db_backup_path() if project is None else _backup_path(project)
    with console.status("Backing up…"):
        for _ in range(600):
            time.sleep(_POLL_INTERVAL)
            rows = _get(path).get("backups", [])
            if rows and rows[0].get("status") != "creating":
                break
    rows = _get(path).get("backups", [])
    latest = rows[0] if rows else {}
    if latest.get("status") == "ok":
        console.print(f"[green]✓[/] {latest.get('filename')} "
                      f"({_fmt_size(latest.get('size_bytes'))})")
    else:
        console.print(f"[red]✗[/] {latest.get('message', 'Backup failed')}")
        sys.exit(1)


@cli.command("backup")
@click.argument("project")
@click.option("--version", "-V", type=int, default=None, metavar="N",
              help="Which version's image(s) to capture (default: the active one).")
@click.option("--volumes/--no-volumes", "include_volumes", default=None,
              help="Include the project's volumes (default: whatever backup-config says).")
@click.option("--upload/--no-upload", default=None,
              help="Ship the archive to the configured destination (default: upload when "
                   "one is configured).")
@click.option("--follow/--no-follow", default=True, help="Stream the backup log (default: on).")
def backup(project: str, version: int | None, include_volumes: bool | None,
           upload: bool | None, follow: bool):
    """Back PROJECT up now — image(s), volumes, env and project files in one archive.

    The archive stays on the server (see `fhcli backups`) and is uploaded to the destination
    named in `fhcli backup-config` when there is one. Fetch it with `fhcli backup-download`.

    Volumes are captured as they are RIGHT NOW: docker keeps no per-version volume state, so
    backing up an older version pairs that version's image with today's data.

    \b
    Examples:
      fhcli backup myapp
      fhcli backup myapp --version 3 --no-volumes
    """
    body = {}
    if version is not None:
        body["version"] = version
    if include_volumes is not None:
        body["include_volumes"] = include_volumes
    if upload is not None:
        body["upload"] = upload
    data = _post(_backup_path(project), json=body)
    console.print(f"[green]✓[/] {data.get('message', 'Backup launched')}")
    if follow:
        _follow_backup(project, data)


@cli.command("backups")
@click.argument("project")
def list_backups(project: str):
    """List PROJECT's backup archives, newest first.

    \b
    Example:
      fhcli backups myapp
    """
    data = _get(_backup_path(project))
    _print_backup_list(data, project, f"take one with [bold]fhcli backup {project}[/]")


@cli.command("backup-download")
@click.argument("project")
@click.argument("backup_id", type=int)
@click.option("--output", "-o", "output", default=None,
              help="Where to write the archive (default: the server's own filename).")
def backup_download(project: str, backup_id: int, output: str | None):
    """Download one of PROJECT's backup archives (see `fhcli backups` for the ID).

    \b
    Example:
      fhcli backup-download myapp 4 -o myapp-backup.tar
    """
    listing = _get(_backup_path(project)).get("backups", [])
    row = next((b for b in listing if b["id"] == backup_id), None)
    if row is None:
        console.print(f"[bold red]Error:[/] no backup {backup_id} for '{project}' — "
                      f"see [bold]fhcli backups {project}[/]")
        sys.exit(1)
    dest = output or row["filename"]
    _chunked_backup_download(_backup_path(project, str(backup_id), "download"), dest,
                             row.get("size_bytes") or 0)
    console.print(f"[green]✓[/] {row['filename']} → [bold]{dest}[/] "
                  f"({_fmt_size(os.path.getsize(dest))})")


@cli.command("backup-upload")
@click.argument("project")
@click.argument("archive", type=click.Path(exists=True))
@click.option("--follow/--no-follow", default=True, help="Stream the import log (default: on).")
def backup_upload(project: str, archive: str, follow: bool):
    """Import ARCHIVE into PROJECT as a new archived version.

    The archive's images are loaded under the project's next version number and the version
    appears in `fhcli versions` as *archived*. Nothing is started — activate it exactly like
    any other version:

    \b
      fhcli rollback PROJECT N                 # code + images only
      fhcli rollback PROJECT N --restore-data  # …and the archive's volume data + env

    \b
    Example:
      fhcli backup-upload myapp myapp-backup.tar
    """
    data = _chunked_backup_upload(project, archive)
    version = data.get("version")
    console.print(f"[green]✓[/] {data.get('message', 'Import launched')}")
    if follow and data.get("ws_path"):
        code = asyncio.run(_install_ws(data["ws_path"]))
        if code != 0:
            console.print(f"\n[red]✗[/] Import failed (exit {code})")
            sys.exit(1)
        console.print(f"\n[green]✓[/] Imported as [bold]v{version}[/] (archived)")
    console.print(f"  Activate with: [bold]fhcli rollback {project} {version} --restore-data[/]")


@cli.command("backup-delete")
@click.argument("project")
@click.argument("backup_id", type=int)
@click.option("--remote", is_flag=True, default=False,
              help="Also delete the copy at the configured destination.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
def backup_delete(project: str, backup_id: int, remote: bool, yes: bool):
    """Delete one of PROJECT's backup archives.

    \b
    Example:
      fhcli backup-delete myapp 4
    """
    if not yes:
        click.confirm(f"Delete backup {backup_id} of '{project}'"
                      + (" and its remote copy" if remote else "") + "?", abort=True)
    data = _delete(_backup_path(project, str(backup_id)), params={"remote": str(remote).lower()})
    for line in data.get("details", []):
        console.print(f"  [dim]{line}[/]")
    console.print(f"[green]✓[/] Backup {backup_id} deleted")


def _config_body(enabled, disable, cron, on_deploy, target, keep, keep_remote, volumes) -> dict:
    body = {}
    if enabled or disable:
        body["enabled"] = bool(enabled)
    if cron is not None:
        body["schedule_cron"] = cron
    if on_deploy is not None:
        body["on_deploy"] = on_deploy
    if target is not None:
        body["target_name"] = target
    if keep is not None:
        body["keep_local"] = keep
    if keep_remote is not None:
        body["keep_remote"] = keep_remote
    if volumes is not None:
        body["include_volumes"] = volumes
    return body


_CONFIG_OPTIONS = [
    click.option("--enable", "enabled", is_flag=True, default=False, help="Turn automatic backups on."),
    click.option("--disable", is_flag=True, default=False, help="Turn automatic backups off."),
    click.option("--cron", default=None, metavar="EXPR",
                 help="Five-field cron schedule, e.g. '0 3 * * *' for 03:00 daily. "
                      "Pass '' to remove the timer."),
    click.option("--on-deploy/--no-on-deploy", default=None,
                 help="Back up after every successful deploy."),
    click.option("--target", default=None, metavar="NAME",
                 help="Destination from the server's .env (see `fhcli backup-targets`). "
                      "Pass '' for local-only."),
    click.option("--keep", type=int, default=None, metavar="N",
                 help="How many archives to keep on the server."),
    click.option("--keep-remote", type=int, default=None, metavar="N",
                 help="How many to keep at the destination (0 = all)."),
    click.option("--volumes/--no-volumes", "volumes", default=None,
                 help="Include volumes in automatic backups."),
]


def _with_config_options(fn):
    for option in reversed(_CONFIG_OPTIONS):
        fn = option(fn)
    return fn


@cli.command("backup-config")
@click.argument("project")
@_with_config_options
def backup_config(project: str, enabled: bool, disable: bool, cron, on_deploy, target,
                  keep, keep_remote, volumes):
    """Show or change PROJECT's automatic-backup settings.

    With no options it prints the current settings. Two independent triggers: a cron
    schedule, and a backup after every successful deploy.

    \b
    Examples:
      fhcli backup-config myapp
      fhcli backup-config myapp --enable --cron '0 3 * * *' --target offsite --keep 7
      fhcli backup-config myapp --enable --on-deploy
    """
    body = _config_body(enabled, disable, cron, on_deploy, target, keep, keep_remote, volumes)
    cfg = (_put(f"/projects/{project}/backup-config", json=body) if body
           else _get(f"/projects/{project}/backup-config"))
    _print_backup_config(cfg, project)


@cli.command("backup-targets")
def backup_targets():
    """List the backup destinations declared in the server's .env.

    Credentials never leave the server — this shows names, types and hosts only. Add a
    destination by editing the server's .env (see .env.example) and restarting freeholdy.

    \b
    Example:
      fhcli backup-targets
    """
    data = _get("/backups/targets")
    targets = data.get("targets", [])
    if not targets:
        console.print("[dim]No backup destinations declared. Add a BACKUP_TARGET_* block to "
                      "the server's .env — see .env.example.[/]")
        return
    table = Table(box=box.ROUNDED, title="[bold cyan]Backup destinations[/]",
                  title_justify="left", show_header=True, header_style="bold")
    table.add_column("Name", min_width=10)
    table.add_column("Type", min_width=6)
    table.add_column("Host", min_width=18)
    table.add_column("Path", min_width=14)
    table.add_column("Auth", min_width=10)
    for t in targets:
        auth = "ssh key" if t.get("has_ssh_key") else ("password" if t.get("has_password") else "[dim]none[/]")
        host = t.get("host", "") + (f":{t['port']}" if t.get("port") else "")
        table.add_row(t["name"], t["type"], f"{t.get('user') + '@' if t.get('user') else ''}{host}",
                      t.get("path") or "[dim]—[/]", auth)
    console.print(table)
    console.print("[dim]Check one with: [bold]fhcli backup-target-test NAME[/][/]")


@cli.command("backup-target-test")
@click.argument("name")
def backup_target_test(name: str):
    """Check that destination NAME is reachable and its credentials work.

    \b
    Example:
      fhcli backup-target-test offsite
    """
    data = _post(f"/backups/targets/{name}/test")
    if data.get("status") == "ok":
        console.print(f"[green]✓[/] {data.get('message')}")
    else:
        console.print(f"[red]✗[/] {data.get('message')}")
        sys.exit(1)


@cli.command("db-backup")
@click.option("--upload/--no-upload", default=None,
              help="Ship the archive to the configured destination.")
@click.option("--follow/--no-follow", default=True, help="Wait for it to finish (default: on).")
def db_backup(upload: bool | None, follow: bool):
    """Back up the freeholdy database itself.

    A consistent SQLite copy, gzipped into an archive under the server's data directory. The
    server's .env is deliberately NOT included — it holds the destination credentials.
    Restore one with `scripts/restore_db.sh` on the server (it stops the service first).

    \b
    Example:
      fhcli db-backup --upload
    """
    body = {} if upload is None else {"upload": upload}
    data = _post(_db_backup_path(), json=body)
    console.print(f"[green]✓[/] {data.get('message', 'Backup launched')}")
    if follow:
        _follow_backup(None, data)


@cli.command("db-backups")
def db_backups():
    """List backups of the freeholdy database.

    \b
    Example:
      fhcli db-backups
    """
    data = _get(_db_backup_path())
    _print_backup_list(data, "freeholdy database", "take one with [bold]fhcli db-backup[/]")


@cli.command("db-backup-download")
@click.argument("backup_id", type=int)
@click.option("--output", "-o", "output", default=None,
              help="Where to write the archive (default: the server's own filename).")
def db_backup_download(backup_id: int, output: str | None):
    """Download a database backup archive (see `fhcli db-backups` for the ID).

    \b
    Example:
      fhcli db-backup-download 3 -o freeholdy-db.tar
    """
    listing = _get(_db_backup_path()).get("backups", [])
    row = next((b for b in listing if b["id"] == backup_id), None)
    if row is None:
        console.print(f"[bold red]Error:[/] no database backup {backup_id} — "
                      f"see [bold]fhcli db-backups[/]")
        sys.exit(1)
    dest = output or row["filename"]
    _chunked_backup_download(_db_backup_path(str(backup_id), "download"), dest,
                             row.get("size_bytes") or 0)
    console.print(f"[green]✓[/] {row['filename']} → [bold]{dest}[/] "
                  f"({_fmt_size(os.path.getsize(dest))})")


@cli.command("db-backup-delete")
@click.argument("backup_id", type=int)
@click.option("--remote", is_flag=True, default=False, help="Also delete the remote copy.")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip the confirmation prompt.")
def db_backup_delete(backup_id: int, remote: bool, yes: bool):
    """Delete a database backup archive.

    \b
    Example:
      fhcli db-backup-delete 3
    """
    if not yes:
        click.confirm(f"Delete database backup {backup_id}?", abort=True)
    data = _delete(_db_backup_path(str(backup_id)), params={"remote": str(remote).lower()})
    for line in data.get("details", []):
        console.print(f"  [dim]{line}[/]")
    console.print(f"[green]✓[/] Database backup {backup_id} deleted")


@cli.command("db-backup-config")
@_with_config_options
def db_backup_config(enabled: bool, disable: bool, cron, on_deploy, target, keep,
                     keep_remote, volumes):
    """Show or change automatic backups of the freeholdy database.

    \b
    Examples:
      fhcli db-backup-config
      fhcli db-backup-config --enable --cron '0 4 * * *' --target offsite --keep 14
    """
    body = _config_body(enabled, disable, cron, on_deploy, target, keep, keep_remote, volumes)
    cfg = (_put(_db_backup_path("config"), json=body) if body
           else _get(_db_backup_path("config")))
    _print_backup_config(cfg, "freeholdy database")


# ── exec ───────────────────────────────────────────────────────────────────────

@cli.command("exec")
@click.argument("project")
@click.argument("command", metavar="[COMMAND]", required=False, default="")
@click.option("--service", "-s", default=None,
              help="For compose projects: the service to exec into.")
def exec_command(project: str, command: str, service: str | None):
    """Open an interactive shell (or run COMMAND) inside the running container.

    Connects a live terminal over a WebSocket — full TTY, so editors and colours work.
    With no COMMAND you get an interactive shell; pass one to run it interactively.

    \b
    Examples:
      fhcli exec myapp                       # interactive shell
      fhcli exec myapp "python manage.py shell"
      fhcli exec mystack --service api       # shell into one compose service
    """
    if service:
        ws_path = f"/projects/{project}/services/{service}/exec"
    else:
        ws_path = f"/projects/{project}/exec"
    if command:
        ws_path += "?cmd=" + urllib.parse.quote(command)

    code = asyncio.run(_exec_session(ws_path))
    if code != 0:
        sys.exit(code)


# ── logs ───────────────────────────────────────────────────────────────────────

@cli.command("logs")
@click.argument("project")
@click.option("--tail", "-n", default=200, show_default=True, metavar="N",
              help="How many trailing lines to fetch.")
@click.option("--service", "-s", default=None,
              help="For compose projects: one service instead of the whole stack.")
@click.option("--output", "-o", "output", default=None,
              help="Write to this file instead of stdout.")
def logs(project: str, tail: int, service: str | None, output: str | None):
    """Show the last N lines PROJECT's container printed.

    This is the container's own stdout/stderr — what the app logged — unlike
    `fhcli status`, which shows the log of the last build/run/stop operation. For a compose
    project you get the whole stack interleaved and service-prefixed; pass --service for one
    service's container.

    A snapshot, not a live follow: the log goes to stdout (so it pipes and redirects
    cleanly) and the summary line to stderr.

    \b
    Examples:
      fhcli logs myapp                       # last 200 lines
      fhcli logs myapp -n 50
      fhcli logs myapp | grep -i error
      fhcli logs myapp -n 1000 > myapp.log   # only log lines land in the file
      fhcli logs mystack -s db               # one compose service
    """
    path = (f"/projects/{project}/services/{service}/logs" if service
            else f"/projects/{project}/logs")
    data = _get(f"{path}?tail={tail}")
    content = data.get("content", "")
    scope = f"[cyan]{project}[/] · service [cyan]{service}[/]" if service else f"[cyan]{project}[/]"

    if output:
        Path(output).write_text(content)
        console.print(f"[green]✓[/] Wrote {data.get('lines', 0)} line(s) from {scope} to [bold]{output}[/]")
        return

    click.echo(content, nl=False)
    if not content:
        err_console.print(f"[dim](no output logged by {scope} yet)[/]")
    elif data.get("lines", 0) < tail:
        err_console.print(f"[dim]── {data.get('lines', 0)} line(s) — the whole log[/]")
    else:
        err_console.print(f"[dim]── last {data.get('lines', 0)} line(s)[/]")


# ── status ─────────────────────────────────────────────────────────────────────

@cli.command("status")
@click.argument("project")
@click.option(
    "--follow", "-f",
    is_flag=True,
    default=False,
    help="Keep polling until the job finishes (like tail -f).",
)
@click.option(
    "--logs/--no-logs",
    default=True,
    help="Print the captured logs (default: on).",
)
def get_status(project: str, follow: bool, logs: bool):
    """Show the status and logs of the last docker operation for a project.

    \b
    Examples:
      fhcli status myapp
      fhcli status myapp --follow
      fhcli status myapp --no-logs
    """
    if follow:
        console.print(
            f"Following job for [cyan]{project}[/]  "
            f"(Ctrl-C to detach)…\n"
        )
        try:
            result = _poll_job(project, show_logs=logs)
        except KeyboardInterrupt:
            console.print("\n[dim]Detached (job may still be running on the server).[/]")
            return
        _print_job_result(result)
        return

    # Single-shot snapshot.
    data = _get(f"/projects/{project}/status")
    status  = data.get("status", "no_job")
    op      = data.get("operation") or "—"
    log_txt = data.get("logs", "")
    exit_cd = data.get("exit_code")

    # Status line
    style_map = {
        "running": "bold yellow",
        "done":    "bold green",
        "error":   "bold red",
        "aborted": "yellow",
        "no_job":  "dim",
    }
    style = style_map.get(status, "white")
    console.print(
        f"[bold]Operation:[/] {op}   "
        f"[bold]Status:[/] [{style}]{status}[/]"
        + (f"   [bold]Exit code:[/] {exit_cd}" if exit_cd is not None else "")
    )

    if logs and log_txt:
        console.print(
            Panel(log_txt.strip(), title="[dim]Logs[/]", border_style="dim")
        )
    elif logs:
        console.print("[dim](no logs captured yet)[/]")


# ── abort ──────────────────────────────────────────────────────────────────────

@cli.command("abort")
@click.argument("project")
def abort_job(project: str):
    """Abort the currently running docker operation for a project.

    Sends SIGTERM to the subprocess on the server.

    \b
    Example:
      fhcli abort myapp
    """
    console.print(f"Aborting job for [cyan]{project}[/]…")
    data = _post(f"/projects/{project}/abort")
    console.print(f"[yellow]⚠[/] {data.get('message', 'Aborted')}")

    if data.get("logs"):
        console.print(
            Panel(data["logs"].strip(), title="[dim]Logs at abort[/]", border_style="dim")
        )


# ── remove ─────────────────────────────────────────────────────────────────────

# ── volumes ────────────────────────────────────────────────────────────────────
#
# A project's docker volumes are the one thing freeholdy cannot rebuild: images come from
# source and the project dir is re-uploaded, but volume data exists only once. These
# commands list them and move them in and out as tar archives, chunked like `deploy`.

def _volume_path(project: str, volume: str, *suffix: str) -> str:
    return "/".join([f"/projects/{project}/volumes/{volume}", *suffix])


def _chunked_volume_download(project: str, volume: str, dest: str) -> dict:
    """Ask the server to archive the volume, pull the staged tar in CHUNK_SIZE pieces, and
    discard the staging copy. The mirror image of `_chunked_upload`."""
    with console.status(f"Archiving volume '{volume}' on the server…"):
        prep = _post(_volume_path(project, volume, "download"))
    total, download_id = prep["size"], prep["download_id"]

    try:
        with Progress(
            TextColumn("[cyan]{task.description}[/]"),
            BarColumn(),
            DownloadColumn(),
            TransferSpeedColumn(),
            TimeRemainingColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("receiving", total=total)
            with open(dest, "wb") as out:
                offset = 0
                while offset < total:
                    piece = _get_bytes(
                        _volume_path(project, volume, "download", download_id),
                        params={"offset": offset, "length": CHUNK_SIZE},
                    )
                    if not piece:
                        break
                    out.write(piece)
                    offset += len(piece)
                    progress.update(task, advance=len(piece))
    finally:
        # Always drop the server-side copy, including after a failed transfer.
        _delete(_volume_path(project, volume, "download", download_id))
    return prep


def _chunked_volume_upload(project: str, volume: str, src: str) -> dict:
    """Send a tar in CHUNK_SIZE pieces, then ask the server to restore it into the volume."""
    upload_id = uuid.uuid4().hex
    total = os.path.getsize(src)
    with Progress(
        TextColumn("[cyan]{task.description}[/]"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("sending", total=total)
        with open(src, "rb") as fh:
            offset = 0
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                _post_raw(
                    _volume_path(project, volume, "upload", "chunk"),
                    data=chunk,
                    params={"upload_id": upload_id, "offset": offset},
                )
                offset += len(chunk)
                progress.update(task, advance=len(chunk))
    return _post(_volume_path(project, volume, "upload", "complete"),
                 json={"upload_id": upload_id, "total_size": total})


@cli.command("volumes")
@click.argument("project")
def list_volumes(project: str):
    """List PROJECT's docker volumes with their sizes.

    Sizes here are always measured (the project list shows cached ones).

    \b
    Example:
      fhcli volumes ws-chat
    """
    data = _get(f"/projects/{project}/volumes")
    volumes = data.get("volumes", [])
    if not volumes:
        console.print(f"[dim]Project [bold]{project}[/] has no docker volumes.[/]")
        return

    table = Table(box=box.ROUNDED, title=f"[bold cyan]{project}[/]  [dim]· volumes[/]",
                  title_justify="left", show_header=True, header_style="bold")
    table.add_column("Volume", style="magenta", min_width=12)
    table.add_column("Docker name", style="dim", min_width=20)
    table.add_column("Size", justify="right", min_width=9)
    table.add_column("Used by", min_width=14)
    table.add_column("Notes", min_width=10)

    for vol in volumes:
        notes = []
        if vol.get("external"):
            notes.append("[yellow]external[/]")
        if not vol.get("exists", True):
            notes.append("[dim]not created[/]")
        if vol.get("anonymous"):
            notes.append("[dim]anonymous[/]")
        table.add_row(
            vol["label"],
            vol["name"],
            _fmt_size(vol.get("size_bytes")),
            ", ".join(vol.get("services") or []) or "[dim]—[/]",
            "  ".join(notes) or "[dim]—[/]",
        )
    console.print(table)
    console.print(f"[dim]total {_fmt_size(data.get('total_bytes'))}[/]")


@cli.command("volume-download")
@click.argument("project")
@click.argument("volume")
@click.option("--output", "-o", "output", default=None,
              help="Where to write the tar (default: {project}-{volume}.tar in the cwd).")
def volume_download(project: str, volume: str, output: str | None):
    """Download PROJECT's VOLUME as a tar archive.

    The server tars the volume, the archive is fetched in pieces, and the server-side copy
    is discarded. VOLUME is the docker name as shown by `fhcli volumes`.

    \b
    Examples:
      fhcli volume-download ws-chat ws-chat_chat-data
      fhcli volume-download ws-chat ws-chat_chat-data -o backup.tar
    """
    dest = output or f"{project}-{volume}.tar"
    prep = _chunked_volume_download(project, volume, dest)
    console.print(f"[green]✓[/] {prep.get('message', 'archived')} → [bold]{dest}[/] "
                  f"({_fmt_size(os.path.getsize(dest))})")


@cli.command("volume-upload")
@click.argument("project")
@click.argument("volume")
@click.argument("archive", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Wait for the restore to complete (default: on).",
)
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
def volume_upload(project: str, volume: str, archive: str, follow: bool, yes: bool):
    """Restore ARCHIVE (a tar) into PROJECT's VOLUME.

    The project's containers are stopped, the volume's contents are REPLACED with the
    archive, and the containers are started again — so the volume ends up holding exactly
    what the tar holds, not a merge.

    \b
    Examples:
      fhcli volume-upload ws-chat ws-chat_chat-data backup.tar
      fhcli volume-upload ws-chat ws-chat_chat-data backup.tar --yes
    """
    if not yes:
        console.print(
            f"[bold yellow]Warning:[/] This replaces everything in volume "
            f"[bold magenta]{volume}[/] with the contents of [bold]{archive}[/], and "
            f"restarts [bold cyan]{project}[/]."
        )
        click.confirm("Are you sure?", abort=True)

    data = _chunked_volume_upload(project, volume, archive)
    console.print(f"[green]✓[/] {data.get('message', 'restoring')}")
    if not follow:
        console.print(f"Check with: [bold]fhcli status {project}[/]")
        return

    # The restore runs under the project's own job key, and which status endpoint that is
    # depends on the deploy mode — the server names it rather than the client guessing.
    result = _poll_status_path(data.get("status_path") or f"/projects/{project}/status")
    _print_job_result(result, success_msg="Volume restored", fail_msg="Volume restore failed")
    if result["status"] != "done":
        sys.exit(1)


@cli.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
@click.option(
    "--delete-volumes/--keep-volumes",
    default=True,
    help="Also delete the project's docker volumes (default: delete). "
         "--keep-volumes leaves the data on disk for a later project of the same name.",
)
def remove_project(name: str, yes: bool, delete_volumes: bool):
    """Remove a project: stop containers, remove images, delete nginx config and DB record.

    By default the project's docker volumes go too — that is the only part of a project
    nothing can rebuild, so `--keep-volumes` leaves them on disk.

    \b
    Example:
      fhcli remove myapp
      fhcli remove myapp --keep-volumes
      fhcli remove myapp --yes
    """
    if not yes:
        console.print(
            f"[bold yellow]Warning:[/] This will permanently delete project "
            f"[bold cyan]{name}[/] including containers, images, and nginx config."
        )
        # Name the volumes explicitly — deleting them is irreversible and on by default.
        volumes = _get(f"/projects/{name}/volumes").get("volumes", [])
        if volumes:
            listed = ", ".join(f"{v['name']} ({_fmt_size(v.get('size_bytes'))})" for v in volumes)
            if delete_volumes:
                console.print(f"[bold red]Volumes to be DELETED:[/] {listed}")
            else:
                console.print(f"[dim]Volumes kept on disk:[/] {listed}")
        click.confirm("Are you sure?", abort=True)

    console.print(f"Removing project [bold cyan]{name}[/]…")
    data = _delete(f"/projects/{name}?delete_volumes={'true' if delete_volumes else 'false'}")

    icon = "[green]✓[/]" if data["status"] == "ok" else "[yellow]⚠[/]"
    console.print(f"{icon} {data['message']}")

    for line in data.get("details", []):
        console.print(f"  [dim]{line}[/]")


# ── ssl ────────────────────────────────────────────────────────────────────────

@cli.command("ssl")
@click.argument("project")
def issue_ssl(project: str):
    """Issue (or re-issue) the Let's Encrypt SSL cert for a project.

    \b
    Example:
      fhcli ssl myapp
    """
    console.print(f"Requesting SSL cert for [cyan]{project}[/]…")
    with console.status("certbot running…"):
        data = _post(f"/projects/{project}/ssl")

    icon = "[green]✓[/]" if data["status"] == "ok" else "[red]✗[/]"
    ssl_status = "[green]enabled[/]" if data["ssl_enabled"] else "[yellow]not yet enabled[/]"
    console.print(f"{icon} SSL: {ssl_status}")
    if data.get("message"):
        console.print(Panel(data["message"].strip(), title="certbot output", border_style="dim"))


# ── domain ───────────────────────────────────────────────────────────────────────

@cli.command("domain")
@click.argument("project")
@click.argument("domain", required=False)
@click.option("--service", "-s", default=None, metavar="SERVICE",
              help="For compose projects: the service to point at the domain.")
@click.option("--clear", is_flag=True, help="Remove the custom domain (revert to the auto subdomain).")
def set_domain(project: str, domain: str | None, service: str | None, clear: bool):
    """Set or clear a custom domain for a project (or a compose SERVICE).

    Without a custom domain a component is served at its auto-generated subdomain. Point
    your domain's A record at the VPS first; if DNS hasn't propagated the component is
    served HTTP-only and you can re-run `fhcli ssl` (dockerfile) or set the domain again
    later.

    \b
    Examples:
      fhcli domain myapp app.acme.com           # dockerfile project
      fhcli domain myproj acme.com -s web        # one compose service
      fhcli domain myapp --clear                 # revert to the subdomain
    """
    if clear and domain:
        console.print("[bold red]Error:[/] pass either a DOMAIN or --clear, not both")
        sys.exit(1)
    if not clear and not domain:
        console.print("[bold red]Error:[/] provide a DOMAIN (or use --clear)")
        sys.exit(1)

    path = (f"/projects/{project}/services/{service}/domain" if service
            else f"/projects/{project}/domain")
    target = f"[cyan]{project}[/]" + (f" service [cyan]{service}[/]" if service else "")
    action = "Clearing custom domain for" if clear else f"Pointing {target} at [blue]{domain}[/] —"
    console.print(f"{action} {target if clear else ''}…")
    with console.status("nginx + certbot running…"):
        data = _post(path, json={"custom_domain": None if clear else domain})

    # Find the affected component in the returned project to report its effective domain + SSL.
    if service:
        info = next((s for s in data.get("services", []) if s["name"] == service), None)
    else:
        info = data.get("container")
    if info:
        ssl_status = "[green]enabled[/]" if info.get("ssl_enabled") else "[yellow]not yet enabled (retry once DNS points here)[/]"
        console.print(f"[bold green]✓[/] Now serving [blue]{info.get('subdomain')}[/]  ·  SSL: {ssl_status}")
    else:
        console.print("[bold green]✓ Done[/]")


# ── environment variables ──────────────────────────────────────────────────────

def _read_env_file(file: str) -> str:
    """Read dotenv text from a path, or from stdin when `file` is "-". Exits on a bad path.
    Shared by `env-set` and `deploy --env`."""
    if file == "-":
        return sys.stdin.read()
    path = Path(file)
    if not path.is_file():
        console.print(f"[bold red]Error:[/] no such file: {file}")
        sys.exit(1)
    return path.read_text()


def _env_path(project: str, service: str | None) -> str:
    return (f"/projects/{project}/services/{service}/env" if service
            else f"/projects/{project}/env")


def _env_scope(project: str, service: str | None) -> str:
    return f"[cyan]{project}[/] · service [cyan]{service}[/]" if service else f"[cyan]{project}[/]"


def _print_env_result(data: dict, project: str, service: str | None) -> None:
    keys = data.get("keys", [])
    console.print(f"[bold green]✓[/] {len(keys)} variable(s) stored for {_env_scope(project, service)}"
                  + (f": [dim]{', '.join(keys)}[/]" if keys else ""))
    if not data.get("applied", True):
        console.print(f"[yellow]![/] Not live yet — apply with: [bold]fhcli restart {project}[/]")


@cli.command("env-get")
@click.argument("project")
@click.option("--service", "-s", default=None,
              help="For compose projects: read this service's own file instead of the shared one.")
@click.option("--output", "-o", "output", default=None,
              help="Write to this file instead of stdout.")
def env_get(project: str, service: str | None, output: str | None):
    """Download PROJECT's .env file.

    Prints the file to stdout so it can be redirected, or writes it to --output.
    Without --service you get the project-level file: for a single-container project
    that is the container's environment, for a compose project the file shared by every
    service.

    \b
    Examples:
      fhcli env-get myapp                    # print to stdout
      fhcli env-get myapp > .env             # save it
      fhcli env-get myapp -o backup.env
      fhcli env-get mystack -s db            # one compose service's own file
    """
    data = _get(_env_path(project, service))
    content = data.get("content", "")
    if output:
        Path(output).write_text(content)
        n = len(data.get("keys", []))
        console.print(f"[green]✓[/] Wrote {n} variable(s) for {_env_scope(project, service)} to [bold]{output}[/]")
        if not data.get("applied", True):
            console.print(f"[yellow]![/] The stored file is newer than the running container — "
                          f"apply with: [bold]fhcli restart {project}[/]")
    else:
        click.echo(content, nl=False)


@cli.command("env-set")
@click.argument("project")
@click.argument("file", type=click.Path())
@click.option("--service", "-s", default=None,
              help="For compose projects: set this service's own file (wins over the shared one).")
def env_set(project: str, file: str, service: str | None):
    """Upload FILE as PROJECT's .env file (replaces the whole file).

    FILE of "-" reads stdin. The file is stored only — a running container keeps its
    current environment until it is next started, so follow up with `fhcli restart`
    (which recreates the container from the image it already runs, with no rebuild).

    \b
    Examples:
      fhcli env-set myapp .env
      printf 'DEBUG=1\\n' | fhcli env-set myapp -
      fhcli env-set mystack db.env -s db     # one compose service's own file
    """
    content = _read_env_file(file)
    data = _put(_env_path(project, service), json={"content": content})
    _print_env_result(data, project, service)


@cli.command("env-clear")
@click.argument("project")
@click.option("--service", "-s", default=None,
              help="For compose projects: clear this service's own file.")
def env_clear(project: str, service: str | None):
    """Delete PROJECT's .env file.

    Like env-set, this takes effect the next time the container starts.

    \b
    Examples:
      fhcli env-clear myapp
      fhcli env-clear mystack -s db
    """
    data = _delete(_env_path(project, service))
    console.print(f"[green]✓[/] {data.get('message') or 'Environment cleared'} for {_env_scope(project, service)}")


@cli.command("restart")
@click.argument("project")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Wait for the operation to complete (default: on).",
)
def restart_project(project: str, follow: bool):
    """Recreate PROJECT's container(s) so edited environment variables take effect.

    Nothing is rebuilt — the containers are recreated from the images they already run,
    which is what it takes for a new environment to be picked up (a container's env is
    fixed when it is created). For a compose project only the services whose environment
    actually changed are recreated.

    \b
    Examples:
      fhcli restart myapp
      fhcli restart mystack --no-follow
    """
    console.print(f"Restarting [cyan]{project}[/]…")
    data = _post(f"/projects/{project}/restart")

    if data.get("status") == "no_job":
        console.print(f"[red]✗[/] {data.get('message', 'Unknown error')}")
        sys.exit(1)

    if not follow:
        console.print(f"[green]✓[/] Restart issued. Check with: [bold]fhcli status {project}[/]")
        return

    # docker_service tags the job by operation: compose stacks run `compose_up`, and their
    # job lives under the compose key, so they are polled on the compose status endpoint.
    compose = str(data.get("operation") or "").startswith("compose")
    status_path = (f"/projects/{project}/compose/status" if compose
                   else f"/projects/{project}/status")
    result = _poll_status_path(status_path)
    _print_job_result(result, success_msg="Restarted", fail_msg="Restart failed")
    if result["status"] != "done":
        sys.exit(1)


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli(prog_name="fhcli")
