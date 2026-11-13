# services/pdf_report.py
# Generador de informes PDF de ejecución, sin dependencias externas.
#
# El proyecto puede desplegarse en redes aisladas (sin acceso a pip), así que
# no se usan librerías como reportlab: se construye el PDF a mano con la
# estructura mínima del formato (objetos, fuentes base Helvetica y
# WinAnsiEncoding, que cubren acentos y ñ del español). Los flujos de
# contenido van sin comprimir para poder inspeccionarlos y para que las
# pruebas puedan verificar el texto del informe.
from __future__ import annotations

import io
import zlib
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Clasificación de motivos
# ---------------------------------------------------------------------------
# Pares (patrón case-insensitive, motivo breve). El orden importa: lo más
# específico primero; el resto se reporta con el texto del error truncado.
REASON_PATTERNS: tuple[tuple[str, str], ...] = (
    # Conexión: estación apagada, fuera de red, puerto cerrado.
    ("unreachable", "Estación apagada o sin conexión"),
    ("timed out", "Estación apagada o sin conexión"),
    ("no route to host", "Estación apagada o sin conexión"),
    ("host unreachable", "Estación apagada o sin conexión"),
    ("connection refused", "Servicio de conexión no disponible"),
    ("connection reset", "Conexión interrumpida"),
    ("temporarily unavailable", "Estación ocupada o sin conexión"),
    # Tiempos agotados (después de conexión).
    ("timeout", "Tiempo de conexión agotado"),
    # Identidad del host.
    ("failed to add the host", "No se autenticó la estación"),
    ("host key verification failed", "Huella del host no reconocida"),
    ("host key", "Huella del host no reconocida"),
    # Credenciales.
    ("permission denied", "Credenciales rechazadas"),
    ("incorrect password", "Contraseña incorrecta"),
    ("authentication failed", "Contraseña rechazada"),
    ("password authentication failed", "Contraseña rechazada"),
    ("credentials were rejected", "Credenciales rechazadas por WinRM"),
    ("incorrect sudo password", "Contraseña de sudo incorrecta"),
    ("sudo", "Contraseña de sudo insuficiente"),
    ("no se proporcionó contraseña", "No se envió contraseña"),
    ("without password", "No se envió contraseña"),
    # Operación y playbook.
    ("operación cancelada", "Operación cancelada"),
    ("operacion cancelada", "Operación cancelada"),
    ("tapiz", "No se pudo aplicar el tapiz"),
    ("playbook", "El playbook falló en la estación"),
    ("hora_errors", "La verificación no confirmó el cambio"),
    ("zona final", "La verificación no confirmó el cambio"),
    ("no se pudo", "Fallo interno del servidor"),
)

_MESES = (
    "enero",
    "febrero",
    "marzo",
    "abril",
    "mayo",
    "junio",
    "julio",
    "agosto",
    "septiembre",
    "octubre",
    "noviembre",
    "diciembre",
)


def format_spanish_datetime(value: datetime | None, with_time: bool = True) -> str:
    """Fecha/hora legible en español, en la zona local del servidor."""
    if value is None:
        return "—"
    local = value if value.tzinfo is None else value.astimezone()
    base = f"{local.day} de {_MESES[local.month - 1]} de {local.year}"
    if with_time:
        base += f", {local.hour:02d}:{local.minute:02d}"
    return base


def format_station_timestamp(value: datetime | None) -> str:
    """Fecha y hora compacta de una estación (columna del informe)."""
    if value is None:
        return "—"
    local = value if value.tzinfo is None else value.astimezone()
    return local.strftime("%d/%m/%Y %H:%M")


def classify_failure_reason(error: str, result: str = "") -> str:
    """Devuelve un motivo breve y legible para un resultado fallido."""
    text = (error or "").strip()
    if not text:
        if result == "skipped":
            return "Omitida en esta corrida"
        return "Motivo desconocido"
    low = text.lower()
    for pattern, reason in REASON_PATTERNS:
        if pattern.lower() in low:
            return reason
    # Los avisos de Ansible (deprecations, config) no son el motivo real: se
    # descartan y se usa la primera línea técnica útil del error.
    meaningful = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
        and not line.strip().startswith(("[WARNING]", "[DEPRECATION WARNING]"))
        and "deprecation" not in line.lower()
    ]
    if not meaningful:
        return "Motivo desconocido (detalle técnico en el historial)"
    first_line = meaningful[0]
    if len(first_line) > 140:
        first_line = first_line[:137].rstrip() + "…"
    return first_line


