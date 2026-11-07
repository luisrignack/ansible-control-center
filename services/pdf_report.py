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
from datetime import datetime, timezone
from typing import Any, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Clasificación de motivos
# ---------------------------------------------------------------------------
# Pares (patrón case-insensitive, motivo breve). El orden importa: lo más
# específico primero; el resto se reporta con el texto del error truncado.
REASON_PATTERNS: Tuple[Tuple[str, str], ...] = (
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
    ("playbook", "El playbook falló en la estación"),
    ("hora_errors", "La verificación no confirmó el cambio"),
    ("zona final", "La verificación no confirmó el cambio"),
    ("no se pudo", "Fallo interno del servidor"),
)

_MESES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio", "julio",
    "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)


def format_spanish_datetime(
    value: Optional[datetime], with_time: bool = True
) -> str:
    """Fecha/hora legible en español, en la zona local del servidor."""
    if value is None:
        return "—"
    local = value if value.tzinfo is None else value.astimezone()
    base = f"{local.day} de {_MESES[local.month - 1]} de {local.year}"
    if with_time:
        base += f", {local.hour:02d}:{local.minute:02d}"
    return base


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
PAGE_W = 595.28   # A4 en puntos
PAGE_H = 841.89
MARGIN = 54
USABLE_W = PAGE_W - 2 * MARGIN
FOOTER_Y = 40

BRAND = "Ansible Control Center"
ORG = "Grupo de Migración a Código Abierto / DCTI - ETECSA"

# Estimación de ancho medio de Helvetica ≈ 0.52 × tamaño de fuente.
_CHARS_PER_POINT = 0.52


def _max_chars(col_width: float, font_size: float) -> int:
    return max(6, int(col_width / (font_size * _CHARS_PER_POINT)))


def _wrap_text(text: str, max_chars: int) -> List[str]:
    """Corte de línea simple por anchura estimada."""
    output: List[str] = []
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
                word = word[max_chars - 1:]
            current = word
        output.append(current)
    return output


class _ContentBuilder:
    """Construye un stream de contenido PDF (texto y gráficos vectoriales)."""

    def __init__(self) -> None:
        self.parts: List[str] = []

    def fill_rect(
        self, x: float, y: float, w: float, h: float, color: Tuple[float, ...]
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
        color: Tuple[float, ...] = (0, 0, 0),
    ) -> None:
        font = "/F2" if bold else "/F1"
        r, g, b = (round(float(c), 3) for c in color)
        # Solo se escapan los caracteres especiales del PDF; la conversión a
        # WinAnsi (cp1252) se hace una sola vez al guardar el stream.
        safe = (
            (text or "")
            .replace("\\", r"\\")
            .replace("(", r"\(")
            .replace(")", r"\)")
        )
        self.parts.append(
            f"BT {r} {g} {b} rg {font} {size:.1f} Tf "
            f"1 0 0 1 {x:.2f} {y:.2f} Tm ({safe}) Tj ET"
        )

    def build(self) -> str:
        return "\n".join(self.parts)


class PDFDocument:
    """PDF de varias páginas con las fuentes base F1/F2 (sin colecciones)."""

    def __init__(self) -> None:
        self.pages: List[_ContentBuilder] = [_ContentBuilder()]

    @property
    def content(self) -> _ContentBuilder:
        return self.pages[-1]

    def add_page(self) -> None:
        self.pages.append(_ContentBuilder())

    def render(self) -> bytes:
        buf = io.BytesIO()
        buf.write(b"%PDF-1.4\n%\xc7\xec\x8f\xa2\n")

        n_pages = len(self.pages)
        # Objetos: 1 catálogo, 2 árbol de páginas, 3-4 fuentes,
        # luego (contenido, página) por cada página a partir del 5.
        objects: List[bytes] = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            (
                f"<< /Type /Pages /Count {n_pages} /Kids ["
                + " ".join(
                    f"{5 + 2 * k + 1} 0 R" for k in range(n_pages)
                )
                + "] >>"
            ).encode(),
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
            b"/Encoding /WinAnsiEncoding >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
            b"/Encoding /WinAnsiEncoding >>",
        ]

        for index, content in enumerate(self.pages, start=1):
            stream = content.build().encode("cp1252", errors="replace")
            objects.append(
                f"<< /Length {len(stream)} >>\nstream\n".encode()
                + stream + b"\nendstream"
            )
            content_id = len(objects)
            page_id = 5 + 2 * (index - 1) + 1
            if content_id != page_id - 1:  # pragma: no cover - invariante
                raise RuntimeError(
                    "Estructura PDF inconsistente: el contenido de la página "
                    f"{index} quedó en el objeto {content_id}, se esperaba "
                    f"{page_id - 1}."
                )
            objects.append(
                (
                    f"<< /Type /Page /Parent 2 0 R "
                    f"/MediaBox [0 0 {PAGE_W:.2f} {PAGE_H:.2f}] "
                    f"/Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> "
                    f"/Contents {content_id} 0 R >>"
                ).encode()
            )

        offsets: List[int] = []
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
COLS_RATIO = (0.31, 0.18, 0.51)
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
}

