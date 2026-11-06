"""Writing config.yaml from the UI must not damage the operator's file.

Every test here guards a specific way a naive save could ruin it: losing comments (PyYAML),
reformatting untouched lines, or leaving a half-written file that stops the service booting.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ttsgrab.core import configfile
from ttsgrab.core.errors import InvalidRequest

SAMPLE = """\
# Top comment that documents the file.
env: dev

security:
  # Leave empty during local development.
  allow_localhost_without_token: true

router:
  fallback_chain: [edge, kokoro, google_cloud, azure]
"""


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(SAMPLE, encoding="utf-8")
    return p


def test_comments_survive_a_write(cfg: Path) -> None:
    configfile.set_values(cfg, {"env": "prod"})
    text = cfg.read_text()
    assert "# Top comment that documents the file." in text
    assert "# Leave empty during local development." in text


def test_untouched_lines_are_byte_identical(cfg: Path) -> None:
    """A save must produce a minimal diff.

    ruamel reformats long lines by default, which turns "set one key" into a diff that
    rewrites the fallback chain across two lines.
    """
    before = {ln for ln in SAMPLE.splitlines() if "fallback_chain" in ln}
    configfile.set_values(cfg, {"env": "prod"})
    after = {ln for ln in cfg.read_text().splitlines() if "fallback_chain" in ln}
    assert before == after


def test_nested_updates_merge_rather_than_replace(cfg: Path) -> None:
    configfile.set_values(cfg, {"security.allow_localhost_without_token": False})
    raw = configfile.read_raw(cfg)
    assert raw["security"]["allow_localhost_without_token"] is False
    # The sibling comment block is still attached to the right key.
    assert "# Leave empty during local development." in cfg.read_text()


def test_a_new_section_is_created(cfg: Path) -> None:
    configfile.set_values(cfg, {"google_cloud.enabled": True})
    assert configfile.read_raw(cfg)["google_cloud"]["enabled"] is True


def test_none_removes_a_key(cfg: Path) -> None:
    configfile.set_values(cfg, {"env": None})
    assert "env" not in configfile.read_raw(cfg)


def test_the_file_is_not_world_readable(cfg: Path) -> None:
    """It now holds API keys, so 0600 is the floor, not a nicety."""
    configfile.set_values(cfg, {"security.admin_token": "s3cret"})
    mode = stat.S_IMODE(os.stat(cfg).st_mode)
    assert mode == 0o600


def test_invalid_yaml_is_refused_rather_than_overwritten(tmp_path: Path) -> None:
    """A half-edited file must not be silently replaced."""
    broken = tmp_path / "config.yaml"
    broken.write_text("key: [unclosed\n", encoding="utf-8")
    with pytest.raises(InvalidRequest):
        configfile.set_values(broken, {"env": "prod"})
    assert "unclosed" in broken.read_text()


def test_no_temporary_files_are_left_behind(cfg: Path) -> None:
    configfile.set_values(cfg, {"env": "prod"})
    leftovers = [p.name for p in cfg.parent.iterdir() if p.name.startswith(".config-")]
    assert leftovers == []


def test_secrets_are_masked_but_distinguishable() -> None:
    masked = configfile.mask("AIzaSyABCDEFGH1234")
    assert "ABCDEFGH" not in masked
    assert masked.endswith("1234")
    # Two different keys must not mask to the same thing.
    assert configfile.mask("AIzaSyZZZZZZZZ9999") != masked


def test_masking_walks_nested_dicts_and_lists() -> None:
    tree = {
        "google_cloud": {"api_key": "AIzaSecretValue999"},
        "google_ai_studio": {"free_api_keys": ["AIzaOne1111", "AIzaTwo2222"]},
        "edge": {"enabled": True},
    }
    out = configfile.mask_tree(tree)
    assert "AIzaSecretValue999" not in str(out)
    assert out["edge"]["enabled"] is True
    assert out["google_ai_studio"]["free_api_keys"] == ["••••1111", "••••2222"]
