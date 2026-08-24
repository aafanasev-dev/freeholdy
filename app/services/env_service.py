"""
env_service.py — .env-format environment variables for projects.

The DB (`ProjectEnvFile`) is the source of truth: one file per project
(`service_name == ""`) and, for compose projects, one per service. This module parses and
validates that content, and **materializes** it as normalized `KEY=value` files under
`{DATA_DIR}/env/{project}/` — deliberately *outside* `PROJECTS_DIR`, because:

  * `git.py` wipes the project dir on every git redeploy,
  * `deploy_service._compose_rollback_job` replaces it with a version snapshot,
  * `projects.py` rmtree's it on teardown,
  * and `{project_dir}/.env` is owned by plugin install.sh scripts (compose `${VAR}`
    interpolation) — freeholdy must never read, rewrite or truncate it.

The materialized files are consumed two ways: `docker run --env-file` for dockerfile
projects, and `env_file:` entries in the generated compose override for compose services.
Both readers are line-based `KEY=value` parsers, which is why `render` emits values raw and
unquoted and why a value containing a newline is rejected at validation time.

Materializing happens at deploy and at restart — never on save. Editing an env file stores
it and nothing more; the values reach the container the next time it starts. `is_applied`
compares the DB content against what is on disk to tell a client whether a restart is due.

Pure parsing + filesystem, like `compose_service` — orchestration lives in the routers and
in `deploy_service`.
"""

import os
import re
import shutil

from app.config import settings
from app.models.orm import ComposeService, Project, ProjectEnvFile

# Environment variable names: the POSIX-portable set, which is also what docker and
# compose accept without quoting games.
KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

PROJECT_SCOPE = ""          # ProjectEnvFile.service_name for the project-level file
_PROJECT_FILE = "_project.env"
_SERVICE_PREFIX = "svc-"


# ── Parsing / rendering ─────────────────────────────────────────────────────────

def _unquote(raw: str) -> str:
    """Strip a matching pair of surrounding quotes and unescape a double-quoted value."""
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        body = raw[1:-1]
        if raw[0] == '"':
            return (body.replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")
                        .replace('\\"', '"').replace("\\\\", "\\"))
        return body
    return raw


