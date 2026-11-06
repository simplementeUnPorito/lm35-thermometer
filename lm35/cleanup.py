"""Borrado acotado de datos generados por la aplicación."""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path


class CleanupError(RuntimeError):
    pass


RECORD_DIRS = (
    "sesiones",
    "demo_verificacion",
    "verificacion",
    "verificacion_gui",
    "validacion_hardware_sin_sensores",
)
CALIBRATION_FILES = (
    "calibracion.json",
    "calibracion_history.jsonl",
    "DEMO_SIMULADO_calibracion.json",
    "DEMO_SIMULADO_calibracion_history.jsonl",
    "calibracion_recuperada.json",
    "calibracion_recuperada_history.jsonl",
)
FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def _is_link_or_reparse(path: Path) -> bool:
    info = os.lstat(path)
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
    )


def _validated_root(data_root, project_root=None) -> Path:
    raw = Path(data_root)
    if not raw.is_absolute():
        raise CleanupError("La carpeta de datos debe ser una ruta absoluta.")
    root = Path(os.path.abspath(raw))
    forbidden = {Path(root.anchor), Path.home(), Path.home().parent}
    if project_root is not None:
        forbidden.add(Path(os.path.abspath(project_root)))
    windows_dir = os.environ.get("WINDIR")
    if windows_dir:
        forbidden.add(Path(os.path.abspath(windows_dir)))
    if not str(root).strip() or any(os.path.normcase(root) == os.path.normcase(p) for p in forbidden):
        raise CleanupError("Ruta de datos insegura o demasiado amplia.")
    if not root.exists() or not root.is_dir():
        raise CleanupError("La carpeta de datos no existe o no es un directorio.")
    if _is_link_or_reparse(root):
        raise CleanupError("La carpeta de datos no puede ser un enlace ni junction.")
    return root


def _preflight(path: Path) -> None:
    if _is_link_or_reparse(path):
        raise CleanupError(f"Se rechazó un enlace o junction: {path.name}")
    if path.is_dir():
        for child in path.iterdir():
            _preflight(child)


def _delete(path: Path, root: Path) -> None:
    # commonpath is checked immediately before deletion as a final containment guard.
    if os.path.commonpath((str(root), str(path))) != str(root) or path == root:
        raise CleanupError("Se rechazó un destino fuera de la carpeta de datos.")
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def cleanup_data(data_root, scope: str, project_root=None) -> list[str]:
    """Delete a preflighted scope and return root-relative names removed."""
    root = _validated_root(data_root, project_root)
    if scope == "records":
        targets = [root / name for name in RECORD_DIRS if (root / name).exists()]
    elif scope == "calibrations":
        names = [name for base in CALIBRATION_FILES for name in (base, base + ".tmp")]
        targets = [root / name for name in names if (root / name).exists()]
    elif scope == "all":
        targets = list(root.iterdir())
    else:
        raise CleanupError(f"Alcance de limpieza desconocido: {scope}")
    for target in targets:
        _preflight(target)
    removed = [target.name for target in targets]
    for target in targets:
        _delete(target, root)
    return removed
