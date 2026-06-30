#!/usr/bin/env python3
"""
fhcli  —  CLI for freeholdy API
Reads TOKEN and BASE_DOMAIN from .env in the same directory as this script.

Usage examples:
  fhcli health
  fhcli -p 8000 health                  # talk to a local dev server (http://localhost:8000)
  fhcli projects
  fhcli plugins
  fhcli plugin-add hello-world mysite
  fhcli create myapp                    # then upload the project files (see below)
  fhcli upload myapp ./myapp            # uploads a file or folder over the HTTP API;
                                      # auto-detects Dockerfile / docker-compose.yml
                                      # and provisions.
  fhcli build myapp
  fhcli build myapp --no-follow
  fhcli start myapp
  fhcli stop  myapp
  fhcli exec  myapp "ls /app"
  fhcli ssl   myapp
  fhcli status myapp
  fhcli abort  myapp
"""

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


def _delete(path: str) -> dict:
    try:
        r = requests.delete(_url(path), headers=_headers(), timeout=60)
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
        console.print("[dim]No projects yet. Use [bold]fhcli create[/] to add one.[/]")
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


# ── create ─────────────────────────────────────────────────────────────────────

@cli.command("create")
@click.argument("name")
def create_project(name: str):
    """Create a new (empty) project.

    The project starts with no deploy mode. Upload your files next with
    [bold]fhcli upload NAME PATH[/]: the server scans the uploaded root for a Dockerfile
    or docker-compose.yml, picks the deploy mode automatically, and wires up nginx + SSL.

    \b
    Example:
      fhcli create myapp                    # then: fhcli upload myapp ./myapp
    """
    _validate_project_name(name)
    console.print(f"Creating project [bold cyan]{name}[/]…")
    data = _post("/projects", json={"name": name})

    console.print(f"\n[bold green]✓ Project '{data['name']}' created[/]  [dim](deploy mode: "
                  f"{data.get('deploy_mode', 'pending')})[/]\n")
    console.print(f"  Next: [bold]fhcli upload {name} ./path-to-your-project[/]")
    console.print("  [dim]The folder should contain a Dockerfile or a docker-compose.yml.[/]")


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
      fhcli plugin-add hello-world mysite
      fhcli plugin-add hello-world mysite --no-follow
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


# ── git-add ──────────────────────────────────────────────────────────────────────

@cli.command("git-add")
@click.argument("name")
@click.argument("git_url")
@click.option("--branch", "-b", default=None, help="Branch/ref to clone (default: the repo's default branch).")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Stream provision logs live (default: on). Use --no-follow to fire-and-forget.",
)
def git_add(name: str, git_url: str, branch: str | None, follow: bool):
    """Create a new project from a git repository, then build + run it.

    Clones GIT_URL into the project, auto-detects a Dockerfile or docker-compose.yml
    (compose wins), wires up nginx + SSL, then builds and starts the container/stack —
    streaming the build log live.

    \b
    Examples:
      fhcli git-add mysite https://github.com/owner/repo.git
      fhcli git-add mysite git@github.com:owner/repo.git --branch dev
      fhcli git-add mysite https://github.com/owner/repo.git --no-follow
    """
    _validate_project_name(name)
    console.print(f"Cloning [cyan]{git_url}[/] as project [bold cyan]{name}[/]…")
    with console.status("Cloning + provisioning (certbot runs during creation)…"):
        data = _post("/git/add", json={"name": name, "git_url": git_url, "branch": branch})

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

    ws_path = data.get("ws_path") or f"/git/deploy/{name}"

    if not follow:
        hint = f"fhcli compose-status {name}" if is_compose else f"fhcli status {name}"
        console.print(f"\n[green]✓[/] Provisioning started. Check progress with: [bold]{hint}[/]")
        return

    console.print("\n[dim]─── provision output (build → run) ───[/]")
    code = asyncio.run(_install_ws(ws_path))
    if code != 0:
        console.print(
            f"\n[red]✗ build failed (exit {code})[/] — re-run after fixing, "
            f"or [bold]fhcli delete {name}[/] to start over"
        )
        sys.exit(1)
    console.print("\n[green]✓[/] Project deployed and running")


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


@cli.command("compose-build")
@click.argument("project")
@click.option("--follow/--no-follow", default=True, help="Stream logs live (default: on).")
def compose_build(project: str, follow: bool):
    """Build images for a compose project (docker compose build)."""
    _compose_lifecycle(project, "build", follow, "build")


@cli.command("compose-up")
@click.argument("project")
@click.option("--follow/--no-follow", default=True, help="Stream logs live (default: on).")
def compose_up(project: str, follow: bool):
    """Start a compose project (docker compose up -d)."""
    _compose_lifecycle(project, "up", follow, "up")


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


# ── upload ─────────────────────────────────────────────────────────────────────

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

    nxt = f"fhcli compose-up {name}" if mode == "compose" else f"fhcli build {name}"
    console.print(f"\n  Next: [bold]{nxt}[/]")


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


