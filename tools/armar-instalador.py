"""Fabrica descargas/VortexiaProspector-Setup.exe (el instalador de Windows) con Inno Setup 6.

Uso: uv run python tools/armar-instalador.py
Toma los archivos del programa que están en git (o por agregarse), sin lo que es solo para
desarrollo (pruebas, herramientas, notas), y compila installer/VortexiaProspector.iss.
Nunca incluye la carpeta data con datos (solo la lista de comunas) ni el archivo .env.
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "build" / "VortexiaProspector"
SCRIPT = ROOT / "installer" / "VortexiaProspector.iss"
# Igual que export-ignore en .gitattributes: no van en la descarga.
DEV_ONLY = ("tests/", "tools/", "descargas/", "installer/", "build/", "CLAUDE.md", ".gitattributes")


def program_files() -> list[str]:
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout.splitlines()
    files = [f for f in listed if f and not f.startswith(DEV_ONLY) and "__pycache__" not in f]
    leaks = [f for f in files if f.startswith("data/") and f != "data/comunas.json" or f == ".env"]
    if leaks:
        sys.exit(f"Se detuvo: estos archivos con datos no deben ir en el instalador: {leaks}")
    return files


def iscc() -> Path:
    candidates = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe",
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ]
    for path in candidates:
        if path.exists():
            return path
    sys.exit("Falta Inno Setup 6. Instálalo con: winget install --id JRSoftware.InnoSetup -e --scope user")


def version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    return match.group(1) if match else "1.0.0"


def main() -> None:
    if BUILD.exists():
        shutil.rmtree(BUILD)
    files = program_files()
    for relative in files:
        target = BUILD / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / relative, target)
    print(f"{len(files)} archivos del programa copiados a {BUILD}")
    subprocess.run(
        [str(iscc()), f"/DAppVersion={version()}", f"/DSourceDir={BUILD}", "/Q", str(SCRIPT)],
        cwd=SCRIPT.parent, check=True,
    )
    setup = ROOT / "descargas" / "VortexiaProspector-Setup.exe"
    print(f"Listo: {setup} ({setup.stat().st_size // 1024} KB, versión {version()})")


if __name__ == "__main__":
    main()
