"""
docker_service.py
All Docker interactions go through subprocess so that:
  - Long-running commands (build, start, stop, exec) are non-blocking.
  - Output is streamed to a temporary log file readable at any time.
  - A single "last job" is tracked per part (keyed by container_name).
  - Any running job can be aborted via abort_job().
"""

import os
import shlex
import subprocess
import tempfile
import threading
from dataclasses import dataclass
from typing import Optional

# How long a synchronous log tail may take before we give up on docker.
LOGS_TIMEOUT = 30

# ---------------------------------------------------------------------------
# Job tracking
# ---------------------------------------------------------------------------

@dataclass
class DockerJob:
    operation: str                    # build | start | stop | exec
    command: list
    process: subprocess.Popen
    log_path: str                     # path to the temp log file
    status: str = "running"           # running | done | error | aborted
    exit_code: Optional[int] = None

# Maps container_name → last DockerJob for that part.
_jobs: dict = {}
_lock = threading.Lock()


def _monitor_job(key: str, log_fd) -> None:
    """Background thread: wait for process to finish, close log fd, update status."""
    job = _jobs.get(key)
    if job is None:
        log_fd.close()
        return
    exit_code = job.process.wait()
    log_fd.flush()
    log_fd.close()
    with _lock:
        job.exit_code = exit_code
        if job.status == "running":       # don't overwrite "aborted"
            job.status = "done" if exit_code == 0 else "error"


def _spawn(key: str, operation: str, cmd: list) -> DockerJob:
    """
    Spawn cmd as a subprocess, redirect stdout+stderr to a fresh temp file,
    register it as the current job for key, start a monitor thread.
    """
    log_fd = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".log",
        prefix=f"freeholdy_{operation}_",
        delete=False,
    )
    log_path = log_fd.name

    proc = subprocess.Popen(
        cmd,
        stdout=log_fd,
        stderr=subprocess.STDOUT,
        text=True,
    )

    job = DockerJob(
        operation=operation,
        command=cmd,
        process=proc,
        log_path=log_path,
        status="running",
    )

    with _lock:
        # Clean up temp file from the previous finished job, if any.
        old = _jobs.get(key)
        if old and old.status != "running":
            try:
                os.unlink(old.log_path)
            except OSError:
                pass
        _jobs[key] = job

    threading.Thread(target=_monitor_job, args=(key, log_fd), daemon=True).start()
    return job


def spawn_job(key: str, operation: str, cmd: list) -> str:
    """Run `cmd` as a tracked background job under `key` and return the key.

    The public seam onto `_spawn` for callers that build their own argv — deploy_service
    does, for the compose invocations that need the image-pin override file."""
    _spawn(key, operation, cmd)
    return key


def register_external_job(
    key: str,
    operation: str,
    command: list,
    process: Optional[subprocess.Popen],
    log_path: str,
) -> DockerJob:
    """Register a process whose lifecycle is managed by the caller (e.g. an interactive
    install session, see interactive_service) so GET /status, log reads, and abort_job
    work on it like any spawned job. The caller writes the log file and must report the
    outcome via finish_external_job — no monitor thread is started here. Note that a
    later _spawn under the same key replaces this job and unlinks its log file.

    `process` may be None for a multi-phase job (compose blue/green deploys) that hasn't
    spawned its first subprocess yet — the runner points the job at each phase's process
    via update_job_process so abort_job always terminates the right one."""
    job = DockerJob(
        operation=operation,
        command=command,
        process=process,
        log_path=log_path,
        status="running",
    )
    with _lock:
        old = _jobs.get(key)
        if old and old.status != "running":
            try:
                os.unlink(old.log_path)
            except OSError:
                pass
        _jobs[key] = job
    return job


def update_job_process(key: str, process: subprocess.Popen) -> None:
    """Point an external job at its currently running subprocess phase, so abort_job
    terminates the right process while a multi-phase job (compose deploy) advances."""
    with _lock:
        job = _jobs.get(key)
        if job is not None:
            job.process = process