# ---------------------------------------------------------------------------
# PDF mínimo (sin dependencias)
# ---------------------------------------------------------------------------
PAGE_W = 595.28  # A4 en puntos
PAGE_H = 841.89
MARGIN = 54
USABLE_W = PAGE_W - 2 * MARGIN
FOOTER_Y = 40

BRAND = "Ansible Control Center"
ORG = "Grupo de Migración a Código Abierto / DCTI - ETECSA"

# Icono azul institucional que acompaña a todos los informes.
LOGO_PATH = (
    Path(__file__).resolve().parent.parent / "static" / "img" / "logo-azul.png"
)
LOGO_SIZE = 26.0

# Estimación de ancho medio de Helvetica ≈ 0.52 × tamaño de fuente.
_CHARS_PER_POINT = 0.52


def _max_chars(col_width: float, font_size: float) -> int:
    return max(6, int(col_width / (font_size * _CHARS_PER_POINT)))


def _wrap_text(text: str, max_chars: int) -> list[str]:
    """Corte de línea simple por anchura estimada."""
    output: list[str] = []
    for raw in (text or "").splitlines() or [""]:
        raw = raw.strip()
        if not raw:
            output.append("")
            continue
        current = ""
        for word in raw.split():
            candidate = f"{current} {word}".strip()
            if len(candidate) <= max_chars:
                current = candidate
                continue
            if current:
                output.append(current)
            while len(word) > max_chars:
                output.append(word[: max_chars - 1] + "-")
                word = word[max_chars - 1 :]
            current = word
        output.append(current)
    return output


class _ReportImage:
    """Imagen RGB con transparencia opcional lista para un XObject PDF."""

    def __init__(
        self,
        width: int,
        height: int,
        rgb: bytes,
        alpha: bytes | None = None,
    ) -> None:
        self.width = width
        self.height = height
        self._rgb = zlib.compress(rgb, 9)
        self._alpha = zlib.compress(alpha, 9) if alpha is not None else None

    @property
    def has_alpha(self) -> bool:
        return self._alpha is not None

    def rgb_stream(self, smask_id: int | None = None) -> bytes:
        smask = f" /SMask {smask_id} 0 R" if smask_id is not None else ""
        header = (
            f"<< /Type /XObject /Subtype /Image /Width {self.width} "
            f"/Height {self.height} /ColorSpace /DeviceRGB "
            f"/BitsPerComponent 8 /Filter /FlateDecode{smask} "
            f"/Length {len(self._rgb)} >>\nstream\n"
        ).encode()
        return header + self._rgb + b"\nendstream"

    def alpha_stream(self) -> bytes:
        if self._alpha is None:  # pragma: no cover - solo si se pide sin canal alfa
            raise ValueError("La imagen no tiene canal alfa")
        header = (
            f"<< /Type /XObject /Subtype /Image /Width {self.width} "
            f"/Height {self.height} /ColorSpace /DeviceGray "
            f"/BitsPerComponent 8 /Filter /FlateDecode "
            f"/Length {len(self._alpha)} >>\nstream\n"
        ).encode()
        return header + self._alpha + b"\nendstream"


def _png_chunks(data: bytes) -> Iterator[tuple[bytes, bytes]]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Firma PNG no válida")
    position = 8
    while position + 12 <= len(data):
        length = int.from_bytes(data[position : position + 4], "big")
        kind = data[position + 4 : position + 8]
        payload = data[position + 8 : position + 8 + length]
        yield kind, payload
        position += 12 + length


def _paeth(left: int, up: int, up_left: int) -> int:
    estimate = left + up - up_left
    d_left = abs(estimate - left)
    d_up = abs(estimate - up)
    d_up_left = abs(estimate - up_left)
    if d_left <= d_up and d_left <= d_up_left:
        return left
    if d_up <= d_up_left:
        return up
    return up_left


def _png_unfilter(raw: bytes, width: int, height: int, bpp: int) -> bytes:
    """Revierte los filtros por línea del PNG (8 bits, sin entrelazado)."""
    stride = width * bpp
    output = bytearray()
    previous = bytearray(stride)
    position = 0
    for _ in range(height):
        filter_type = raw[position]
        position += 1
        line = bytearray(raw[position : position + stride])
        position += stride
        if filter_type == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif filter_type == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif filter_type == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif filter_type == 4:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                up = previous[i]
                up_left = previous[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + _paeth(left, up, up_left)) & 0xFF
        elif filter_type != 0:
            raise ValueError(f"Filtro PNG no soportado: {filter_type}")
        output.extend(line)
        previous = line
    return bytes(output)


