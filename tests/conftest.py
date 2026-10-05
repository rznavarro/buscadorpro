"""Ajustes comunes de los tests."""

import pytest


@pytest.fixture(autouse=True)
def _no_retry_pause(monkeypatch):
    # Los reintentos se prueban igual, pero sin esperar 3 segundos entre intentos.
    monkeypatch.setenv("WEB_RETRY_PAUSE_SECONDS", "0")
