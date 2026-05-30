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
) -> DockerJob:
    """Remove any stopped container then 'docker run' in the background."""
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
        "-p", f"127.0.0.1:{local_port}:{container_port}",
        image_name,
    ]
    return _spawn(job_key, "start", cmd)


def stop_container(container_name: str, job_key: str) -> DockerJob:
    """Stop a container in the background."""
    cmd = ["docker", "stop", "--time", "10", container_name]
    return _spawn(job_key, "stop", cmd)


def exec_in_container(container_name: str, command: str, job_key: str) -> DockerJob:
    """Run a command inside a container in the background."""
    cmd = ["docker", "exec", container_name] + shlex.split(command)
    return _spawn(job_key, "exec", cmd)


def provision_from_plugin(
    job_key: str,
    project_dir: str,
    plugin_dir: str,
    install_script: Optional[str],
    image_name: str,
    container_name: str,
    local_port: int,
    container_port: int,
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
        f"-p 127.0.0.1:{int(local_port)}:{int(container_port)} {q(image_name)}",
    ]

    cmd = ["bash", "-c", "\n".join(lines)]
    return _spawn(job_key, "provision", cmd)


def _compose_cmd(project: str, project_dir: str, *args: str) -> list:
    """Build a `docker compose` invocation pinned to a project's two compose files."""
    return [
        "docker", "compose",
        "-p", project,
        "-f", os.path.join(project_dir, "docker-compose.yml"),
        "-f", os.path.join(project_dir, "docker-compose.override.yml"),
        *args,
    ]


def compose_build(project: str, project_dir: str, job_key: str) -> DockerJob:
    """Start 'docker compose build' in the background. Poll get_job(job_key)."""
    cmd = _compose_cmd(project, project_dir, "build", "--progress=plain")
    return _spawn(job_key, "compose_build", cmd)


def compose_up(project: str, project_dir: str, job_key: str) -> DockerJob:
    """Start 'docker compose up -d' in the background (builds images as needed)."""
    cmd = _compose_cmd(project, project_dir, "up", "-d")
    return _spawn(job_key, "compose_up", cmd)


def compose_down(project: str, project_dir: str, job_key: str) -> DockerJob:
    """Start 'docker compose down' in the background."""
    cmd = _compose_cmd(project, project_dir, "down")
    return _spawn(job_key, "compose_down", cmd)


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