def _decode_logo(data: bytes) -> _ReportImage:
    """Decodifica el PNG del logotipo a RGB y, si existe, canal alfa."""
    width = height = 0
    bit_depth = color_type = interlace = -1
    compressed = bytearray()
    for kind, payload in _png_chunks(data):
        if kind == b"IHDR":
            width = int.from_bytes(payload[0:4], "big")
            height = int.from_bytes(payload[4:8], "big")
            bit_depth = payload[8]
            color_type = payload[9]
            interlace = payload[12]
        elif kind == b"IDAT":
            compressed.extend(payload)
        elif kind == b"IEND":
            break
    channels = {0: 1, 2: 3, 4: 2, 6: 4}.get(color_type)
    if not width or not height or channels is None:
        raise ValueError("PNG sin cabecera válida o tipo de color no soportado")
    if bit_depth != 8 or interlace != 0:
        raise ValueError("Solo se admiten PNG de 8 bits sin entrelazado")
    pixels = _png_unfilter(
        zlib.decompress(bytes(compressed)), width, height, channels
    )

    if color_type == 6:
        rgb = bytearray(width * height * 3)
        alpha = bytearray(width * height)
        for index in range(width * height):
            source = index * 4
            target = index * 3
            rgb[target : target + 3] = pixels[source : source + 3]
            alpha[index] = pixels[source + 3]
        return _ReportImage(width, height, bytes(rgb), bytes(alpha))
    if color_type == 2:
        return _ReportImage(width, height, pixels)
    if color_type == 4:
        rgb = bytearray(width * height * 3)
        alpha = bytearray(width * height)
        for index in range(width * height):
            gray = pixels[index * 2]
            target = index * 3
            rgb[target : target + 3] = bytes((gray, gray, gray))
            alpha[index] = pixels[index * 2 + 1]
        return _ReportImage(width, height, bytes(rgb), bytes(alpha))
    # Escala de grises opaca (color_type == 0).
    rgb = bytearray(width * height * 3)
    for index, gray in enumerate(pixels):
        target = index * 3
        rgb[target : target + 3] = bytes((gray, gray, gray))
    return _ReportImage(width, height, bytes(rgb))


@lru_cache(maxsize=1)
def _load_logo() -> _ReportImage | None:
    """Carga el icono azul una sola vez; si falta, el informe sigue saliendo."""
    try:
        return _decode_logo(LOGO_PATH.read_bytes())
    except (OSError, ValueError, zlib.error):
        return None


class _ContentBuilder:
    """Construye un stream de contenido PDF (texto y gráficos vectoriales)."""

    def __init__(self) -> None:
        self.parts: list[str] = []

    def fill_rect(
        self, x: float, y: float, w: float, h: float, color: tuple[float, ...]
    ) -> None:
        r, g, b = (round(float(c), 3) for c in color)
        self.parts.append(f"{r} {g} {b} rg")
        self.parts.append(f"{x:.2f} {y:.2f} {w:.2f} {h:.2f} re f")

    def text(
        self,
        x: float,
        y: float,
        text: str,
        size: float = 10,
        bold: bool = False,
        color: tuple[float, ...] = (0, 0, 0),
    ) -> None:
        font = "/F2" if bold else "/F1"
        r, g, b = (round(float(c), 3) for c in color)
        # Solo se escapan los caracteres especiales del PDF; la conversión a
        # WinAnsi (cp1252) se hace una sola vez al guardar el stream.
        safe = (text or "").replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        self.parts.append(
            f"BT {r} {g} {b} rg {font} {size:.1f} Tf "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm ({safe}) Tj ET"
        )

    def build(self) -> str:
        return "\n".join(self.parts)


