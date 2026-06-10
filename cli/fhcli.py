#!/usr/bin/env python3
"""
fhcli  —  CLI for freeholdy API
Reads TOKEN and BASE_DOMAIN from .env in the same directory as this script.

Usage examples:
  fhcli health
  fhcli projects
  fhcli plugins
  fhcli plugin-add hello-world mysite
  fhcli create myapp                    # then upload the project files (see below)
  fhcli upload myapp ./myapp            # uploads a file or folder; auto-detects
                                      # Dockerfile / docker-compose.yml and provisions
  fhcli build myapp
  fhcli build myapp --no-follow
  fhcli start myapp
  fhcli stop  myapp
  fhcli exec  myapp "ls /app"
  fhcli ssl   myapp
  fhcli status myapp
  fhcli abort  myapp
"""

import os
import sys
import time
from pathlib import Path

import click
import paramiko
import requests
from dotenv import load_dotenv
from rich import box
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, FileSizeColumn, Progress, TextColumn, TransferSpeedColumn
from rich.table import Table
from rich.text import Text

# ── Load .env from the script's own directory ──────────────────────────────────
_ENV_FILE = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_FILE)

TOKEN       = os.getenv("TOKEN", "")
BASE_DOMAIN = os.getenv("BASE_DOMAIN", "").strip()
BASE_URL    = f"https://api.{BASE_DOMAIN}".rstrip("/") if BASE_DOMAIN else ""

SFTP_HOST     = os.getenv("SFTP_HOST", BASE_DOMAIN)
SFTP_PORT     = int(os.getenv("SFTP_PORT", "2022"))
SFTP_USER     = os.getenv("SFTP_USER", "")
SFTP_PASSWORD = os.getenv("SFTP_PASSWORD", "")
SFTP_KEY_PATH = os.getenv("SFTP_KEY_PATH", "")

console = Console()

_POLL_INTERVAL = 0.75   # seconds between status polls


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
def cli():
    """freeholdy CLI  —  deploy pet projects on your_domain.com"""


# ── health ─────────────────────────────────────────────────────────────────────

@cli.command()
def health():
    """Check API reachability."""
    data = _get("/health")
    console.print(f"[green]✓[/] API is [bold]{data.get('status', '?')}[/]  ({BASE_URL})")


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
    console.print(f"Installing plugin [cyan]{plugin}[/] as project [bold cyan]{project}[/]…")
    with console.status("Creating project (certbot runs during creation)…"):
        data = _post(f"/plugins/{plugin}/add", json={"project_name": project})

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

    if not follow:
        hint = (f"fhcli compose-status {project}" if is_compose
                else f"fhcli status {project}")
        console.print(f"\n[green]✓[/] Provisioning started. Check progress with: [bold]{hint}[/]")
        return

    if is_compose:
        console.print("\n[dim]─── provision output (docker compose up) ───[/]")
        result = _poll_status_path(f"/projects/{project}/compose/status", show_logs=True)
    else:
        console.print("\n[dim]─── provision output (install.sh → build → run) ───[/]")
        result = _poll_status_path(f"/projects/{project}/status", show_logs=True)
    _print_job_result(result, success_msg="Plugin installed and running", fail_msg="Provisioning failed")

    if result["status"] != "done":
        sys.exit(1)


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


@cli.command("upload")
@click.argument("project")
@click.argument("path", metavar="LOCAL_PATH", type=click.Path(exists=True))
@click.option("--dest", "-d", default="", metavar="REMOTE_DIR",
              help="Sub-directory inside the project to upload into (default: project root).")
