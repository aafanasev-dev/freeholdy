"""
interactive_service.py
Bridge between a WebSocket and an install.sh process running on a pty, for plugins
declaring "interactive": true in plugin.json (see routers/plugins.py::install_session).

Frame protocol (JSON text frames):
  client -> server : {"type": "stdin", "data": "line\n"}
  server -> client : {"type": "stdout", "data": "..."}
The surrounding handshake (auth / ready / exit / error frames) is the router's job;
this module only pumps bytes once the session is sanctioned.

The child runs on a pty (not pipes) so prompts flush unbuffered; ECHO is disabled on
the slave so clients do their own local echo without seeing input twice. The process
is registered in docker_service's job registry under the caller's job_key, so the
transcript shows up in GET /status and abort_job works mid-session.
"""

import asyncio
import codecs
import os
import pty
import signal
import subprocess
import tempfile
import termios
import threading
from typing import Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.services import docker_service

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
) -> Optional[int]:
    """Run cmd on a pty bridged to an accepted websocket.

    Returns the process exit code, or None if the client disconnected mid-run
    (in which case the process group has been killed and the job marked aborted).
    The caller is responsible for try_acquire/release around this call and for
    sending exit/error frames.
    """
    master_fd, slave_fd = pty.openpty()
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
            if msg.get("type") == "stdin":
                os.write(master_fd, str(msg.get("data", "")).encode())

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