def _chunked_upload(project: str, file_pairs: list[tuple[str, str]]) -> dict:
    """Zip the files, send the archive in CHUNK_SIZE pieces with a progress bar, and ask
    the server to reassemble + unzip + provision. Returns the completion response dict.

    Each (local_path, rel_path) pair's rel_path becomes the zip entry name, so the server
    rebuilds the same tree (the dest prefix from `_collect_files` is already baked in)."""
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
            return _post(
                f"/projects/{project}/upload/complete",
                json={"upload_id": upload_id, "total_size": total},
            )
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


@cli.command("upload")
@click.argument("project")
@click.argument("path", metavar="LOCAL_PATH", type=click.Path(exists=True))
@click.option("--dest", "-d", default="", metavar="REMOTE_DIR",
              help="Sub-directory inside the project to upload into (default: project root).")
def upload(project: str, path: str, dest: str):
    """Upload a file or a folder into a project, then auto-detect + provision.

    LOCAL_PATH may be a single file or a directory (sent recursively, tree preserved).
    The selection is zipped and streamed to the server in 1 MiB pieces (with a progress
    bar), so files and folders of any size go through; the server reassembles and unzips
    the archive, preserving the tree.

    After the files land, the server scans the project root for a manifest: a
    docker-compose.yml selects compose mode (it wins over a Dockerfile), a bare Dockerfile
    selects dockerfile mode (its EXPOSE'd port becomes the container port), and nginx + SSL
    are wired up. Uploads with no manifest are a plain file sync.

    \b
    Examples:
      fhcli upload myapp ./myapp            # a project folder (Dockerfile or compose inside)
      fhcli upload myapp ./Dockerfile       # a single file
      fhcli upload myapp ./assets --dest static
    """
    file_pairs = _collect_files(path, dest)
    if not file_pairs:
        console.print(f"[yellow]⚠[/] No files found under [cyan]{path}[/]")
        return

    console.print(f"Uploading [bold]{len(file_pairs)}[/] file(s) → [cyan]{project}[/]…")
    data = _chunked_upload(project, file_pairs)
    _report_upload(data)


# ── build ──────────────────────────────────────────────────────────────────────

@cli.command("build")
@click.argument("project")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Stream build logs live (default: on). Use --no-follow to fire-and-forget.",
)
def build_image(project: str, follow: bool):
    """Build (or rebuild) the project's Docker image.

    The build runs in a background subprocess on the server.
    By default the CLI streams logs live until the build finishes.

    \b
    Examples:
      fhcli build myapp
      fhcli build myapp --no-follow
    """
    console.print(f"Starting build for [cyan]{project}[/]…")
    data = _post(f"/projects/{project}/build")

    if data.get("status") == "no_job":
        console.print(f"[red]✗[/] {data.get('message', 'Unknown error')}")
        sys.exit(1)

    if not follow:
        console.print(
            f"[green]✓[/] Build started. "
            f"Check progress with: [bold]fhcli status {project}[/]"
        )
        return

    console.print("[dim]─── build output ───────────────────────────────[/]")
    result = _poll_job(project, show_logs=True)
    _print_job_result(result, success_msg="Image built successfully", fail_msg="Build failed")

    if result["status"] != "done":
        sys.exit(1)


# ── start ──────────────────────────────────────────────────────────────────────

@cli.command("start")
@click.argument("project")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Wait for the operation to complete (default: on).",
)
def start_container(project: str, follow: bool):
    """Start the project's container.

    \b
    Example:
      fhcli start myapp
    """
    console.print(f"Starting container for [cyan]{project}[/]…")
    data = _post(f"/projects/{project}/start")

    if data.get("status") == "no_job":
        console.print(f"[red]✗[/] {data.get('message', 'Unknown error')}")
        sys.exit(1)

    if not follow:
        console.print(
            f"[green]✓[/] Start issued. "
            f"Check with: [bold]fhcli status {project}[/]"
        )
        return

    result = _poll_job(project, show_logs=False)
    _print_job_result(result, success_msg=f"Container started", fail_msg="Start failed")

    if result["status"] != "done":
        sys.exit(1)


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

@cli.command("remove")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, default=False, help="Skip confirmation prompt.")
def remove_project(name: str, yes: bool):
    """Remove a project: stop containers, remove images, delete nginx config and DB record.

    \b
    Example:
      fhcli remove myapp
      fhcli remove myapp --yes
    """
    if not yes:
        console.print(
            f"[bold yellow]Warning:[/] This will permanently delete project "
            f"[bold cyan]{name}[/] including containers, images, and nginx config."
        )
        click.confirm("Are you sure?", abort=True)

    console.print(f"Removing project [bold cyan]{name}[/]…")
    data = _delete(f"/projects/{name}")

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


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli(prog_name="fhcli")
