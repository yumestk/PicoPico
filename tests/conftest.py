"""Tests never read the developer's real Pico model credentials."""

import os
from pathlib import Path

import pytest

from pico import settings


@pytest.fixture(autouse=True)
def isolate_pico_settings(tmp_path, monkeypatch):
    before = dict(os.environ)
    isolated = tmp_path / 'pico-settings'
    monkeypatch.setenv('PICO_CONFIG_DIR', str(isolated))
    monkeypatch.setattr(settings, 'directory', lambda: Path(os.environ.get('PICO_CONFIG_DIR', str(isolated))))
    yield
    os.environ.clear()
    os.environ.update(before)
