"""Abre Vortexia Prospector como una aplicación de escritorio: sin ventana negra y en su propia ventana.

Lo usan los accesos directos del menú Inicio y del Escritorio (`pythonw -m app.desktop`):
1. Arranca la página local (el mismo servidor que `serve`), sin consola.
2. La muestra en una ventana propia: Microsoft Edge (o Chrome) en modo aplicación, sin barra
   de direcciones ni pestañas.
3. Al cerrar esa ventana, se cierra el programa.
Si el programa ya estaba abierto, solo abre otra ventana. Si algo falla, avisa con un cuadro de
mensaje de Windows (no hay consola donde mostrarlo) y deja el detalle en el registro del día.
"""

import argparse
import ctypes
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path

PORT = 8000
URL = f"http://127.0.0.1:{PORT}/"
TITLE = "Vortexia Prospector"

_BROWSERS = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
)


def _use_port(port: int) -> None:
    global PORT, URL
    PORT, URL = port, f"http://127.0.0.1:{port}/"


def _quiet_streams(data_dir: Path) -> None:
    """Sin consola (pythonw), lo que se escribiría en pantalla va a un archivo en data/logs."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115
    if sys.stderr is None:
        folder = data_dir / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        sys.stderr = open(folder / "consola.log", "a", encoding="utf-8")  # noqa: SIM115


def message(text: str, *, error: bool = False) -> None:
    """Cuadro de mensaje de Windows (el programa no tiene consola donde mostrar avisos)."""
    try:
        ctypes.windll.user32.MessageBoxW(None, text, TITLE, 0x10 if error else 0x40)
    except (AttributeError, OSError):
        print(text, file=sys.stderr)


def port_in_use(port: int | None = None) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port or PORT)) == 0


def find_browser() -> str | None:
    """Edge viene con Windows 10 y 11; si no está, se prueba Chrome."""
    found = shutil.which("msedge")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA", "")
    candidates = [*_BROWSERS, os.path.join(local, r"Google\Chrome\Application\chrome.exe")]
    return next((path for path in candidates if path and os.path.exists(path)), None)


def open_window(browser: str | None, profile: Path) -> subprocess.Popen | None:
    """Abre la ventana del programa. Devuelve el proceso para saber cuándo se cierra (None si no se puede)."""
    if browser is None:
        webbrowser.open(URL)
        return None
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            browser,
            f"--app={URL}",
            # Perfil propio: la ventana es un proceso aparte y se sabe cuándo la cierras.
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1400,900",
        ]
    )


def wait_until_ready(timeout: float = 60) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(URL, timeout=2) as response:  # noqa: S310 (solo localhost)
                if response.status == 200:
                    return True
        except OSError:
            time.sleep(0.3)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.desktop", description=TITLE)
    parser.add_argument("--sin-ventana", action="store_true", help="Solo arranca la página (para pruebas)")
    parser.add_argument("--puerto", type=int, default=PORT, help="Puerto local (por defecto 8000)")
    args = parser.parse_args(argv)
    _use_port(args.puerto)

    from app.config import get_settings
    from app.logs import log, setup_logging

    settings = get_settings()
    _quiet_streams(settings.data_dir)
    setup_logging(settings.data_dir)
    browser = find_browser()
    profile = settings.data_dir / "ventana"

    if port_in_use():
        # Ya está abierto (o el puerto lo usa otro programa): se abre otra ventana y listo.
        log.info("El programa ya estaba abierto: se abre otra ventana")
        if not args.sin_ventana:
            open_window(browser, profile)
        return 0

    import uvicorn

    from app.pipeline import friendly_error
    from app.web.app import create_app

    try:
        server = uvicorn.Server(
            uvicorn.Config(create_app(settings), host="127.0.0.1", port=PORT, log_level="warning", log_config=None)
        )
    except Exception as exc:  # por ejemplo, la base de datos no se pudo abrir
        log.exception("No se pudo preparar el programa")
        message(f"Vortexia Prospector no pudo abrirse:\n\n{friendly_error(exc)}", error=True)
        return 1

    def watch_window() -> None:
        if not wait_until_ready():
            message("Vortexia Prospector tardó demasiado en abrirse. Cierra y vuelve a intentarlo.", error=True)
            server.should_exit = True
            return
        if args.sin_ventana:
            return
        window = open_window(browser, profile)
        if window is None:
            return  # se abrió en el navegador normal: el programa sigue abierto hasta apagar el computador
        window.wait()
        log.info("Se cerró la ventana del programa: se cierra Vortexia Prospector")
        server.should_exit = True

    log.info("Vortexia Prospector abierto en %s (ventana: %s)", URL, browser or "navegador predeterminado")
    threading.Thread(target=watch_window, daemon=True).start()
    try:
        server.run()
    except BaseException as exc:  # uvicorn sale con SystemExit si no puede usar el puerto
        log.exception("El programa se cerró por un error")
        message(f"Vortexia Prospector se cerró por un error:\n\n{friendly_error(exc)}", error=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