class PDFDocument:
    """PDF de varias páginas con las fuentes base F1/F2 (sin colecciones)."""

    def __init__(self) -> None:
        self.pages: list[_ContentBuilder] = [_ContentBuilder()]

    @property
    def content(self) -> _ContentBuilder:
        return self.pages[-1]

    def add_page(self) -> None:
        self.pages.append(_ContentBuilder())

    def render(self) -> bytes:
        buf = io.BytesIO()
        buf.write(b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n")

        n_pages = len(self.pages)
        # Objetos: 1 catálogo, 2 árbol de páginas, 3-4 fuentes y, si está
        # disponible, 5-6 imagen del logotipo (RGB + máscara alfa). Después
        # (contenido, página) por cada página.
        objects: list[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"",  # se completa al conocer los identificadores de página
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            + b"/Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            + b"/Encoding /WinAnsiEncoding >>",
        ]

        logo = _load_logo()
        logo_id: int | None = None
        if logo is not None:
            logo_id = len(objects) + 1
            if logo.has_alpha:
                objects.append(logo.rgb_stream(logo_id + 1))
                objects.append(logo.alpha_stream())
            else:  # pragma: no cover - el logotipo actual tiene alfa
                objects.append(logo.rgb_stream())

        xobject = f"/XObject << /Logo {logo_id} 0 R >> " if logo_id else ""
        logo_ops = ""
        if logo is not None:
            height = LOGO_SIZE * logo.height / logo.width
            logo_ops = (
                f"q {LOGO_SIZE:.2f} 0 0 {height:.2f} "
                f"{PAGE_W - MARGIN - LOGO_SIZE:.2f} "
                f"{PAGE_H - 10 - height:.2f} cm /Logo Do Q\n"
            )

        page_ids: list[int] = []
        for content in self.pages:
            stream = (logo_ops + content.build()).encode("cp1252", errors="replace")
            objects.append(
                f"<< /Length {len(stream)} >>\nstream\n".encode()
                + stream
                + b"\nendstream"
            )
            content_id = len(objects)
            page_id = content_id + 1
            page_ids.append(page_id)
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R "
                    f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> "
                    f"{xobject}>> "
                    f"/Contents {content_id} 0 R >>"
                ).encode()
            )

        objects[1] = (
            f"<< /Type /Pages /Count {n_pages} /Kids ["
            + " ".join(f"{page_id} 0 R" for page_id in page_ids)
            + "] >>"
        ).encode()

        offsets: list[int] = []
        for i, obj in enumerate(objects, start=1):
            offsets.append(buf.tell())
            buf.write(f"{i} 0 obj\n".encode())
            buf.write(obj)
            buf.write(b"\nendobj\n")

        xref_offset = buf.tell()
        buf.write(f"xref\n0 {len(objects) + 1}\n".encode())
        buf.write(b"0000000000 65535 f \n")
        for off in offsets:
            buf.write(f"{off:010d} 00000 n \n".encode())
        buf.write(
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref_offset}\n%%EOF\n".encode()
        )
        return buf.getvalue()


# ---------------------------------------------------------------------------
# Arma el informe
# ---------------------------------------------------------------------------
COLS_RATIO = (0.22, 0.20, 0.13, 0.45)
FOOTER_LEFT = ORG

DARK = (0.15, 0.2, 0.32)
ZEBRA = (0.95, 0.96, 0.97)
RED = (0.72, 0.13, 0.13)
GREEN = (0.05, 0.45, 0.25)
GRAY = (0.45, 0.45, 0.45)
LIGHT_LINE = (0.8, 0.82, 0.85)

RESULT_LABELS = {
    "success": "Correcta",
    "failure": "Fallida",
    "skipped": "Omitida",
    "error": "Con error",
    "preview": "Vista previa",
    "attention": "Requiere atención",
}

RESULT_COLORS = {
    "success": GREEN,
    "failure": RED,
    "error": RED,
    "skipped": GRAY,
    "preview": (0.5, 0.3, 0.05),
    "attention": (0.65, 0.42, 0.05),
}

STATUS_LABELS = {
    "success": "CORRECTA — todas las estaciones ajustadas",
    "partial": "PARCIAL — hubo estaciones sin ajustar",
    "failure": "FALLIDA — ninguna estación se ajustó",
    "error": "ERROR — la operación no se completó",
    "preview": "VISTA PREVIA — solo inspección, sin cambios",
    "running": "EN CURSO",
    "incomplete": "INCOMPLETA — se perdió contacto con el servidor",
    "cancelled": "CANCELADA por el operador",
}

_MODE_LABELS = {
    "utc": "Hora exacta UTC",
    "local": "Hora local de estación",
}

