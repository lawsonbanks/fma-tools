"""The one place fma keeps state: the Xero sign-in.

Every other fma tool discovers nothing and writes nowhere except the paths it is given.
This module is the stated exception, and it is kept small so the exception stays
visible: three JSON files in one folder outside any repo or synced drive.

    app.json      the registered app's client id and the callback port
    tokens.json   one rotating sign-in per Xero login that has authorised
    tenants.json  the organisations those logins have connected, by short key

Xero replaces the refresh token on every refresh and the old one stops working, so a
write that is lost or half-made is a dead sign-in. Every write here is whole-file,
0600, to a temporary name in the same folder and then moved into place; refresh runs
under a lock so two processes cannot each spend the same token.

Nothing in this module prints or returns a token in an error message.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import stat
from pathlib import Path

from ..errors import EnvProblem, InputProblem

APP, TOKENS, TENANTS, PENDING = "app.json", "tokens.json", "tenants.json", "pending.json"
_FILES = (APP, TOKENS, TENANTS, PENDING)

CONFIG_FIX = "fma xero config --client-id <the app's client id>"
AUTH_FIX = "fma xero auth"


def config_dir() -> Path:
    """`$FMA_CONFIG_DIR/xero`, else `~/.config/fma/xero`. Named after the tool, not a
    drive or a client, so it is the same folder on every Mac."""
    base = os.environ.get("FMA_CONFIG_DIR")
    root = Path(base).expanduser() if base else Path.home() / ".config" / "fma"
    return root / "xero"


def is_configured() -> bool:
    return (config_dir() / APP).exists()


def _ensure_dir() -> Path:
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    # mkdir's mode is masked by umask; set it outright so the folder is private even
    # when it already existed with looser permissions.
    os.chmod(d, 0o700)
    return d


def load(name: str) -> dict:
    """The file's contents, or {} when it has never been written. A file that exists
    and cannot be read is a refusal to guess, never an empty sign-in."""
    p = config_dir() / name
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        raise InputProblem("XERO_STORE_UNREADABLE",
                           f"{p} exists but cannot be read as JSON ({type(e).__name__}). "
                           "Do not delete it blind: it may hold the only live sign-in.")
    if not isinstance(data, dict):
        raise InputProblem("XERO_STORE_UNREADABLE", f"{p} does not hold a JSON object")
    return data


def save(name: str, data: dict) -> None:
    d = _ensure_dir()
    final = d / name
    tmp = d / f".{name}.{os.getpid()}.tmp"
    payload = json.dumps(data, indent=1, sort_keys=True).encode()
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(payload)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, final)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise


def delete(name: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        (config_dir() / name).unlink()


@contextlib.contextmanager
def locked():
    """Serialise anything that spends a refresh token."""
    d = _ensure_dir()
    fd = os.open(d / ".lock", os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def app() -> dict:
    """The registered app. Refuses with the fix line when it has not been set."""
    a = load(APP)
    if not a.get("client_id"):
        raise EnvProblem("XERO_NOT_CONFIGURED",
                         "no Xero app is configured on this Mac", fix=CONFIG_FIX)
    return a


def mode_problems() -> list[str]:
    """What is looser than 0700 / 0600. Empty when the folder is private."""
    d = config_dir()
    out = []
    if not d.exists():
        return out
    if stat.S_IMODE(d.stat().st_mode) != 0o700:
        out.append(f"{d} is mode {stat.S_IMODE(d.stat().st_mode):o}, want 700")
    for name in _FILES:
        p = d / name
        if p.exists() and stat.S_IMODE(p.stat().st_mode) != 0o600:
            out.append(f"{p} is mode {stat.S_IMODE(p.stat().st_mode):o}, want 600")
    return out
