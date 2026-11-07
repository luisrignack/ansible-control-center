"""Gestión segura de imágenes de tapiz subidas por el operador.

La imagen se valida por su firma binaria (no por el nombre ni por el
``Content-Type`` que envía el navegador), se guarda fuera de la raíz web con
permisos ``0600`` y se identifica con un UUID. El playbook la copia desde esa
ruta local del controlador al escritorio de cada host.

Formatos admitidos: PNG, JPEG, WebP, GIF y BMP. Son formatos raster que GNOME
puede usar como fondo; no se admite SVG porque puede contener scripts.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from config import DATA_DIR, MAX_CONTENT_LENGTH

WALLPAPER_SUBDIR = "wallpapers"
REMOTE_WALLPAPER_DIR = "/usr/local/share/backgrounds"
REMOTE_WALLPAPER_BASENAME = "acc-tapiz"

# Firma binaria -> (extensión, MIME). El orden no importa salvo WebP, que
# necesita comprobar además el identificador en el offset 8.
_SIGNATURES: Tuple[Tuple[bytes, str, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
    (b"BM", "bmp", "image/bmp"),
)

ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "image/gif",
    "image/bmp",
}


def wallpaper_dir(data_dir: Optional[Path] = None) -> Path:
    """Directorio privado donde viven las imágenes subidas."""
    root = Path(data_dir) if data_dir is not None else Path(DATA_DIR)
    return root / WALLPAPER_SUBDIR


def detect_image_type(raw: bytes) -> Optional[Tuple[str, str]]:
    """Devuelve ``(extensión, mime)`` según la firma real del archivo."""
    if not raw:
        return None
    for signature, extension, mime in _SIGNATURES:
        if raw.startswith(signature):
            return extension, mime
    if len(raw) >= 12 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp", "image/webp"
    return None


def safe_display_name(filename: str) -> str:
    """Nombre original saneado, solo para mostrar en la interfaz."""
    name = Path(filename or "").name.strip()
    name = "".join(ch for ch in name if ch.isprintable() and ch not in "\r\n")
    return name[:160] or "tapiz"


def remote_wallpaper_path(extension: str) -> str:
    ext = extension.lstrip(".").lower() or "png"
    return f"{REMOTE_WALLPAPER_DIR}/{REMOTE_WALLPAPER_BASENAME}.{ext}"


def remote_wallpaper_uri(extension: str) -> str:
    return f"file://{remote_wallpaper_path(extension)}"


def prune_wallpapers(max_age_hours: int = 24, data_dir: Optional[Path] = None) -> None:
    """Elimina restos de operaciones antiguas que no llegaron a limpiarse.

    Es una salvaguarda para caídas del proceso: el flujo normal borra la
    imagen al terminar el lote. No toca archivos recientes.
    """
    directory = wallpaper_dir(data_dir)
    if not directory.is_dir():
        return
    cutoff = time.time() - max(1, int(max_age_hours)) * 3600
    for entry in directory.iterdir():
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            # Un archivo que otro proceso está usando se reintenta en la
            # próxima limpieza; no debe tumbar la subida actual.
            continue


def save_wallpaper(
    upload: Any,
    data_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Valida y guarda una imagen subida.

    Devuelve un diccionario con ``path``, ``name``, ``mime``, ``ext`` y
    ``size``. Lanza ``ValueError`` con un mensaje apto para el usuario si el
    archivo está vacío, supera el límite o no es una imagen admitida.
    """
    raw = upload.read(MAX_CONTENT_LENGTH + 1) if upload is not None else b""
    if not raw:
        raise ValueError("Selecciona una imagen para el tapiz.")
    if len(raw) > MAX_CONTENT_LENGTH:
        megabytes = MAX_CONTENT_LENGTH / (1024 * 1024)
        raise ValueError(
            f"La imagen supera el máximo permitido ({megabytes:.1f} MB). "
            "Reduce su tamaño o ajusta ACC_MAX_UPLOAD_BYTES."
        )

    detected = detect_image_type(raw)
    if detected is None:
        raise ValueError(
            "El archivo no es una imagen admitida. Usa PNG, JPEG, WebP, GIF "
            "o BMP."
        )
    extension, mime = detected

    directory = wallpaper_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        directory.chmod(0o700)
    except OSError:
        pass
    prune_wallpapers(data_dir=data_dir)

    destination = directory / f"{uuid.uuid4().hex}.{extension}"
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)
    except Exception:
        try:
            destination.unlink()
        except OSError:
            pass
        raise

    return {
        "path": str(destination),
        "name": safe_display_name(getattr(upload, "filename", "")),
        "mime": mime,
        "ext": extension,
        "size": len(raw),
    }


def remove_wallpaper(path: Optional[str], data_dir: Optional[Path] = None) -> None:
    """Borra una imagen temporal si está dentro del directorio de tapices."""
    if not path:
        return
    directory = wallpaper_dir(data_dir).resolve(strict=False)
    try:
        candidate = Path(path).resolve(strict=False)
    except OSError:
        return
    if candidate.parent != directory:
        return
    try:
        candidate.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