ACTION_LABELS = {
    "TZ": "Zona horaria",
    "TIME": "Hora exacta",
    "WALLPAPER": "Tapiz de escritorio",
    "APT": "Actualización segura del sistema",
    "PASSWORD": "Cambio de contraseña",
    "FIREWALL": "Configuración del firewall",
}


def _apt_state(row: Any) -> dict:
    state = getattr(row, "after_state", None)
    return state if isinstance(state, dict) else {}


def _attention_reason(row: Any) -> str:
    """Motivo legible de una estación que no se actualizó por política."""
    state = _apt_state(row)
    kind = str(state.get("attention_kind") or "")
    name = str(state.get("hostname") or "").strip()
    detail = (getattr(row, "error", "") or "").strip()
    if not detail:
        detail = str(state.get("attention_reason") or "").strip()
    if kind == "migration":
        base = "Requiere migración de distribución"
    elif kind == "space":
        base = "Espacio insuficiente para actualizar"
    else:
        base = "Requiere atención antes de actualizar"
    parts = [base]
    if name:
        parts.append(f"equipo {name}")
    if detail:
        parts.append(detail)
    return " · ".join(parts)


def _migration_rows(host_results: Sequence[Any]) -> list[Any]:
    """Estaciones del lote que necesitan migración de distribución."""
    rows = []
    for row in host_results or []:
        state = _apt_state(row)
        kind = str(state.get("attention_kind") or "")
        if kind == "migration" or (
            getattr(row, "result", "") == "attention" and state.get("blocked")
        ):
            rows.append(row)
    return rows


def _batch_mode_text(batch: Any) -> str:
    if batch.dry_run:
        return "Simulada (DRY RUN, sin cambios)"
    if batch.status == "preview":
        return "Vista previa (solo inspección)"
    return "Ejecución real"