def parse(content: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse .env text into `(pairs, errors)`.

    Understands blank lines, `#` comments, an optional `export ` prefix, `KEY=VALUE`, and
    single- or double-quoted values (quotes stripped; `\\n`/`\\t`/`\\"`/`\\\\` unescaped
    inside double quotes only). Duplicate keys are allowed — last one wins, like dotenv.

    `errors` is a list of human-readable problems, each naming its line number. A non-empty
    list means the content is rejected (the router turns it into a 422); `pairs` is still
    returned for whatever parsed, so a caller can show partial results.
    """
    pairs: list[tuple[str, str]] = []
    errors: list[str] = []

    for lineno, line in enumerate(content.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export "):].lstrip()
        if "=" not in stripped:
            errors.append(f"line {lineno}: expected KEY=VALUE, got {line.strip()!r}")
            continue
        key, _, raw = stripped.partition("=")
        key = key.strip()
        if not KEY_RE.match(key):
            errors.append(
                f"line {lineno}: {key!r} is not a valid variable name — use letters, "
                f"digits and underscores, not starting with a digit"
            )
            continue
        value = _unquote(raw.strip())
        if "\n" in value or "\r" in value:
            errors.append(
                f"line {lineno}: the value of {key} spans multiple lines — docker's "
                f"--env-file and compose's env_file are both line-based, so values must "
                f"be single-line"
            )
            continue
        pairs.append((key, value))

    return pairs, errors


def keys(content: str) -> list[str]:
    """The variable names defined by `content`, in order, deduplicated (last wins)."""
    pairs, _ = parse(content)
    return list(dict.fromkeys(k for k, _ in pairs))


def render(pairs: list[tuple[str, str]]) -> str:
    """Render pairs as the normalized `KEY=value` form both docker and compose read.

    Values go out verbatim and unquoted: docker's `--env-file` does not strip quotes, so
    quoting here would leak the quote characters into the container.
    """
    return "".join(f"{k}={v}\n" for k, v in pairs)


def normalized(content: str) -> str:
    """The on-disk form of a stored env file — parse then render, dropping comments."""
    pairs, _ = parse(content)
    return render(pairs)


# ── Paths ───────────────────────────────────────────────────────────────────────

def env_dir(project_name: str) -> str:
    """The directory holding a project's materialized env files. Outside PROJECTS_DIR."""
    return os.path.join(settings.DATA_DIR, "env", project_name)


def env_path(project_name: str, service: str | None = None) -> str:
    """Absolute path of the materialized file for a scope (`None`/`""` = project-level)."""
    fname = f"{_SERVICE_PREFIX}{service}.env" if service else _PROJECT_FILE
    return os.path.abspath(os.path.join(env_dir(project_name), fname))


# ── DB access ───────────────────────────────────────────────────────────────────

def get_row(db, project: Project, service: str | None = None) -> ProjectEnvFile | None:
    """The stored file for a scope, or None when the project has no env for it yet."""
    return (
        db.query(ProjectEnvFile)
        .filter(
            ProjectEnvFile.project_id == project.id,
            ProjectEnvFile.service_name == (service or PROJECT_SCOPE),
        )
        .first()
    )


def get_content(db, project: Project, service: str | None = None) -> str:
    row = get_row(db, project, service)
    return row.content if row else ""


def set_content(db, project: Project, content: str, service: str | None = None) -> ProjectEnvFile:
    """Store (or clear) a scope's file. Does **not** touch any container — the values are
    picked up the next time it starts (POST /projects/{name}/restart, or a deploy)."""
    row = get_row(db, project, service)
    if row is None:
        row = ProjectEnvFile(
            project_id=project.id,
            service_name=service or PROJECT_SCOPE,
            content=content,
        )
        db.add(row)
    else:
        row.content = content
    db.flush()
    return row


def delete_row(db, project: Project, service: str | None = None) -> bool:
    """Drop a scope's file. Returns True when something was actually removed."""
    row = get_row(db, project, service)
    if row is None:
        return False
    db.delete(row)
    db.flush()
    return True


# ── Materializing ───────────────────────────────────────────────────────────────

def _write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("# Generated by freeholdy from the database. Do not edit manually.\n")
        f.write(text)
    os.chmod(path, 0o600)


def materialize(db, project: Project) -> dict[str, list[str]]:
    """Write the project's env files to `{DATA_DIR}/env/{project}/` and return, per scope,
    the paths to feed docker in precedence order.

    The returned mapping is keyed by compose service name (and `""` for the project-level
    scope). Each value lists the project-level file first and the service file second, so
    a later `env_file:` entry — or a later `--env-file` — overrides an earlier one, giving
    service values priority over shared ones.

    Empty content writes no file and yields no path, so a project with no env configured
    produces byte-identical docker commands to before this feature existed. Stale files
    (a cleared scope, a renamed service) are removed. Env rows for services that no longer
    exist are simply skipped — harmless, and they come back if the service returns.
    """
    d = env_dir(project.name)
    project_content = normalized(get_content(db, project))
    project_file = env_path(project.name)

    wanted: set[str] = set()
    result: dict[str, list[str]] = {}

    if project_content:
        _write(project_file, project_content)
        wanted.add(os.path.basename(project_file))
    base = [project_file] if project_content else []

    if project.deploy_mode == "compose":
        # Queried rather than read off `project.services`: provision_compose deletes and
        # re-adds every row by project_id, so the loaded collection can be stale here.
        names = [
            r[0] for r in db.query(ComposeService.name)
            .filter(ComposeService.project_id == project.id).all()
        ]
        for svc_name in names:
            content = normalized(get_content(db, project, svc_name))
            path = env_path(project.name, svc_name)
            if content:
                _write(path, content)
                wanted.add(os.path.basename(path))
                result[svc_name] = base + [path]
            else:
                result[svc_name] = list(base)
    else:
        result[PROJECT_SCOPE] = list(base)

    # Drop files for scopes that no longer have content.
    if os.path.isdir(d):
        for fname in os.listdir(d):
            if fname not in wanted and fname.endswith(".env"):
                try:
                    os.remove(os.path.join(d, fname))
                except OSError:
                    pass

    return result


def project_env_file(db, project: Project) -> str | None:
    """The materialized project-level file, or None when there is no project-level env.

    Used for the dockerfile `docker run --env-file` and for compose `${VAR}` interpolation.
    Call after `materialize`.
    """
    if not normalized(get_content(db, project)):
        return None
    path = env_path(project.name)
    return path if os.path.exists(path) else None


def is_applied(db, project: Project, service: str | None = None) -> bool:
    """Whether the stored content matches what is on disk — i.e. whether the running
    container was started with these values. The on-disk file is only rewritten at deploy
    and restart time, so a False here is exactly the UI's "restart to apply" condition.
    """
    want = normalized(get_content(db, project, service))
    path = env_path(project.name, service)
    if not want:
        return not os.path.exists(path)
    if not os.path.exists(path):
        return False
    with open(path) as f:
        have = f.read()
    # Skip the generated-by banner _write prepends.
    _, _, body = have.partition("\n")
    return body == want


def remove_project(project_name: str) -> None:
    """Delete a project's materialized env directory (teardown). Rows cascade off Project."""
    shutil.rmtree(env_dir(project_name), ignore_errors=True)