def finish_external_job(key: str, exit_code: Optional[int], aborted: bool = False) -> None:
    """Record the outcome of a job registered via register_external_job."""
    with _lock:
        job = _jobs.get(key)
        if job is None:
            return
        job.exit_code = exit_code
        if job.status == "running":       # don't overwrite "aborted" set by abort_job
            if aborted:
                job.status = "aborted"
            else:
                job.status = "done" if exit_code == 0 else "error"


# ---------------------------------------------------------------------------
# Public job API
# ---------------------------------------------------------------------------

def get_job(key: str) -> Optional[DockerJob]:
    """Return the last DockerJob for a part, or None."""
    return _jobs.get(key)


def get_job_logs(key: str) -> str:
    """Read all logs written so far for the last job of key."""
    job = _jobs.get(key)
    if job is None:
        return ""
    try:
        with open(job.log_path, "r") as f:
            return f.read()
    except OSError:
        return ""


def abort_job(key: str) -> tuple:
    """Send SIGTERM to the running process for key, mark it aborted."""
    job = _jobs.get(key)
    if job is None:
        return False, "No job found for this part"
    if job.status != "running":
        return False, f"Job is not running (current status: {job.status})"
    if job.process is not None:      # a multi-phase job may be between subprocesses
        job.process.terminate()
    with _lock:
        job.status = "aborted"
    return True, f"Job '{job.operation}' aborted"


# ---------------------------------------------------------------------------
# Status helpers  (fast, synchronous)
# ---------------------------------------------------------------------------

def get_container_status(container_name: str) -> str:
    """Return container status: running | exited | not_found | error"""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", container_name],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "no such container" in stderr or "no such object" in stderr:
            return "not_found"
        return "error"
    return result.stdout.strip() or "not_found"


