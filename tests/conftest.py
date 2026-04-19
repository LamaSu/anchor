"""Shared pytest fixtures. Isolates tests from user's real ~/.anchor."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_anchor_home(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("ANCHOR_HOME", tmp)
        yield Path(tmp)
