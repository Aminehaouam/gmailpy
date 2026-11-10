from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from config import DEFAULTS, AppPaths, Config  # noqa: E402
from database import Database  # noqa: E402
from fakes import FakeGmailBackend, FakeGmailService  # noqa: E402
from gmail_client import GmailClient  # noqa: E402
from mime_samples import make_pdf_bytes  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "apphome"
    home.mkdir()
    monkeypatch.setenv("BEWERBUNG_ASSISTANT_HOME", str(home))
    return home


@pytest.fixture
def paths(isolated_home) -> AppPaths:
    return AppPaths(isolated_home)


@pytest.fixture
def config(paths) -> Config:
    cfg = Config.load(paths)
    cfg.scanner["stability_check_seconds"] = 0.0  # keep tests fast
    return cfg


@pytest.fixture
def db(paths) -> Database:
    return Database(paths.database_file)


@pytest.fixture
def folder(tmp_path) -> Path:
    path = tmp_path / "Bewerbungen"
    path.mkdir()
    return path


@pytest.fixture
def make_pdf(folder):
    """Create a complete PDF in the folder with a given age (seconds)."""
    def _make(name: str, age: float = 120.0, content: bytes | None = None, label: str | None = None) -> Path:
        path = folder / name
        path.write_bytes(content if content is not None else make_pdf_bytes(label or name))
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path
    return _make


@pytest.fixture
def backend() -> FakeGmailBackend:
    return FakeGmailBackend()


@pytest.fixture
def gmail(backend) -> GmailClient:
    return GmailClient(FakeGmailService(backend), account_email=backend.account, num_retries=0)


@pytest.fixture
def parser_settings() -> dict:
    return dict(DEFAULTS["filename_parser"])