def get_container_logs(container_name: str, tail: int) -> tuple:
    """Return the last `tail` lines a container printed: (success, output).

    A read-only snapshot (`docker logs --tail N`), not a follow — it spawns no DockerJob,
    so tailing can never collide with a build/deploy tracked under the same job key.
    Docker sends the container's stdout to our stdout and its stderr to our stderr; both
    are the container's output, so they are concatenated the way a terminal shows them.
    On a missing container the message is "not_found" (same vocabulary as
    get_container_status), which the router turns into a 404."""
    try:
        result = subprocess.run(
            ["docker", "logs", "--tail", str(tail), container_name],
            capture_output=True,
            text=True,
            timeout=LOGS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"docker logs timed out after {LOGS_TIMEOUT}s"
    if result.returncode != 0:
        stderr = result.stderr.lower()
        if "no such container" in stderr or "no such object" in stderr:
            return False, "not_found"
        return False, result.stderr.strip()
    return True, result.stdout + result.stderr


def image_exists(image_name: str) -> bool:
    result = subprocess.run(
        ["docker", "image", "inspect", image_name],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Dockerfile validation  (synchronous)
# ---------------------------------------------------------------------------

def validate_dockerfile(dockerfile_path: str) -> tuple:
    """Validate Dockerfile syntax. Tries docker build --check, falls back to basic parse."""
    if not os.path.exists(dockerfile_path):
        return False, "File not found"

    result = subprocess.run(
        [
            "docker", "build", "--check",
            "-f", dockerfile_path,
            os.path.dirname(dockerfile_path) or ".",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, "Dockerfile syntax is valid (docker --check passed)"
    if "unknown flag" in result.stderr or "unknown option" in result.stderr:
        return _basic_dockerfile_parse(dockerfile_path)
    return False, result.stderr.strip()


def _basic_dockerfile_parse(dockerfile_path: str) -> tuple:
    VALID_INSTRUCTIONS = {
        "FROM", "RUN", "CMD", "LABEL", "EXPOSE", "ENV", "ADD", "COPY",
        "ENTRYPOINT", "VOLUME", "USER", "WORKDIR", "ARG", "ONBUILD",
        "STOPSIGNAL", "HEALTHCHECK", "SHELL",
    }
    try:
        with open(dockerfile_path) as f:
            raw_lines = f.readlines()

        lines, buf = [], ""
        for line in raw_lines:
            stripped = line.rstrip("\n")
            if stripped.endswith("\\"):
                buf += stripped[:-1] + " "
            else:
                buf += stripped
                logical = buf.strip()
                buf = ""
                if logical and not logical.startswith("#"):
                    lines.append(logical)

        if not lines:
            return False, "Dockerfile is empty"
        first_word = lines[0].split()[0].upper()
        if first_word not in ("FROM", "ARG"):
            return False, f"Dockerfile must start with FROM (or ARG). Got: {first_word}"
        if not any(l.split()[0].upper() == "FROM" for l in lines):
            return False, "Dockerfile must contain a FROM instruction"
        for i, line in enumerate(lines, 1):
            word = line.split()[0].upper()
            if word not in VALID_INSTRUCTIONS:
                return False, f"Unknown instruction at logical line {i}: {word}"
        return True, "Dockerfile syntax is valid"
    except Exception as e:
        return False, f"Error reading Dockerfile: {e}"


# ---------------------------------------------------------------------------
# Docker operations — all non-blocking
# ---------------------------------------------------------------------------

def build_image(dockerfile_path: str, image_name: str, job_key: str) -> DockerJob:
    """Start 'docker build' in the background. Poll get_job(job_key) for status."""
    build_dir = os.path.dirname(os.path.abspath(dockerfile_path))
    dockerfile_name = os.path.basename(dockerfile_path)
    cmd = [
        "docker", "build",
        "--progress=plain",
        "-t", image_name,
        "-f", os.path.join(build_dir, dockerfile_name),
        build_dir,
    ]
    return _spawn(job_key, "build", cmd)


def start_container(
    container_name: str,
    image_name: str,
    local_port: int,
    container_port: int,
    job_key: str,
    env_file: Optional[str] = None,
) -> DockerJob:
    """Remove any stopped container then 'docker run' in the background.

    `env_file` is the project's materialized env file (`env_service.materialize`)."""
    # Synchronous remove — fast, must complete before docker run.
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
    )
    cmd = [
        "docker", "run",
        "--detach",
        "--name", container_name,
        "--restart", "unless-stopped",
        *(["--env-file", env_file] if env_file and os.path.exists(env_file) else []),
        "-p", f"127.0.0.1:{local_port}:{container_port}",
        image_name,
    ]
    return _spawn(job_key, "start", cmd)


def stop_container(container_name: str, job_key: str) -> DockerJob:
    """Stop a container in the background."""
    cmd = ["docker", "stop", "--time", "10", container_name]
    return _spawn(job_key, "stop", cmd)


def provision_from_plugin(
    job_key: str,
    project_dir: str,
    plugin_dir: str,
    install_script: Optional[str],
    image_name: str,
    container_name: str,
    local_port: int,
    container_port: int,
    env_file: Optional[str] = None,
) -> DockerJob:
    """Background job that provisions a container from a plugin in one shot:
    optional install.sh → docker build → docker run. All steps stream to one log,
    tracked under job_key (the container name) like every other docker operation.

    install_script, when present, runs with cwd=project_dir and PLUGIN_DIR/PROJECT_DIR
    in the environment so it can copy bundled assets or fetch from git into the build context.
    """
    q = shlex.quote
    dockerfile = os.path.join(project_dir, "Dockerfile")
    lines = ["set -e"]

    if install_script and os.path.exists(install_script):
        lines += [
            'echo "── running install.sh ──"',
            f"cd {q(project_dir)}",
            f"PLUGIN_DIR={q(plugin_dir)} PROJECT_DIR={q(project_dir)} bash {q(install_script)}",
        ]

    lines += [
        'echo "── docker build ──"',
        f"docker build --progress=plain -t {q(image_name)} -f {q(dockerfile)} {q(project_dir)}",
        'echo "── docker run ──"',
        f"docker rm -f {q(container_name)} >/dev/null 2>&1 || true",
        f"docker run --detach --name {q(container_name)} --restart unless-stopped "
        f"{f'--env-file {q(env_file)} ' if env_file and os.path.exists(env_file) else ''}"
        f"-p 127.0.0.1:{int(local_port)}:{int(container_port)} {q(image_name)}",
    ]

    cmd = ["bash", "-c", "\n".join(lines)]
    return _spawn(job_key, "provision", cmd)


def compose_env_flags(project_dir: str, env_file: Optional[str]) -> list:
    """`--env-file` flags for a `docker compose` invocation, or [] when there is no env.

    These control **interpolation** (`${VAR}` in the compose file), not what lands inside
    the containers — injection is the `env_file:` entries the generated override carries.

    Passing `--env-file` at all disables compose's automatic pickup of `{project_dir}/.env`,
    and that file is load-bearing: every compose plugin's install.sh writes its generated
    secrets there for `${VAR}` substitution. So when a project-level env file exists, the
    project's own `.env` is passed first and the freeholdy file second (later wins), and
    when there is none the argv stays exactly as it was before this feature. Repeated
    `--env-file` needs Docker Compose >= 2.24.
    """
    if not env_file or not os.path.exists(env_file):
        return []
    flags = []
    dotenv = os.path.join(project_dir, ".env")
    if os.path.exists(dotenv):
        flags += ["--env-file", dotenv]
    return flags + ["--env-file", env_file]


def _compose_cmd(project: str, project_dir: str, *args: str,
                 env_file: Optional[str] = None) -> list:
    """Build a `docker compose` invocation pinned to a project's two compose files."""
    return [
        "docker", "compose",
        "-p", project,
        *compose_env_flags(project_dir, env_file),
        "-f", os.path.join(project_dir, "docker-compose.yml"),
        "-f", os.path.join(project_dir, "docker-compose.override.yml"),
        *args,
    ]


def compose_build(project: str, project_dir: str, job_key: str,
                  env_file: Optional[str] = None) -> DockerJob:
    """Start 'docker compose build' in the background. Poll get_job(job_key)."""
    cmd = _compose_cmd(project, project_dir, "build", "--progress=plain", env_file=env_file)
    return _spawn(job_key, "compose_build", cmd)


def compose_up(project: str, project_dir: str, job_key: str,
               env_file: Optional[str] = None) -> DockerJob:
    """Start 'docker compose up -d' in the background (builds images as needed)."""
    cmd = _compose_cmd(project, project_dir, "up", "-d", env_file=env_file)
    return _spawn(job_key, "compose_up", cmd)


def compose_down(project: str, project_dir: str, job_key: str,
                 env_file: Optional[str] = None) -> DockerJob:
    """Start 'docker compose down' in the background."""
    cmd = _compose_cmd(project, project_dir, "down", env_file=env_file)
    return _spawn(job_key, "compose_down", cmd)


def compose_logs(project: str, project_dir: str, tail: int,
                 env_file: Optional[str] = None) -> tuple:
    """Return the last `tail` lines of every service in the stack, interleaved and
    service-prefixed: (success, output). Synchronous read-only snapshot, like
    get_container_logs — no DockerJob is spawned."""
    cmd = _compose_cmd(project, project_dir, "logs", "--tail", str(tail), "--no-color",
                       env_file=env_file)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=LOGS_TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, f"docker compose logs timed out after {LOGS_TIMEOUT}s"
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, result.stdout + result.stderr


def stop_container_sync(container_name: str) -> tuple:
    """Synchronously stop a running container (blue/green demotion keeps the stopped
    container on disk for fast rollback). Returns (success, message)."""
    result = subprocess.run(
        ["docker", "stop", "--time", "10", container_name],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, f"Container '{container_name}' stopped"
    stderr = result.stderr.strip()
    if "no such container" in stderr.lower():
        return True, f"Container '{container_name}' did not exist — skipped"
    return False, f"Failed to stop container '{container_name}': {stderr}"


def remove_container(container_name: str) -> tuple:
    """Stop (if running) and remove a container. Returns (success, message)."""
    result = subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, f"Container '{container_name}' stopped and removed"
    stderr = result.stderr.strip()
    if "no such container" in stderr.lower():
        return True, f"Container '{container_name}' did not exist — skipped"
    return False, f"Failed to remove container '{container_name}': {stderr}"


def remove_image(image_name: str) -> tuple:
    """Remove a docker image. Returns (success, message)."""
    result = subprocess.run(
        ["docker", "rmi", "-f", image_name],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return True, f"Image '{image_name}' removed"
    stderr = result.stderr.strip()
    if "no such image" in stderr.lower():
        return True, f"Image '{image_name}' did not exist — skipped"
    return False, f"Failed to remove image '{image_name}': {stderr}"
