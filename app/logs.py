"""Registro legible de lo que hace el programa, un archivo por día en `data/logs/`.

En la página se ven los mensajes simples; aquí quedan también los detalles técnicos de
cada error, para revisarlos después sin tener que repetir la búsqueda. Se guardan los
registros de los últimos 30 días.
"""

import logging
from datetime import date, timedelta
from pathlib import Path

LOGGER_NAME = "prospector"
log = logging.getLogger(LOGGER_NAME)
KEEP_DAYS = 30


def log_dir(data_dir: Path) -> Path:
    return data_dir / "logs"


def log_file(data_dir: Path, day: date | None = None) -> Path:
    return log_dir(data_dir) / f"prospector-{(day or date.today()).isoformat()}.log"


class _DailyFile(logging.FileHandler):
    """Escribe en prospector-AAAA-MM-DD.log y cambia de archivo al cambiar el día."""

    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir
        self.day = date.today()
        super().__init__(log_file(data_dir), encoding="utf-8", delay=True)

    def emit(self, record: logging.LogRecord) -> None:
        if date.today() != self.day:
            self.day = date.today()
            self.close()  # el próximo mensaje abre el archivo del día nuevo
            self.baseFilename = str(log_file(self.data_dir))
            remove_old_logs(self.data_dir)
        super().emit(record)


def remove_old_logs(data_dir: Path, today: date | None = None) -> None:
    oldest = (today or date.today()) - timedelta(days=KEEP_DAYS)
    for path in log_dir(data_dir).glob("prospector-*.log"):
        try:
            day = date.fromisoformat(path.stem.removeprefix("prospector-"))
        except ValueError:
            continue
        if day < oldest:
            path.unlink(missing_ok=True)


def setup_logging(data_dir: Path) -> Path:
    """Activa el registro en archivo (una sola vez). Devuelve la carpeta de los registros."""
    folder = log_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    remove_old_logs(data_dir)
    for handler in list(log.handlers):
        if isinstance(handler, _DailyFile):
            log.removeHandler(handler)
            handler.close()
    handler = _DailyFile(data_dir)
    handler.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-7s | %(message)s", "%Y-%m-%d %H:%M:%S"))
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    return folder


def read_today(data_dir: Path, max_lines: int = 400) -> list[str]:
    """Últimas líneas del registro de hoy, para verlas en la página."""
    path = log_file(data_dir)
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8", errors="replace").splitlines()[-max_lines:]
