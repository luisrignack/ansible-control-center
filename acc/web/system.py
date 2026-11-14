"""Vista unificada de Sistema.

Agrupa en una sola sección con pestañas lo que antes estaba repartido entre
el menú del avatar y la barra lateral: auditoría, rendimiento, ajustes y
acerca de. Las rutas antiguas se conservan como redirecciones para no romper
enlaces guardados.
"""

from __future__ import annotations

import platform
from datetime import UTC, datetime

from flask import current_app, render_template, request

from acc import runtime
from acc.runtime import (
    load_ldap_access_setting,
    request_metrics,
)
from acc.security import roles_required
from acc.task_state import _queue_snapshot
from acc.web.registry import RouteRegistry
from config import (
    AUTH_EVENT_RETENTION_DAYS,
    SLOW_REQUEST_MS,
    TASK_REQUEST_EMAIL,
)
from models.db import SCHEMA_VERSION
from models.user import AuthEvent
from services import ldap_auth
from services.metrics import summarize
from services.pdf_report import format_spanish_datetime

app = RouteRegistry()

SISTEMA_TABS = ("auditoria", "rendimiento", "ajustes", "acerca-de")
SISTEMA_TAB_LABELS = {
    "auditoria": "Auditoría",
    "rendimiento": "Rendimiento",
    "ajustes": "Ajustes",
    "acerca-de": "Acerca de",
}


def _about_context() -> dict:
    """Datos institucionales y técnicos que muestra la pestaña Acerca de."""
    now_utc = datetime.now(UTC)
    local_now = now_utc.astimezone()
    offset = local_now.strftime("%z")
    offset = f"UTC{offset[:3]}:{offset[3:]}" if offset else "UTC"
    timezone_label = f"{local_now.tzname() or 'Local'} ({offset})"
    return {
        "host_date": format_spanish_datetime(now_utc, with_time=True),
        "host_datetime_iso": local_now.isoformat(timespec="seconds"),
        "timezone_label": timezone_label,
        "app_version": current_app.config.get("APP_VERSION", "desconocida"),
        "build_commit": current_app.config.get("APP_BUILD_COMMIT", ""),
        "schema_version": SCHEMA_VERSION,
        "python_version": platform.python_version(),
        "platform_label": f"{platform.system()} {platform.machine()}",
        "support_email": TASK_REQUEST_EMAIL,
        "current_year": now_utc.year,
    }


@app.route("/sistema")
@roles_required("admin")
def sistema():
    """Sección Sistema: auditoría, rendimiento, ajustes y acerca de."""
    tab = request.args.get("tab", "auditoria").strip().lower()
    if tab not in SISTEMA_TABS:
        tab = "auditoria"

    db_path = runtime.get_db_path()
    snapshot = request_metrics.snapshot()
    rows = sorted(
        snapshot.items(),
        key=lambda item: item[1].get("p95_ms", 0.0),
        reverse=True,
    )
    return render_template(
        "sistema.html",
        tab=tab,
        tabs=SISTEMA_TABS,
        tab_labels=SISTEMA_TAB_LABELS,
        events=AuthEvent.recent(limit=200, db_path=db_path),
        retention_days=AUTH_EVENT_RETENTION_DAYS,
        metrics=snapshot,
        rows=rows,
        summary=summarize(snapshot),
        queue=_queue_snapshot(),
        window=request_metrics.window,
        slow_request_ms=SLOW_REQUEST_MS,
        ldap_supported=load_ldap_access_setting(),
        ldap_configured=ldap_auth.ldap_configured(),
        **_about_context(),
    )


def register(flask_app) -> None:
    app.apply(flask_app)
