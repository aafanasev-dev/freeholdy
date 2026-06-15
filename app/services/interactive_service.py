"""
interactive_service.py
Bridge between a WebSocket and an install.sh process running on a pty, for plugins
declaring "interactive": true in plugin.json (see routers/plugins.py::install_session).

Frame protocol (JSON text frames):
  client -> server : {"type": "stdin", "data": "line\n"}
                     {"type": "resize", "rows": R, "cols": C}   (interactive shells)
                     {"type": "abort"}                          (stream_job only)
  server -> client : {"type": "stdout", "data": "..."}
The surrounding handshake (auth / ready / exit / error frames) is the router's job;
this module only pumps bytes once the session is sanctioned.

run_session bridges a *live* pty (install.sh, or `docker exec -it` when raw=True).
stream_job tails an *already-running* docker_service background job's log to the same
client (used to show build output during install) without owning the process, so the
build survives a client disconnect.

The child runs on a pty (not pipes) so prompts flush unbuffered; ECHO is disabled on
the slave so clients do their own local echo without seeing input twice. The process
is registered in docker_service's job registry under the caller's job_key, so the
transcript shows up in GET /status and abort_job works mid-session.
"""

import asyncio
import codecs
import fcntl
import os
import pty
import shlex
import signal
import struct
import subprocess
import tempfile
import termios
import threading
import tty
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.services import docker_service, ws_session

# One interactive session per job_key at a time.
_active: set = set()
_active_lock = threading.Lock()

_KILL_GRACE_SECONDS = 5


def try_acquire(key: str) -> bool:
    """Claim the interactive-session slot for key. Returns False if already held."""
    with _active_lock:
        if key in _active:
            return False
        _active.add(key)
        return True


def release(key: str) -> None:
    with _active_lock:
        _active.discard(key)


def _set_winsize(fd: int, rows, cols) -> None:
    """Apply a client-reported terminal size to the pty so full-screen programs (top,
    vim) lay out correctly. Ignores malformed values."""
    try:
        rows, cols = int(rows), int(cols)
    except (TypeError, ValueError):
        return
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
    except OSError:
        pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGTERM the child's process group, escalate to SIGKILL after a grace period.
    The child was started with start_new_session=True, so pid == pgid."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=_KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait()


async def run_session(
    websocket: WebSocket,
    cmd: list,
    cwd: str,
    env: dict,
    job_key: str,
    operation: str = "install",
    raw: bool = False,
) -> Optional[int]:
    """Run cmd on a pty bridged to an accepted websocket.

    Returns the process exit code, or None if the client disconnected mid-run
    (in which case the process group has been killed and the job marked aborted).
    The caller is responsible for try_acquire/release around this call and for
    sending exit/error frames.

    raw=True puts the pty in raw mode for an interactive shell (the container's shell
    owns echo and line editing — e.g. `docker exec -it`). raw=False (install.sh prompts)
    keeps the pty in line mode with echo suppressed so clients can echo locally.
    """
    master_fd, slave_fd = pty.openpty()
    if raw:
        tty.setraw(slave_fd)
    else:
        # Clients echo locally; without this every typed line would come back duplicated.
        attrs = termios.tcgetattr(slave_fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        start_new_session=True,
        close_fds=True,
    )
    os.close(slave_fd)

    log_fd = tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".log",
        prefix=f"freeholdy_{operation}_",
        delete=False,
    )
    docker_service.register_external_job(job_key, operation, cmd, proc, log_fd.name)

    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _on_readable() -> None:
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            data = b""  # Linux ptys raise EIO at EOF once the child exits
        if data:
            queue.put_nowait(data)
        else:
            loop.remove_reader(master_fd)
            queue.put_nowait(None)

    loop.add_reader(master_fd, _on_readable)

    # os.read can split multi-byte UTF-8 sequences across chunks.
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    async def pump_output() -> None:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            text = decoder.decode(chunk)
            if not text:
                continue
            log_fd.write(text)
            log_fd.flush()
            await websocket.send_json({"type": "stdout", "data": text})

    async def pump_input() -> None:
        while True:
            try:
                msg = await websocket.receive_json()
            except (ValueError, KeyError):
                await websocket.send_json({"type": "error", "message": "expected JSON frames"})
                continue
            mtype = msg.get("type")
            if mtype == "stdin":
                os.write(master_fd, str(msg.get("data", "")).encode())
            elif mtype == "resize":
                _set_winsize(master_fd, msg.get("rows"), msg.get("cols"))

    out_task = asyncio.ensure_future(pump_output())
    in_task = asyncio.ensure_future(pump_input())
    try:
        done, _pending = await asyncio.wait(
            {out_task, in_task}, return_when=asyncio.FIRST_COMPLETED
        )

        if out_task in done:
            # pty EOF — the child exited on its own.
            in_task.cancel()
            exit_code = await loop.run_in_executor(None, proc.wait)
            docker_service.finish_external_job(job_key, exit_code)
            return exit_code

        # Input pump finished first: the client went away (WebSocketDisconnect) or the
        # receive failed. Either way the user is gone — kill the install and bail out.
        await loop.run_in_executor(None, _kill_process_group, proc)
        out_task.cancel()
        docker_service.finish_external_job(job_key, proc.returncode, aborted=True)
        # Surface a real disconnect to the caller as "client gone", anything else too.
        exc = in_task.exception()
        if exc is not None and not isinstance(exc, WebSocketDisconnect):
            raise exc
        return None
    finally:
        for task in (out_task, in_task):
            if not task.done():
                task.cancel()
        try:
            loop.remove_reader(master_fd)
        except (ValueError, OSError):
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        log_fd.close()
        if proc.poll() is None:
            await loop.run_in_executor(None, _kill_process_group, proc)
            docker_service.finish_external_job(job_key, proc.returncode, aborted=True)