def upload(project: str, path: str, dest: str):
    """Upload a file or a folder into a project, then auto-detect + provision.

    LOCAL_PATH may be a single file or a directory (sent recursively, tree preserved).
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
    if os.path.isdir(path):
        paths = [os.path.join(root, n) for root, _dirs, names in os.walk(path) for n in names]
        base = path
    else:
        paths = [path]
        base = os.path.dirname(path) or "."
    if not paths:
        console.print(f"[yellow]⚠[/] No files found under [cyan]{path}[/]")
        return

    prefix = dest.strip("/")
    handles = []
    try:
        files = []
        for p in paths:
            rel = os.path.relpath(p, base).replace(os.sep, "/")
            if prefix:
                rel = f"{prefix}/{rel}"
            fh = open(p, "rb")
            handles.append(fh)
            files.append(("files", (rel, fh, "application/octet-stream")))

        console.print(f"Uploading [bold]{len(files)}[/] file(s) → [cyan]{project}[/]…")
        with console.status("Contacting API (certbot may run if a manifest is detected)…"):
            data = _post(f"/projects/{project}/upload", files=files)
    finally:
        for fh in handles:
            fh.close()

    console.print(f"[green]✓[/] {data.get('message', 'uploaded')}")
    for rel in data.get("files", [])[:50]:
        console.print(f"  [dim]{rel}[/]")
    if data.get("count", 0) > 50:
        console.print(f"  [dim]… and {data['count'] - 50} more[/]")

    if data.get("provisioned"):
        _print_provisioned(data)


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
@click.argument("command", metavar="COMMAND")
@click.option(
    "--follow/--no-follow",
    default=True,
    help="Stream command output live (default: on).",
)
def exec_command(project: str, command: str, follow: bool):
    """Run a command inside the running container and print output.

    \b
    Examples:
      fhcli exec myapp "ls /app"
      fhcli exec myapp "python manage.py migrate"
    """
    console.print(f"Running command in [cyan]{project}[/]…")
    data = _post(
        f"/projects/{project}/exec",
        json={"command": command},
    )

    if data.get("status") == "no_job":
        console.print(f"[red]✗[/] {data.get('message', 'Unknown error')}")
        sys.exit(1)

    if not follow:
        console.print(
            f"[green]✓[/] Command launched. "
            f"Check output with: [bold]fhcli status {project}[/]"
        )
        return

    result = _poll_job(project, show_logs=True)
    exit_code = result.get("exit_code", 0) or 0
    if exit_code != 0:
        console.print(f"[yellow]exit code {exit_code}[/]")
        sys.exit(exit_code)


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


# ── SFTP helpers ───────────────────────────────────────────────────────────────

def _sftp_connect() -> tuple[paramiko.SSHClient, paramiko.SFTPClient]:
    """Open an SSH+SFTP connection using password or key from .env."""
    if not SFTP_USER:
        console.print("[bold red]Error:[/] SFTP_USER is not set in cli/.env")
        sys.exit(1)
    if not SFTP_PASSWORD and not SFTP_KEY_PATH:
        console.print("[bold red]Error:[/] set SFTP_PASSWORD or SFTP_KEY_PATH in cli/.env")
        sys.exit(1)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    connect_kwargs: dict = dict(
        hostname=SFTP_HOST,
        port=SFTP_PORT,
        username=SFTP_USER,
        timeout=15,
    )
    if SFTP_KEY_PATH:
        connect_kwargs["key_filename"] = str(Path(SFTP_KEY_PATH).expanduser())
    else:
        connect_kwargs["password"] = SFTP_PASSWORD

    try:
        ssh.connect(**connect_kwargs)
    except paramiko.AuthenticationException:
        console.print("[bold red]SFTP auth failed.[/] Check SFTP_USER / SFTP_PASSWORD in cli/.env")
        sys.exit(1)
    except Exception as exc:
        console.print(f"[bold red]SFTP connection error:[/] {exc}")
        sys.exit(1)

    return ssh, ssh.open_sftp()


def _sftp_makedirs(sftp: paramiko.SFTPClient, remote_path: str) -> None:
    """Create remote directory tree (like mkdir -p)."""
    parts = Path(remote_path).parts
    current = ""
    for part in parts:
        current = str(Path(current) / part)
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)


# ── sftp-upload ──────────────────────────────────────────────────────────────────

@cli.command("sftp-upload")
@click.argument("project")
@click.argument("files", metavar="FILE...", nargs=-1, required=True,
                type=click.Path(exists=True, dir_okay=False))
@click.option("--dest", "-d", default="", metavar="REMOTE_DIR",
              help="Sub-directory inside the project folder (default: project root).")
def sftp_upload_files(project: str, files: tuple, dest: str):
    """Upload files to a project's folder over SFTP (raw transfer, no provisioning).

    For getting files into a project and auto-provisioning, prefer [bold]fhcli upload[/];
    this SFTP path is for large archives / direct transfers to /srv/projects/PROJECT/.

    \b
    Examples:
      fhcli sftp-upload myapp ./app.tar.gz
      fhcli sftp-upload myapp ./dist.zip ./assets.tar.gz
      fhcli sftp-upload myapp ./data/seed.sql --dest db
    """
    ssh, sftp = _sftp_connect()

    remote_base = f"/{project}"
    if dest:
        remote_base = f"{remote_base}/{dest.strip('/')}"

    try:
        _sftp_makedirs(sftp, remote_base)
    except Exception as exc:
        console.print(f"[bold red]Cannot create remote directory {remote_base}:[/] {exc}")
        ssh.close()
        sys.exit(1)

    with Progress(
        TextColumn("[cyan]{task.fields[filename]}[/]"),
        BarColumn(),
        FileSizeColumn(),
        TransferSpeedColumn(),
        console=console,
    ) as progress:
        for local_path in files:
            filename  = os.path.basename(local_path)
            file_size = os.path.getsize(local_path)
            remote    = f"{remote_base}/{filename}"
            task      = progress.add_task("", filename=filename, total=file_size)

            def _callback(sent: int, _total: int, t=task) -> None:
                progress.update(t, completed=sent)

            try:
                sftp.put(local_path, remote, callback=_callback)
                progress.update(task, completed=file_size)
            except Exception as exc:
                console.print(f"\n[bold red]✗ Failed to upload {filename}:[/] {exc}")
                ssh.close()
                sys.exit(1)

    sftp.close()
    ssh.close()

    remote_display = remote_base.lstrip("/")
    console.print(
        f"[green]✓[/] {len(files)} file(s) uploaded to "
        f"[dim]{SFTP_HOST}:{SFTP_PORT}[/] → [cyan]{remote_display}[/]"
    )


# ── entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    cli(prog_name="fhcli")
