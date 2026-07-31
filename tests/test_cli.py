"""CLI behaviour, focused on `init` bootstrapping a fresh checkout.

`init` is the one command whose failure is silent: it either wrote your config or
it did not, and until this test existed nothing checked. Running it from the repo
root used to copy companies.yaml onto itself and report success having done
nothing, which left a fresh clone with no config at all.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jobhunter.cli import app

runner = CliRunner()
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def in_empty_dir(tmp_path, monkeypatch):
    """Run the CLI from an empty directory with its own database."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOBHUNTER_DB_URL", f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    # Settings are read at import time, so point the module-level object too.
    from jobhunter import config, db

    monkeypatch.setattr(config.settings, "db_url", f"sqlite+pysqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_Session", None)
    return tmp_path


def test_example_configs_are_shipped():
    """The files `init` copies from must exist in the repo, not just locally."""
    assert (REPO_ROOT / "companies.example.yaml").is_file()
    assert (REPO_ROOT / "profile.example.yaml").is_file()


def test_example_configs_carry_no_personal_detail():
    """The examples ship; the real config is gitignored precisely because it does not.

    Checks shapes rather than one person's details, so it keeps working for anyone
    who forks this and drops their own history into profile.yaml.
    """
    import re

    for name in ("profile.example.yaml", "companies.example.yaml"):
        text = (REPO_ROOT / name).read_text()
        assert not re.search(r"[\w.+-]+@[\w-]+\.[\w.]{2,}", text), f"{name} contains an email address"
        assert not re.search(r"(?<!\w)(?:\+\d{1,3}[\s-]?)?\d{10}(?!\w)", text), (
            f"{name} contains something shaped like a phone number"
        )
        # A personal CV should never be the documented source of a shipped example.
        assert not re.search(r"\bresume\b|\bcv\b|\.docx|\.pdf", text, re.IGNORECASE), (
            f"{name} references a personal document"
        )


def test_init_writes_both_configs_from_the_examples(in_empty_dir):
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0, result.output

    companies, profile = in_empty_dir / "companies.yaml", in_empty_dir / "profile.yaml"
    assert companies.is_file(), f"init did not write companies.yaml:\n{result.output}"
    assert profile.is_file(), f"init did not write profile.yaml:\n{result.output}"
    # Real content, not an empty file.
    assert "ats_token" in companies.read_text()
    assert "must_have_keywords" in profile.read_text()


def test_init_writes_config_that_actually_loads(in_empty_dir):
    """A bootstrap that produces unparseable config is not a bootstrap."""
    from jobhunter.config import load_profile, load_targets

    runner.invoke(app, ["init"])
    targets = load_targets(in_empty_dir / "companies.yaml")
    profile = load_profile(in_empty_dir / "profile.yaml")
    assert targets and all(t.name for t in targets)
    assert profile.titles


def test_init_does_not_clobber_existing_config(in_empty_dir):
    (in_empty_dir / "profile.yaml").write_text("profile:\n  titles: [Mine]\n")
    result = runner.invoke(app, ["init"])
    assert result.exit_code == 0
    assert "Mine" in (in_empty_dir / "profile.yaml").read_text()
    assert "already exists" in result.output


def test_init_force_overwrites(in_empty_dir):
    (in_empty_dir / "profile.yaml").write_text("profile:\n  titles: [Mine]\n")
    result = runner.invoke(app, ["init", "--force"])
    assert result.exit_code == 0
    assert "Mine" not in (in_empty_dir / "profile.yaml").read_text()


def test_help_lists_every_command():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("init", "scan", "score", "contacts", "purge", "list", "export", "stats"):
        assert command in result.output


def test_commands_that_need_config_fail_clearly_without_it(in_empty_dir):
    """A missing companies.yaml should point at `init`, not raise a traceback."""
    for command in (["scan"], ["contacts"]):
        result = runner.invoke(app, command)
        assert result.exit_code == 1
        assert "init" in result.output