def build_execution_report(
    batch: Any,
    host_results: Sequence[Any] | None,
    task_name: str = "",
    generated_at: datetime | None = None,
) -> bytes:
    """Construye un informe PDF de un lote: resumen y estaciones fallidas.

    ``batch``/``host_results`` son los objetos de ``models.batch``. El informe
    se forma por capas: cabecera, datos de la corrida, tabla de estaciones
    (fallidas primero) y pie con paginación.
    """
    doc = PDFDocument()
    body = doc.content
    generated_at = generated_at or datetime.now(UTC)

    host_rows = list(host_results or [])
    failed_rows = [h for h in host_rows if h.result in {"failure", "error", "skipped"}]
    attention_rows = [h for h in host_rows if h.result == "attention"]
    ok_rows = [h for h in host_rows if h.result == "success"]
    ordered_rows = (
        failed_rows
        + attention_rows
        + [
            h
            for h in host_rows
            if h not in failed_rows
            and h not in attention_rows
            and h.result != "success"
        ]
        + ok_rows
    )

    status_text = STATUS_LABELS.get(batch.status, batch.status)

    # --- Cabecera (página 1) ---
    y = PAGE_H - MARGIN
    body.text(MARGIN, y, BRAND, size=17, bold=True, color=DARK)
    y -= 17
    body.text(MARGIN, y, "Informe de ejecución de tarea", size=13, bold=True)
    y -= 15
    body.text(MARGIN, y, ORG, size=8.5, color=GRAY)
    y -= 12
    body.fill_rect(MARGIN, y, USABLE_W, 1.2, DARK)
    y -= 22

    def line(key: str, value: str, bold_value: bool = False) -> None:
        nonlocal y
        body.text(MARGIN, y, key, size=9, bold=True)
        wrapped = _wrap_text(value, _max_chars(USABLE_W - 135, 9))
        for part in wrapped:
            body.text(MARGIN + 135, y, part, size=9, bold=bold_value)
            y -= 12.5

    def gap(h: float = 3.0) -> None:
        nonlocal y
        y -= h

    gap(3)
    line("Tarea", task_name or batch.task_id or "—")
    line(
        "Acción aplicada",
        f"{ACTION_LABELS.get(batch.action, batch.action)} = " f"{batch.value or '—'}",
    )
    line("Objetivo", batch.target or "—")
    line("Ordenada por", batch.operator or "—")
    line("Credenciales", f"{batch.admin_user or '—'} ({batch.password_origin or '—'})")
    line("Tipo de corrida", _batch_mode_text(batch))
    line("Inicio", format_spanish_datetime(batch.created_at))
    line("Fin", format_spanish_datetime(batch.finished_at))
    line(
        "Duración",
        f"{batch.duration_seconds} s" if batch.duration_seconds is not None else "—",
    )
    line(
        "Referencia",
        f"Lote {batch.batch_id[:12]} · corrida {batch.created_at.strftime('%Y%m%d')}",
    )
    line("Resultado", status_text, bold_value=True)
    gap(7)

    # Resumen de contadores.
    counts = (
        f"{batch.total_hosts} estaciones en el lote: "
        f"{batch.success_count} correctas · {batch.failure_count + batch.error_count} "
        f"fallidas · {batch.skipped_count} omitidas · {batch.preview_count} vistas previas · "
        f"{batch.attention_count} requieren atención"
    )
    body.fill_rect(MARGIN, y - 4, USABLE_W, 16, ZEBRA)
    body.text(MARGIN + 8, y + 5, counts, size=9.5, bold=True)
    y -= 26

    # --- Tabla de estaciones ---
    body.text(MARGIN, y, "Estaciones", size=11, bold=True, color=DARK)
    y -= 14

    col_widths = tuple(r * USABLE_W for r in COLS_RATIO)
    font_size = 8.5
    max_chars = tuple(_max_chars(w, font_size) for w in col_widths)
    padding = 6.0
    row_min_h = 15.0
    line_h = 11.5

    def table_header() -> None:
        nonlocal y
        table_body = doc.content  # siempre sobre la página activa
        head_y = y - row_min_h
        table_body.fill_rect(MARGIN, head_y, USABLE_W, row_min_h, DARK)
        x = float(MARGIN)
        for i, title in enumerate(("Estación", "Fecha y hora", "Estado", "Motivo")):
            table_body.text(
                x + padding, head_y + 5, title, size=9, bold=True, color=(1, 1, 1)
            )
            x += col_widths[i]
        y = head_y - 2

    def ensure_space(needed: float) -> None:
        nonlocal y
        if y - needed < MARGIN + 30:
            doc.add_page()
            new_body = doc.content
            new_body.text(
                MARGIN, PAGE_H - MARGIN + 6, BRAND, size=9, bold=True, color=DARK
            )
            new_body.fill_rect(MARGIN, PAGE_H - MARGIN + 8, USABLE_W, 0.8, DARK)
            # La nueva página vuelve a empezar bajo su cabecera; sin este
            # reinicio la tabla seguiría escribiendo al pie y generaría una
            # página casi vacía por cada fila.
            y = PAGE_H - MARGIN - 6
            # Reusar el builder nuevo para continuar el bucle.
            table_header()

    # Se sustituye el cuerpo activo cuando cambia la página.
    table_header()
    current_body = doc.content
    if not host_rows:
        ensure_space(row_min_h + 4)
        current_body = doc.content
        current_body.text(
            MARGIN,
            y - 10,
            "No hay estaciones registradas en esta corrida.",
            size=9,
            color=GRAY,
        )
        y -= 18
    else:
        for index, row in enumerate(ordered_rows, start=1):
            if row.result == "success":
                if getattr(batch, "dry_run", False):
                    reason = "Simulación: sin cambios aplicados"
                elif getattr(batch, "action", "") == "WALLPAPER":
                    reason = "Tapiz aplicado y verificado"
                else:
                    reason = "Ajuste aplicado y verificado"
            elif row.result == "attention":
                reason = _attention_reason(row)
            else:
                reason = classify_failure_reason(row.error, row.result)
            station = row.host
            estado = RESULT_LABELS.get(row.result, row.result)
            cells = (
                station,
                format_station_timestamp(getattr(row, "created_at", None)),
                estado,
                reason,
            )
            wrapped_cells = [_wrap_text(str(c), m) for c, m in zip(cells, max_chars)]
            n_lines = max(len(w) for w in wrapped_cells)
            row_h = max(row_min_h, n_lines * line_h + 2 * padding)
            ensure_space(row_h)
            current_body = doc.content
            if index % 2 == 0:
                current_body.fill_rect(MARGIN, y - row_h, USABLE_W, row_h, ZEBRA)
            # Separador fino.
            current_body.fill_rect(MARGIN, y - row_h, USABLE_W, 0.5, LIGHT_LINE)
            x = float(MARGIN)
            for i, parts in enumerate(wrapped_cells):
                ty = y - padding - line_h
                for j, part in enumerate(parts):
                    if i == 0:
                        current_body.text(
                            x + padding, ty, part, size=font_size, bold=True
                        )
                    elif i == 2:
                        color = RESULT_COLORS.get(row.result, (0, 0, 0))
                        current_body.text(
                            x + padding,
                            ty,
                            part,
                            size=font_size,
                            bold=row.result in {"failure", "error", "attention"},
                            color=color if j == 0 else (0, 0, 0),
                        )
                    else:
                        current_body.text(x + padding, ty, part, size=font_size)
                    ty -= line_h
                x += col_widths[i]
            y -= row_h

    # --- Pie de sección ---
    current_body = doc.content
    y -= 6
    current_body.fill_rect(MARGIN, y, USABLE_W, 0.6, LIGHT_LINE)
    y -= 14
    note = (
        "Motivos habituales: estación apagada o fuera de red, credenciales "
        "rechazadas (contraseña de administrador) o verificación no confirmada. "
        "Los detalles técnicos completos de cada estación están en el "
        f"historial de la operación (lote {batch.batch_id[:12]})."
    )
    for part in _wrap_text(note, _max_chars(USABLE_W, 8.5)):
        current_body.text(MARGIN, y, part, size=8.5, color=GRAY)
        y -= 11.5

    # --- Cabeceras/pies de página ---
    total_pages = len(doc.pages)
    for number, builder in enumerate(doc.pages, start=1):
        builder.fill_rect(MARGIN, FOOTER_Y + 14, USABLE_W, 0.6, LIGHT_LINE)
        builder.text(MARGIN, FOOTER_Y, FOOTER_LEFT, size=8, bold=True, color=GRAY)
        stamp = format_spanish_datetime(generated_at)
        builder.text(
            MARGIN,
            FOOTER_Y - 11,
            f"Informe generado el {stamp} (hora del servidor)",
            size=8,
            color=GRAY,
        )
        builder.text(
            PAGE_W - MARGIN,
            FOOTER_Y,
            f"Página {number} de {total_pages}",
            size=8,
            color=GRAY,
        )

    return doc.render()