RESULT_COLORS = {
    "success": GREEN,
    "failure": RED,
    "error": RED,
    "skipped": GRAY,
    "preview": (0.5, 0.3, 0.05),
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


def _batch_mode_text(batch: Any) -> str:
    if batch.dry_run:
        return "Simulada (DRY RUN, sin cambios)"
    if batch.status == "preview":
        return "Vista previa (solo inspección)"
    return "Ejecución real"


def build_execution_report(
    batch: Any,
    host_results: Optional[Sequence[Any]],
    task_name: str = "",
    generated_at: Optional[datetime] = None,
) -> bytes:
    """Construye un informe PDF de un lote: resumen y estaciones fallidas.

    ``batch``/``host_results`` son los objetos de ``models.batch``. El informe
    se forma por capas: cabecera, datos de la corrida, tabla de estaciones
    (fallidas primero) y pie con paginación.
    """
    doc = PDFDocument()
    body = doc.content
    generated_at = generated_at or datetime.now(timezone.utc)

    host_rows = list(host_results or [])
    failed_rows = [
        h for h in host_rows
        if h.result in {"failure", "error", "skipped"}
    ]
    ok_rows = [h for h in host_rows if h.result == "success"]
    ordered_rows = failed_rows + [h for h in host_rows if h not in failed_rows
                                  and h.result != "success"] + ok_rows

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
    line("Acción aplicada", f"{batch.action} = {batch.value or '—'}")
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
        f"fallidas · {batch.skipped_count} omitidas · {batch.preview_count} vistas previas"
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
        x = MARGIN
        for i, title in enumerate(("Estación", "Estado", "Motivo")):
            table_body.text(x + padding, head_y + 5, title, size=9, bold=True,
                            color=(1, 1, 1))
            x += col_widths[i]
        y = head_y - 2

    def ensure_space(needed: float) -> None:
        nonlocal y
        if y - needed < MARGIN + 30:
            doc.add_page()
            new_body = doc.content
            new_body.text(MARGIN, PAGE_H - MARGIN + 6, BRAND, size=9,
                          bold=True, color=DARK)
            new_body.fill_rect(
                MARGIN, PAGE_H - MARGIN + 8, USABLE_W, 0.8, DARK
            )
            # Reusar el builder nuevo para continuar el bucle.
            table_header()

    # Se sustituye el cuerpo activo cuando cambia la página.
    table_header()
    current_body = doc.content
    if not host_rows:
        ensure_space(row_min_h + 4)
        current_body = doc.content
        current_body.text(MARGIN, y - 10,
                          "No hay estaciones registradas en esta corrida.",
                          size=9, color=GRAY)
        y -= 18
    else:
        for index, row in enumerate(ordered_rows, start=1):
            if row.result == "success":
                if getattr(batch, "dry_run", False):
                    reason = "Simulación: sin cambios aplicados"
                else:
                    reason = "Ajuste aplicado y verificado"
            else:
                reason = classify_failure_reason(row.error, row.result)
            station = row.host
            estado = RESULT_LABELS.get(row.result, row.result)
            cells = (station, estado, reason)
            wrapped_cells = [
                _wrap_text(str(c), m) for c, m in zip(cells, max_chars)
            ]
            n_lines = max(len(w) for w in wrapped_cells)
            row_h = max(row_min_h, n_lines * line_h + 2 * padding)
            ensure_space(row_h)
            current_body = doc.content
            if index % 2 == 0:
                current_body.fill_rect(MARGIN, y - row_h, USABLE_W, row_h,
                                       ZEBRA)
            # Separador fino.
            current_body.fill_rect(
                MARGIN, y - row_h, USABLE_W, 0.5, LIGHT_LINE
            )
            x = MARGIN
            for i, parts in enumerate(wrapped_cells):
                ty = y - padding - line_h
                for j, part in enumerate(parts):
                    if i == 0:
                        current_body.text(x + padding, ty, part,
                                          size=font_size, bold=True)
                    elif i == 1:
                        color = RESULT_COLORS.get(row.result, (0, 0, 0))
                        current_body.text(x + padding, ty, part,
                                          size=font_size,
                                          bold=row.result in {"failure", "error"},
                                          color=color
                                          if j == 0 else (0, 0, 0))
                    else:
                        current_body.text(x + padding, ty, part,
                                          size=font_size)
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
        builder.fill_rect(
            MARGIN, FOOTER_Y + 14, USABLE_W, 0.6, LIGHT_LINE
        )
        builder.text(MARGIN, FOOTER_Y, FOOTER_LEFT, size=8, bold=True,
                     color=GRAY)
        stamp = format_spanish_datetime(generated_at)
        builder.text(
            MARGIN, FOOTER_Y - 11,
            f"Informe generado el {stamp} (hora del servidor)",
            size=8, color=GRAY,
        )
        builder.text(
            PAGE_W - MARGIN, FOOTER_Y,
            f"Página {number} de {total_pages}", size=8, color=GRAY,
        )

    return doc.render()
