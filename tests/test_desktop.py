"""Lanzador de escritorio (app/desktop.py): ventana propia, sin consola, una sola instancia."""

import socket

import pytest

from app import desktop


class FakePopen:
    calls: list[list[str]] = []

    def __init__(self, args):
        FakePopen.calls.append(args)


@pytest.fixture(autouse=True)
def _reset():
    FakePopen.calls = []


def test_the_window_opens_as_an_app_with_its_own_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop.subprocess, "Popen", FakePopen)
    desktop.open_window(r"C:\Edge\msedge.exe", tmp_path / "ventana")
    args = FakePopen.calls[0]
    assert args[0] == r"C:\Edge\msedge.exe"
    assert "--app=http://127.0.0.1:8000/" in args  # sin barra de direcciones ni pestañas
    assert f"--user-data-dir={tmp_path / 'ventana'}" in args  # proceso propio: se sabe cuándo se cierra
    assert (tmp_path / "ventana").is_dir()


def test_without_edge_or_chrome_it_uses_the_default_browser(monkeypatch, tmp_path):
    opened: list[str] = []
    monkeypatch.setattr(desktop.webbrowser, "open", opened.append)
    assert desktop.open_window(None, tmp_path / "ventana") is None
    assert opened == ["http://127.0.0.1:8000/"]


def test_find_browser_prefers_edge(monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda name: r"C:\Edge\msedge.exe" if name == "msedge" else None)
    assert desktop.find_browser() == r"C:\Edge\msedge.exe"


def test_find_browser_falls_back_to_known_paths(monkeypatch):
    monkeypatch.setattr(desktop.shutil, "which", lambda name: None)
    monkeypatch.setattr(desktop.os.path, "exists", lambda path: path.endswith("chrome.exe"))
    assert desktop.find_browser().endswith("chrome.exe")


def test_opening_it_twice_only_opens_another_window(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(desktop, "URL", desktop.URL)  # main() cambia el puerto: se restaura al terminar
    monkeypatch.setattr(desktop, "find_browser", lambda: r"C:\Edge\msedge.exe")
    monkeypatch.setattr(desktop.subprocess, "Popen", FakePopen)
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        monkeypatch.setattr(desktop, "PORT", busy.getsockname()[1])
        monkeypatch.setattr(desktop, "port_in_use", lambda port=None: True)
        assert desktop.main([]) == 0  # no arranca un segundo servidor
    assert len(FakePopen.calls) == 1
    get_settings.cache_clear()
    from app.logs import log

    for handler in list(log.handlers):
        log.removeHandler(handler)
        handler.close()