async def run_exec_session(
    websocket: WebSocket,
    container_name: str,
    cwd: str,
    job_key: str,
    command: str = "",
) -> None:
    """Bridge an already-accepted+authenticated websocket to an interactive `docker exec
    -it` shell on a pty. Handles the running-container check, the per-key session lock,
    and the ready/exit/close frames. `command` (optional) overrides the default shell.

    Shared by the dockerfile and compose exec WebSocket routes.
    """
    if docker_service.get_container_status(container_name) != "running":
        await ws_session.reject(websocket, 4409, f"container '{container_name}' is not running")
        return
    if not try_acquire(job_key):
        await ws_session.reject(websocket, 4409, "an exec session is already in progress")
        return
    try:
        # cwd is irrelevant to `docker exec`; fall back if the project dir is missing.
        if not os.path.isdir(cwd):
            cwd = os.path.abspath(os.sep)
        cmd = ["docker", "exec", "-it", container_name] + (shlex.split(command) or ["sh"])
        await websocket.send_json({"type": "ready"})
        exit_code = await run_session(
            websocket, cmd, cwd=cwd, env=dict(os.environ),
            job_key=job_key, operation="exec", raw=True,
        )
        if exit_code is None:
            return  # client disconnected; the exec was killed
        await websocket.send_json({"type": "exit", "code": exit_code})
        await websocket.close(code=1000)
    except WebSocketDisconnect:
        pass
    finally:
        release(job_key)


async def stream_job(websocket: WebSocket, job_key: str, poll: float = 0.2) -> Optional[int]:
    """Tail a docker_service background job's log to the websocket until it finishes.

    Sends {"type":"stdout"} frames for new output and returns the job's exit code once it
    leaves "running". Unlike run_session this does NOT own the process: a client
    disconnect simply stops the stream (the job keeps running) and returns None, and a
    {"type":"abort"} client frame aborts the job. Returns -1 if no job ever appears.
    The caller sends the exit/close frames.
    """
    # The POST that launched the job may still be racing us — wait briefly for it.
    job = docker_service.get_job(job_key)
    waited = 0.0
    while job is None and waited < 5.0:
        await asyncio.sleep(poll)
        waited += poll
        job = docker_service.get_job(job_key)
    if job is None:
        return -1

    async def pump_input() -> None:
        # Watch for an abort frame or the client going away; either ends the stream.
        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "abort":
                    docker_service.abort_job(job_key)
        except (WebSocketDisconnect, ValueError, KeyError, RuntimeError):
            return

    async def pump_output() -> Optional[int]:
        offset = 0
        while True:
            # Sample status BEFORE reading: the monitor thread flushes+closes the log
            # before flipping status off "running", so once we observe a finished job a
            # subsequent read is guaranteed to see the complete log.
            running = job.status == "running"
            try:
                with open(job.log_path, "r") as f:
                    f.seek(offset)
                    chunk = f.read()
                    offset = f.tell()
            except OSError:
                chunk = ""
            if chunk:
                await websocket.send_json({"type": "stdout", "data": chunk})
            if not running:
                # abort_job flips status before the monitor thread records exit_code —
                # wait briefly so we always return a real code (never None, which the
                # caller reads as "client disconnected").
                for _ in range(40):
                    if job.exit_code is not None:
                        break
                    await asyncio.sleep(0.025)
                return job.exit_code if job.exit_code is not None else -1
            await asyncio.sleep(poll)

    in_task = asyncio.ensure_future(pump_input())
    out_task = asyncio.ensure_future(pump_output())
    try:
        done, _pending = await asyncio.wait(
            {in_task, out_task}, return_when=asyncio.FIRST_COMPLETED
        )
        # Output pump finished → job done; report its exit code. Otherwise the client
        # went away first — leave the job running and signal "gone".
        return out_task.result() if out_task in done else None
    finally:
        for task in (in_task, out_task):
            if not task.done():
                task.cancel()
