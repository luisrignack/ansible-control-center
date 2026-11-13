"""Rutas de contrasenas e historial.
"""

from __future__ import annotations

import logging
import os

from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from acc import runtime
from acc.operations import read_temporal_passwords
from acc.runtime import (
    get_vault_file,
)
from acc.security import (
    _check_rate_limit,
    _client_ip,
    _rate_limited_response,
    _wants_json,
)
from config import (
    ALLOWED_PASSWORD_EXTENSIONS,
    MAX_CONTENT_LENGTH,
    PASSWORD_ORIGIN_VALUES,
    PASSWORD_ORIGINS,
    VAULT_RATE_LIMIT,
    VAULT_RATE_WINDOW,
)
from models.batch import ExecutionBatch
from models.password import PasswordRecord
from services.secure_temp import secure_temp_file
from services.vault_manager import VaultManager

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


def _password_response(success: bool, message: str, status: int = 200):
    if _wants_json():
        return jsonify({"success": success, "message": message}), status
    flash(message, "success" if success else "danger")
    return redirect(url_for("passwords"))


@app.route("/passwords", methods=["GET", "POST"])
@login_required
def passwords():
    if request.method == "POST":
        # El vault es compartido: todos pueden usarlo al ejecutar tareas,
        # pero solo un administrador puede añadir, importar o eliminar.
        if not current_user.has_role("admin"):
            abort(403)
        retry_after = _check_rate_limit(
            f"vault:{current_user.get_id()}:{_client_ip()}",
            VAULT_RATE_LIMIT,
            VAULT_RATE_WINDOW,
        )
        if retry_after is not None:
            logger.warning(
                "Operación de vault bloqueada por rate limit para usuario=%s",
                current_user.get_id(),
            )
            return _rate_limited_response(
                retry_after,
                "Demasiadas operaciones sobre el vault. Espera antes de reintentar.",
            )

        action = request.form.get("action", "")
        mpw = request.form.get("master_password", "")

        if action == "encrypt":
            pws = [
                line
                for line in request.form.get("passwords_textarea", "").splitlines()
                if line.strip()
            ]
            origin = request.form.get("origin", "manual").strip().lower() or "manual"
            if not mpw:
                return _password_response(
                    False, "Ingresa la contraseña maestra del Vault.", 400
                )
            if not pws:
                return _password_response(
                    False, "Ingresa al menos una contraseña candidata.", 400
                )
            if origin not in PASSWORD_ORIGIN_VALUES:
                return _password_response(False, "Origen de contraseña no válido.", 400)
            success, msg = VaultManager.encrypt_passwords(
                passwords=pws,
                master_password=mpw,
                origin=origin,
                db_path=runtime.get_db_path(),
            )
            return _password_response(success, msg, 200 if success else 400)

        if action == "delete":
            pid = request.form.get("password_id", "")
            try:
                password_id = int(pid)
            except (TypeError, ValueError):
                return _password_response(False, "ID de contraseña no válido.", 400)

            hash_full = PasswordRecord.get_hash_full(password_id, db_path=runtime.get_db_path())
            if hash_full is None:
                return _password_response(False, "La contraseña no existe.", 400)

            try:
                vault_file = get_vault_file()
            except RuntimeError as exc:
                return _password_response(False, str(exc), 400)

            if os.path.exists(vault_file):
                if not mpw:
                    return _password_response(
                        False,
                        "La contraseña maestra es obligatoria para actualizar el vault.",
                        400,
                    )
                removed, message = VaultManager.remove_password(
                    hash_full, mpw, vault_file=vault_file
                )
                if not removed:
                    return _password_response(False, message, 400)

            deleted = PasswordRecord.delete_password(password_id, db_path=runtime.get_db_path())
            return _password_response(
                deleted,
                (
                    "Contraseña eliminada del vault y de la base de datos."
                    if deleted
                    else "No se pudo eliminar de la base de datos."
                ),
                200 if deleted else 400,
            )

        if action == "load_file":
            uploaded = request.files.get("password_file")
            if uploaded is None or not uploaded.filename:
                return _password_response(
                    False, "No se seleccionó ningún archivo.", 400
                )

            extension = os.path.splitext(uploaded.filename)[1].lower()
            if extension not in ALLOWED_PASSWORD_EXTENSIONS:
                return _password_response(
                    False,
                    "Solo se permiten archivos .txt, .yml o .yaml.",
                    400,
                )

            if not mpw:
                return _password_response(
                    False,
                    "La contraseña maestra es obligatoria para importar contraseñas.",
                    400,
                )

            if extension == ".txt":
                pws, file_error = read_temporal_passwords(uploaded)
                if file_error:
                    return _password_response(False, file_error, 400)
                success, msg = VaultManager.encrypt_passwords(
                    passwords=pws,
                    master_password=mpw,
                    origin="file",
                    db_path=runtime.get_db_path(),
                )
                return _password_response(success, msg, 200 if success else 400)

            # .yml/.yaml: debe ser un vault ya cifrado con ansible-vault.
            raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
            if len(raw) > MAX_CONTENT_LENGTH:
                return _password_response(
                    False, "El archivo del vault es demasiado grande.", 400
                )
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                return _password_response(
                    False, "El archivo debe estar codificado en UTF-8.", 400
                )

            if not content.lstrip().startswith("$ANSIBLE_VAULT"):
                return _password_response(
                    False,
                    "El archivo .yml debe ser un vault cifrado con ansible-vault.",
                    400,
                )

            with secure_temp_file(
                content, prefix=".vault-upload-", suffix=".yml"
            ) as uploaded_path:
                ok, message, entries = VaultManager.decrypt_vault_file(
                    vault_file=uploaded_path,
                    master_password=mpw,
                )

            if not ok:
                return _password_response(False, message, 400)

            pws = [entry["password"] for entry in entries or []]
            if not pws:
                return _password_response(
                    False, "El vault no contiene contraseñas.", 400
                )

            success, msg = VaultManager.encrypt_passwords(
                passwords=pws,
                master_password=mpw,
                origin="file",
                db_path=runtime.get_db_path(),
            )
            if success:
                msg = f"Vault importado correctamente. {msg}"
            return _password_response(success, msg, 200 if success else 400)

        return _password_response(False, "Acción no reconocida.", 400)

    stored = PasswordRecord.get_all_passwords(limit=100, db_path=runtime.get_db_path())
    try:
        vault_exists = os.path.exists(get_vault_file())
    except RuntimeError:
        vault_exists = False
    return render_template(
        "passwords.html",
        passwords=stored,
        vault_exists=vault_exists,
        password_origin_labels=dict(PASSWORD_ORIGINS),
    )


@app.route("/history")
@login_required
def history():
    batches = ExecutionBatch.get_batches(limit=50, db_path=runtime.get_db_path())
    stats = ExecutionBatch.get_stats(db_path=runtime.get_db_path())
    return render_template("history.html", batches=batches, stats=stats)

def register(flask_app) -> None:
    app.apply(flask_app)

