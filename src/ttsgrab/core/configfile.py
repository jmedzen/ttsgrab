"""Writing settings back to ``config.yaml`` from the UI.

Everything the gateway reads from ``config.yaml`` can be edited in the web UI, including
provider API keys. That reverses an earlier rule — the file used to say "secrets belong in
.env, never here" — and the reason is that this is a single-tenant product: one person or a
small team on one instance. Spreading settings across a YAML file, a `.env`, three
environment variables and a database meant nobody could say where a value came from, and a
key pasted into the UI evaporated on restart.

The rules this module enforces:

* **Comments survive.** PyYAML would rewrite the file without them; the comments in
  ``config.example.yaml`` are the documentation for every option. ruamel round-trips them.
* **The file stays 0600.** It now holds API keys, which is a deliberate trade for a
  single-tenant box. Anyone who can read the file already runs code on the machine, so the
  marginal risk is low — but a world-readable file is still a needless invitation.
* **Writes are atomic.** A crash mid-write must not leave a truncated config that stops the
  service from booting.
* **Environment still wins.** ``config.yaml`` sits below the environment in precedence, so
  an operator using ``TTSGRAB_*`` variables can still override what the UI wrote. The UI
  reports when this is happening rather than pretending its value took effect.
"""

from __future__ import annotations

import contextlib
import io
import os
import tempfile
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError

from ttsgrab.core.errors import InvalidRequest

# Keys that must never be echoed back through the API, only written and reported as set.
SECRET_FIELDS = frozenset(
    {
        "admin_token",
        "api_key",
        "credentials_file",
        # Both pools: a free Google AI Studio key is still a credential, just a
        # rate-limited one.
        "free_api_keys",
        "paid_api_keys",
        "api_keys",
    }
)


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    # Indent nested mappings two spaces, matching the hand-written file.
    y.indent(mapping=2, sequence=4, offset=2)
    # Do not re-wrap long lines. The default width of 80 would take a hand-written
    # flow-style list such as the fallback chain and fold it across two lines, which turns
    # a one-line diff into a spurious reformat of the operator's file.
    y.width = 4096
    return y


def config_path(configured: str | os.PathLike[str] | None) -> Path:
    return Path(configured or os.environ.get("TTSGRAB_CONFIG") or "config.yaml")


def read_raw(path: Path) -> dict[str, Any]:
    """The file as a plain dict, for merging. Missing or empty is an empty mapping."""
    if not path.is_file():
        return {}
    try:
        loaded = _yaml().load(path.read_text(encoding="utf-8"))
    except YAMLError as exc:
        raise InvalidRequest(f"config.yaml is not valid YAML: {exc}") from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise InvalidRequest("config.yaml must contain a mapping at the top level")
    return dict(loaded)


def set_values(path: Path, updates: dict[str, Any]) -> None:
    """Merge ``updates`` into the file, preserving comments and everything not mentioned.

    ``None`` removes a key. Nested dicts merge rather than replace, so setting
    ``google_cloud.enabled`` does not wipe the rest of the ``google_cloud`` block.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    if path.is_file():
        try:
            doc = _yaml().load(path.read_text(encoding="utf-8"))
        except YAMLError as exc:
            # Refuse rather than overwrite: the operator may have a half-edited file and
            # silently replacing it would destroy work.
            raise InvalidRequest(f"config.yaml is not valid YAML: {exc}") from exc
    else:
        doc = None
    if doc is None:
        doc = {}

    for dotted, value in updates.items():
        _assign(doc, dotted.split("."), value)

    buf = io.StringIO()
    _yaml().dump(doc, buf)
    text = buf.getvalue()

    _atomic_write(path, text)


def _assign(node: Any, parts: list[str], value: Any) -> None:
    head, rest = parts[0], parts[1:]
    if not rest:
        if value is None:
            node.pop(head, None)
        else:
            node[head] = value
        return
    child = node.get(head)
    if child is None or not isinstance(child, dict):
        if value is None:
            return
        child = {}
        node[head] = child
    _assign(child, rest, value)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file in the same directory, then rename.

    A partial write here would leave a config that the service cannot parse, which means it
    cannot start — the worst possible failure for a settings page.
    """
    # A config that holds API keys should not be world-readable on a shared machine.
    mode = 0o600
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def mask(value: Any) -> Any:
    """Replace a secret with a fingerprint that proves *which* value is set.

    Returning the real key through the API would put it in browser history, devtools and
    any screenshot of the settings page. A short tail is enough for a human to tell two
    keys apart without being enough to use one.
    """
    if not isinstance(value, str) or not value:
        return value
    if len(value) <= 6:
        return "••••"
    return f"••••{value[-4:]}"


def mask_tree(data: dict[str, Any]) -> dict[str, Any]:
    """Mask secrets recursively, including list members such as a key pool."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out[key] = mask_tree(value)
        elif isinstance(value, list) and key in SECRET_FIELDS:
            out[key] = [mask(v) if isinstance(v, str) else v for v in value]
        elif key in SECRET_FIELDS:
            out[key] = mask(value)
        else:
            out[key] = value
    return out