# Referencia de almacenamiento recomendada para Ubuntu Server (documentación
# oficial). Se usa como orientación, no como umbral automático del lote.
MIGRATION_MIN_REFERENCE_GB = 25.0
MIGRATION_ACTION_TEXT = (
    "Planificar la migración a una versión con soporte (por ejemplo, Ubuntu "
    "Server 24.04 LTS). Resguardar datos y configuración, validar las "
    "aplicaciones y ejecutar la migración en una ventana de mantenimiento. "
    "La plataforma no actualiza la distribución ni reinicia la estación "
    "automáticamente."
)


def build_migration_report(
    batch: Any,
    host_results: Sequence[Any] | None,
    task_name: str = "",
    generated_at: datetime | None = None,
) -> bytes:
    """Informe PDF específico para estaciones que requieren migración.

    Solo incluye estaciones cuyo estado final declara ``attention_kind ==
    "migration"`` (Ubuntu 20.04 y derivados basados en focal). El espacio
    insuficiente se reporta en el informe general, no aquí.
    """
    doc = PDFDocument()
    generated_at = generated_at or datetime.now(UTC)
    rows = _migration_rows(host_results or [])
    y = PAGE_H - MARGIN

    def ensure(space: float) -> None:
        nonlocal y
        if y - space < MARGIN + 34:
            doc.add_page()
            new_body = doc.content
            new_body.text(
                MARGIN,
                PAGE_H - MARGIN + 6,
                BRAND,
                size=9,
                bold=True,
                color=DARK,
            )
            new_body.fill_rect(MARGIN, PAGE_H - MARGIN + 8, USABLE_W, 0.8, DARK)
            y = PAGE_H - MARGIN - 6

    def write(
        text: str,
        size: float = 9,
        bold: bool = False,
        color: tuple[float, ...] = (0, 0, 0),
        gap: float = 12.0,
        indent: float = 0.0,
    ) -> None:
        nonlocal y
        for part in _wrap_text(text, _max_chars(USABLE_W - indent, size)):
            ensure(size + 4)
            doc.content.text(
                MARGIN + indent, y, part, size=size, bold=bold, color=color
            )
            y -= gap

    # --- Cabecera ---
    doc.content.text(MARGIN, y, BRAND, size=17, bold=True, color=DARK)
    y -= 17
    doc.content.text(
        MARGIN, y, "Informe de migración de distribución", size=13, bold=True
    )
    y -= 15
    doc.content.text(MARGIN, y, ORG, size=8.5, color=GRAY)
    y -= 12
    doc.content.fill_rect(MARGIN, y, USABLE_W, 1.2, DARK)
    y -= 22

    write(f"Tarea: {task_name or batch.task_id or '—'}", size=9)
    write(f"Objetivo: {batch.target or '—'}", size=9)
    write(f"Ordenada por: {batch.operator or '—'}", size=9)
    write(f"Inicio: {format_spanish_datetime(batch.created_at)}", size=9)
    write(
        f"Referencia: Lote {batch.batch_id[:12]} · corrida "
        f"{batch.created_at.strftime('%Y%m%d')}",
        size=9,
    )
    y -= 4
    write(
        f"{len(rows)} estación(es) requieren migración de distribución. "
        "No se actualizaron automáticamente.",
        size=9.5,
        bold=True,
        color=RED,
    )
    y -= 4

    if not rows:
        write(
            "No hay estaciones con migración pendiente en esta corrida.",
            size=9,
            color=GRAY,
        )
    else:
        for row in rows:
            state = _apt_state(row)
            station = getattr(row, "host", "—")
            name = str(state.get("hostname") or "").strip() or "—"
            os_pretty = str(state.get("os_pretty") or "").strip() or "—"
            try:
                ram_mb = int(state.get("ram_mb") or 0)
            except (TypeError, ValueError):
                ram_mb = 0
            try:
                cores = int(state.get("cpu_cores") or 0)
            except (TypeError, ValueError):
                cores = 0
            cpu_model = str(state.get("cpu_model") or "").strip()
            try:
                disk_total = float(state.get("disk_total_gb") or 0)
            except (TypeError, ValueError):
                disk_total = 0.0
            try:
                disk_free = float(state.get("disk_free_gb") or 0)
            except (TypeError, ValueError):
                disk_free = 0.0
            reason = (
                (getattr(row, "error", "") or "").strip()
                or str(state.get("attention_reason") or "").strip()
                or "Distribución sin soporte estándar."
            )

            # Cada estación se presenta como una ficha para que los datos
            # técnicos y la acción recomendada sean legibles sin tabla ancha.
            ensure(110)
            doc.content.fill_rect(MARGIN, y - 4, USABLE_W, 0.6, LIGHT_LINE)
            y -= 14
            write(f"{station} — {name}", size=11, bold=True, color=DARK)
            write(f"Sistema operativo: {os_pretty}", size=9)
            resources = f"{ram_mb} MB de RAM · {cores} CPU"
            if cpu_model:
                resources += f" · {cpu_model}"
            write(f"Recursos: {resources}", size=9)
            write(
                f"Disco: {disk_total:.2f} GB totales · " f"{disk_free:.2f} GB libres",
                size=9,
            )
            write(f"Motivo: {reason}", size=9, color=(0.55, 0.28, 0.05))
            write(
                f"Referencia de almacenamiento: Ubuntu Server recomienda al "
                f"menos {MIGRATION_MIN_REFERENCE_GB:g} GB de disco.",
                size=9,
            )
            write(f"Acción recomendada: {MIGRATION_ACTION_TEXT}", size=9)
            y -= 6

    y -= 6
    write(
        "Este informe se genera solo cuando la política de la tarjeta "
        "detecta una distribución bloqueada. El espacio insuficiente se "
        "documenta en el informe general de la corrida.",
        size=8.5,
        color=GRAY,
    )

    # --- Cabeceras/pies de página ---
    total_pages = len(doc.pages)
    for number, builder in enumerate(doc.pages, start=1):
        builder.fill_rect(MARGIN, FOOTER_Y + 14, USABLE_W, 0.6, LIGHT_LINE)
        builder.text(MARGIN, FOOTER_Y, FOOTER_LEFT, size=8, bold=True, color=GRAY)
        stamp = format_spanish_datetime(generated_at)
        builder.text(
            MARGIN,
            FOOTER_Y - 11,
            f"Informe generado el {stamp} (hora del servidor)",
            size=8,
            color=GRAY,
        )
        builder.text(
            PAGE_W - MARGIN,
            FOOTER_Y,
            f"Página {number} de {total_pages}",
            size=8,
            color=GRAY,
        )

    return doc.render()
