"""Extensiones compartidas de Flask.

Se declara aquí el ``LoginManager`` para que los módulos de rutas y de
seguridad puedan decorar funciones sin depender del módulo de arranque.
"""

from __future__ import annotations

from flask_login import LoginManager

login_manager = LoginManager()
login_manager.login_view = "login"
login_manager.login_message = "Inicia sesión para continuar."
login_manager.login_message_category = "warning"
login_manager.session_protection = "strong"
