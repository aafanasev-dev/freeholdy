"""
git_service.py
Thin wrapper around `git clone` for the git-project add-flow (routers/git.py), plus
self-service SSH key management so users can clone private repos over SSH.

Follows the (success, message) convention of docker_service / nginx_service: the
router decides the HTTP shape. The URL is passed as argv (never a shell string), so
clone can't be used for command injection.
"""

import os
import subprocess

from app.config import settings

# One server-global SSH key for GitHub. The clone runs as the server process user, so
# everything is rooted at that user's home (~/.ssh = /root/.ssh in production).
GITHUB_HOST = "github.com"
_SSH_DIR = os.path.expanduser("~/.ssh")
_SSH_CONFIG = os.path.join(_SSH_DIR, "config")
_KEY_PATH = os.path.join(_SSH_DIR, "freeholdy_github")


def clone(git_url: str, dest: str, branch: str | None = None) -> tuple[bool, str]:
    """Shallow-clone `git_url` into `dest` (which must not already exist or must be
    empty — the caller clears any stale dir first). When `branch` is given, that ref
    is checked out. Returns (success, message); message carries git's stderr on failure."""
    cmd = ["git", "clone", "--depth", "1"]
    if branch:
        cmd += ["--branch", branch]
    cmd += [git_url, dest]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode == 0:
        return True, f"Cloned {git_url} into {dest}"
    return False, (result.stderr.strip() or result.stdout.strip() or "git clone failed")


# ── SSH key management ──────────────────────────────────────────────────────────
#
# A single server-global GitHub key, exposed via GET /git/key so a user can add the
# public half to GitHub and clone private repos over SSH (git@github.com:owner/repo).
# The key is keyed off the `Host github.com` block in the server user's ~/.ssh/config.

# git stderr fragments that indicate an SSH auth/host problem rather than e.g. a bad URL.
_AUTH_FAILURE_MARKERS = (
    "permission denied",
    "publickey",
    "could not read from remote repository",
    "host key verification failed",
    "authentication failed",
)


def is_auth_failure(message: str) -> bool:
    """True when a git clone error looks like an SSH auth/host failure (so the caller can
    point the user at `get-git-key`) rather than a missing repo or malformed URL."""
    low = (message or "").lower()
    return any(marker in low for marker in _AUTH_FAILURE_MARKERS)


def github_instructions(public_key: str) -> str:
    """Human-readable steps for adding the server's public key to GitHub."""
    return (
        "Add this public key to GitHub so the server can clone your private repos over SSH:\n"
        "  1. Open https://github.com/settings/ssh/new (Settings → SSH and GPG keys → New SSH key).\n"
        "  2. Give it a title (e.g. \"freeholdy server\") and paste the key below into \"Key\".\n"
        "  3. Click \"Add SSH key\".\n"
        "Alternatively, for a single repo only, add it as a Deploy key under that repo's\n"
        "Settings → Deploy keys. Then retry creating the git project with a git@github.com:… URL."
    )


def _identity_file_for_github() -> str | None:
    """Return the IdentityFile of the `Host github.com` block in ~/.ssh/config (with `~`
    expanded), or None if there is no such block / no IdentityFile in it."""
    if not os.path.isfile(_SSH_CONFIG):
        return None
    with open(_SSH_CONFIG, "r") as f:
        lines = f.readlines()

    in_block = False
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, _, value = stripped.partition(" ")
        key = key.lower()
        if key == "host":
            # A Host line may list several patterns; the block matches if github.com is one.
            patterns = value.split()
            in_block = GITHUB_HOST in patterns
            continue
        if in_block and key == "identityfile":
            return os.path.expanduser(value.strip().strip('"'))
    return None


def _read_public_key(identity_file: str) -> str | None:
    """Read `<identity_file>.pub`; if absent but the private key exists, derive it with
    `ssh-keygen -y`. Returns the public key text or None when neither is available."""
    pub_path = identity_file + ".pub"
    if os.path.isfile(pub_path):
        with open(pub_path, "r") as f:
            return f.read().strip()
    if os.path.isfile(identity_file):
        result = subprocess.run(
            ["ssh-keygen", "-y", "-f", identity_file], capture_output=True, text=True
        )
        if result.returncode == 0:
            return result.stdout.strip()
    return None


def _append_github_config() -> None:
    """Append a `Host github.com` block pointing at the freeholdy key to ~/.ssh/config."""
    block = (
        "\nHost github.com\n"
        "    HostName github.com\n"
        "    User git\n"
        f"    IdentityFile {_KEY_PATH}\n"
        "    IdentitiesOnly yes\n"
        "    StrictHostKeyChecking accept-new\n"
    )
    existed = os.path.isfile(_SSH_CONFIG)
    with open(_SSH_CONFIG, "a") as f:
        f.write(block)
    if not existed:
        os.chmod(_SSH_CONFIG, 0o600)


def get_or_create_github_key() -> tuple[str, bool]:
    """Return (public_key, created). If ~/.ssh/config already has a `Host github.com`
    block, return its key (created=False). Otherwise generate an ed25519 keypair, add the
    config block, and return the new public key (created=True). Raises RuntimeError on a
    genuine ssh-keygen failure."""
    os.makedirs(_SSH_DIR, mode=0o700, exist_ok=True)

    identity_file = _identity_file_for_github()
    if identity_file:
        pubkey = _read_public_key(identity_file)
        if pubkey:
            return pubkey, False
        # Config points at a key that no longer exists on disk — fall through and recreate.

    result = subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-N", "", "-C", f"freeholdy@{settings.BASE_DOMAIN}",
         "-f", _KEY_PATH],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ssh-keygen failed")

    if not _identity_file_for_github():
        _append_github_config()

    pubkey = _read_public_key(_KEY_PATH)
    if not pubkey:
        raise RuntimeError("generated key but could not read its public half")
    return pubkey, True


def github_key_path() -> str:
    """Server-side path of the private key backing the `Host github.com` config block."""
    return _identity_file_for_github() or _KEY_PATH
