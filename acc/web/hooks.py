"""Hooks de peticion: metricas, CSP, cabeceras de seguridad y contexto.
"""

from __future__ import annotations

import logging
import secrets
import time

from flask import g, request
from flask_login import current_user

from acc import runtime
from acc.runtime import (
    is_dry_run,
    request_metrics,
)
from config import (
    CSP_ENABLED,
    HSTS_ENABLED,
    HSTS_MAX_AGE,
    IS_PRODUCTION,
    MAX_HOSTS,
    REQUEST_METRICS_ENABLED,
    SECURITY_HEADERS_ENABLED,
    SLOW_REQUEST_MS,
)
from models.batch import (
    HOST_RESULT_LABELS,
    STATUS_LABELS,
)
from models.ops import (
    APPROVAL_PENDING,
    ApprovalRequest,
)
from services import task_catalog
from services.csrf import (
    csrf_protect,
    generate_csrf_token,
)

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


_METRICS_SKIP_ENDPOINTS = {"static", "healthz", "progress_sse"}


@app.before_request
def _metrics_before_request():
    if REQUEST_METRICS_ENABLED:
        g._acc_request_started = time.perf_counter()


@app.after_request
def _metrics_after_request(response):
    started = getattr(g, "_acc_request_started", None)
    if started is None:
        return response
    endpoint = request.endpoint or "desconocido"
    if endpoint in _METRICS_SKIP_ENDPOINTS:
        return response
    duration_ms = (time.perf_counter() - started) * 1000.0
    request_metrics.record(endpoint, duration_ms, response.status_code)
    if duration_ms >= SLOW_REQUEST_MS:
        logger.warning(
            "Petición lenta: endpoint=%s status=%s duracion_ms=%.1f",
            endpoint,
            response.status_code,
            duration_ms,
        )
    return response


@app.before_request
def _security_before_request():
    g.csp_nonce = secrets.token_urlsafe(16)
    return csrf_protect()


@app.context_processor
def _security_context():
    pending_approvals = 0
    if current_user.is_authenticated and current_user.has_role("admin", "operator"):
        try:
            db_path = runtime.get_db_path()
            if current_user.has_role("admin"):
                pending_approvals = len(
                    ApprovalRequest.list_status(
                        APPROVAL_PENDING, limit=50, db_path=db_path
                    )
                )
            else:
                pending_approvals = sum(
                    1
                    for item in ApprovalRequest.list_for_requester(
                        current_user.username, limit=50, db_path=db_path
                    )
                    if item.status == APPROVAL_PENDING
                )
        except Exception:  # noqa: BLE001 - contexto nunca debe romper
            pending_approvals = 0
    palette_tasks = [
        {
            "id": task["id"],
            "name": task["name"],
            "icon": task.get("icon", "bi-lightning-charge"),
        }
        for task in task_catalog.TASK_CATALOG
    ]
    return {
        "csrf_token": generate_csrf_token,
        "csp_nonce": getattr(g, "csp_nonce", ""),
        "dry_run": is_dry_run(),
        "is_production": IS_PRODUCTION,
        "max_hosts": MAX_HOSTS,
        "status_labels": STATUS_LABELS,
        "host_result_labels": HOST_RESULT_LABELS,
        "ui_config_panel": runtime.UI_CONFIG_PANEL,
        "pending_approvals": pending_approvals,
        "palette_tasks": palette_tasks,
    }


@app.after_request
def _security_headers(response):
    if not SECURITY_HEADERS_ENABLED:
        return response

    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault(
        "Permissions-Policy", "geolocation=(), microphone=(), camera=()"
    )

    if CSP_ENABLED:
        nonce = getattr(g, "csp_nonce", "")
        response.headers.setdefault(
            "Content-Security-Policy",
            (
                "default-src 'self'; "
                f"script-src 'self' 'nonce-{nonce}'; "
                "style-src 'self'; "
                "font-src 'self'; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "object-src 'none'; "
                "base-uri 'self'; "
                "form-action 'self'; "
                "frame-ancestors 'none'"
            ),
        )

    if HSTS_ENABLED:
        response.headers.setdefault(
            "Strict-Transport-Security",
            f"max-age={HSTS_MAX_AGE}; includeSubDomains",
        )

    if response.mimetype == "text/html":
        response.headers.setdefault("Cache-Control", "no-store")

    return response

def register(flask_app) -> None:
    app.apply(flask_app)

