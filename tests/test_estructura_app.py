"""Caracterización de la estructura de la aplicación tras el troceado.

Estas pruebas fijan lo que no debe cambiar al mover código de ``app.py`` a
``acc/``: nombres de *endpoint* y URLs (``url_for`` y las plantillas dependen de
ellos), métodos HTTP, comandos del CLI, hooks de seguridad y la fachada pública
que consumen ``gunicorn`` y las pruebas históricas.
"""

from __future__ import annotations

import app as app_module
from acc import runtime, task_state

# (endpoint, regla, métodos) en el orden en que se registran.
URL_MAP_ESPERADO = [
    ("acerca_de", "/acerca-de", ["GET"]),
    ("api_metricas", "/api/metricas", ["GET"]),
    ("api_task_status", "/api/task-status/<task_id>", ["GET"]),
    ("aprobaciones", "/aprobaciones", ["GET", "POST"]),
    ("audit", "/audit", ["GET"]),
    ("batch_detail", "/batches/<batch_id>", ["GET"]),
    ("batch_migration_pdf", "/batches/<batch_id>/migracion.pdf", ["GET"]),
    ("cancel_task", "/tasks/<task_id>/cancel", ["POST"]),
    ("configuracion", "/configuracion", ["GET"]),
    (
        "ejecutar_aprobacion",
        "/mis-solicitudes/<int:request_id>/ejecutar",
        ["GET", "POST"],
    ),
    ("execute_execution", "/execute", ["POST"]),
    ("healthz", "/healthz", ["GET"]),
    ("history", "/history", ["GET"]),
    ("index", "/", ["GET", "POST"]),
    ("inventario", "/inventario", ["GET", "POST"]),
    ("login", "/login", ["GET", "POST"]),
    ("logout", "/logout", ["POST"]),
    ("metricas", "/metricas", ["GET"]),
    ("mis_solicitudes", "/mis-solicitudes", ["GET"]),
    ("modulo_mostrar", "/modulos/<task_id>/mostrar", ["POST"]),
    ("modulo_ocultar", "/modulos/<task_id>/ocultar", ["POST"]),
    ("modulos", "/modulos", ["GET"]),
    ("modulos_restaurar", "/modulos/restaurar", ["POST"]),
    ("passwords", "/passwords", ["GET", "POST"]),
    ("preview_detail", "/preview/<batch_id>", ["GET"]),
    ("preview_execution", "/preview", ["POST"]),
    ("programaciones", "/programaciones", ["GET", "POST"]),
    ("progress_page", "/progress/<task_id>", ["GET"]),
    ("progress_sse", "/progress/<task_id>/events", ["GET"]),
    ("schedule_edit", "/programaciones/<int:schedule_id>/editar", ["GET", "POST"]),
    ("static", "/static/<path:filename>", ["GET"]),
    ("task_migration_pdf", "/tarea/<task_id>/migracion.pdf", ["GET"]),
    ("task_report_pdf", "/tarea/<task_id>/informe.pdf", ["GET"]),
    ("task_view", "/tarea/<task_id>", ["GET"]),
    ("update_dry_run", "/settings/dry-run", ["POST"]),
    ("usuarios", "/usuarios", ["GET"]),
    ("usuarios_crear", "/usuarios/crear", ["POST"]),
    ("usuarios_eliminar", "/usuarios/eliminar", ["POST"]),
    ("usuarios_estado", "/usuarios/estado", ["POST"]),
    ("usuarios_ldaps", "/usuarios/ldaps", ["POST"]),
    ("usuarios_password", "/usuarios/password", ["POST"]),
    ("usuarios_rol", "/usuarios/rol", ["POST"]),
]


def test_url_map_y_endpoints_no_cambian():
    actual = [
        (rule.endpoint, rule.rule, sorted(rule.methods - {"HEAD", "OPTIONS"}))
        for rule in sorted(
            app_module.app.url_map.iter_rules(), key=lambda rule: rule.endpoint
        )
    ]
    assert actual == URL_MAP_ESPERADO


def test_comandos_del_cli_siguen_disponibles():
    assert {
        "init-db",
        "init-admin",
        "scheduler",
        "reconcile-batches",
        "sync-known-hosts",
    } <= set(app_module.app.cli.commands)


def test_manipuladores_de_error_registrados():
    handlers = app_module.app.error_handler_spec[None]
    assert {400, 403, 404, 429, 500} <= set(handlers)


def test_hooks_de_metricas_y_seguridad_en_orden():
    before = app_module.app.before_request_funcs[None]
    assert [func.__name__ for func in before] == [
        "_metrics_before_request",
        "_security_before_request",
    ]
    # Flask ejecuta ``after_request`` en orden inverso al registro: la
    # seguridad se aplica antes que las métricas.
    after = [func.__name__ for func in app_module.app.after_request_funcs[None]]
    assert after.index("_security_headers") > after.index("_metrics_after_request")


def test_plantillas_y_estaticos_se_resuelven_desde_la_raiz():
    from pathlib import Path

    import config

    root = Path(app_module.app.root_path)
    assert root == Path(config.BASE_DIR)
    assert (root / "templates" / "base.html").is_file()
    assert (root / "static").is_dir()
    assert app_module.app.jinja_env.get_template("base.html") is not None


def test_fachada_reexporta_los_simbolos_historicos():
    assert app_module.create_task is task_state.create_task
    assert app_module.update_task is task_state.update_task
    assert app_module.remove_task is task_state.remove_task
    assert app_module.get_db_path is runtime.get_db_path
    assert app_module.is_dry_run is runtime.is_dry_run
    assert app_module.app.name == "app"


def test_login_manager_apunta_al_endpoint_login():
    from acc.extensions import login_manager

    assert login_manager.login_view == "login"
    assert app_module.login_manager is login_manager
