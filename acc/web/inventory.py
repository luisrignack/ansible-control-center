"""Inventario de grupos y hosts."""

from __future__ import annotations

import csv
import io
import os

from flask import (
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user, login_required

from acc import runtime
from acc.security import (
    _can_manage_owner,
    _current_owner_id,
    _owner_scope,
    _view_all_requested,
    roles_required,
)
from acc.web.registry import RouteRegistry
from config import (
    MAX_CONTENT_LENGTH,
    MAX_HOSTS,
    SECRETS_DIR,
)
from models.batch import ExecutionBatch
from models.ops import (
    HostGroup,
    InventoryHost,
    Schedule,
)
from services.safety import (
    CRITICALITY_LABELS,
    CRITICALITY_LEVELS,
)

app = RouteRegistry()


INVENTORY_KEYS_DIR = SECRETS_DIR / "inventory_keys"

# Cabeceras admitidas (español e inglés) para importar CSV.
_CSV_ADDRESS_KEYS = ("address", "direccion", "dirección", "ip", "host")
_CSV_LABEL_KEYS = ("label", "etiqueta", "nombre")
_CSV_CRITICALITY_KEYS = ("criticality", "criticidad")
_MAX_IMPORT_ERRORS = 10


def _csv_cell(value: object) -> str:
    """Neutraliza fórmulas al abrir el CSV en una hoja de cálculo.

    Un valor que empieza por ``=``, ``+``, ``-`` o ``@`` puede ejecutarse como
    fórmula en Excel/LibreOffice. El inventario lo controla el operador, pero
    la exportación se comparte por correo; se antepone un apóstrofo.
    """
    text = "" if value is None else str(value)
    if text[:1] in {"=", "+", "-", "@", "\t", "\r"}:
        return "'" + text
    return text


def _inventario_group_and_hosts(owner_user_id: int | None = None):
    groups = [
        g.to_dict()
        for g in HostGroup.all(
            db_path=runtime.get_db_path(), owner_user_id=owner_user_id
        )
    ]
    outcomes = ExecutionBatch.latest_host_outcomes(
        db_path=runtime.get_db_path()
    )
    for group in groups:
        hosts = [
            h.to_dict()
            for h in HostGroup.hosts_of(group["id"], db_path=runtime.get_db_path())
        ]
        for host in hosts:
            outcome = outcomes.get(host["address"], {})
            host["last_result"] = outcome.get("last_result", "")
            host["last_run"] = outcome.get("last_run", "")
        group["hosts"] = hosts
    return groups


def _csv_key(row: dict[str, str], keys: tuple[str, ...]) -> str:
    for key, value in row.items():
        if (key or "").strip().lower() in keys:
            return (value or "").strip()
    return ""


@app.route("/inventario/exportar.csv")
@login_required
def inventario_exportar_csv():
    """Exporta el inventario visible en CSV (UTF-8 con BOM para Excel)."""
    groups = _inventario_group_and_hosts(_owner_scope())
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "grupo",
            "descripcion",
            "plataforma",
            "conexion",
            "direccion",
            "etiqueta",
            "criticidad",
            "añadido",
            "ultima_ejecucion",
            "ultimo_resultado",
        ]
    )
    for group in groups:
        for host in group["hosts"]:
            writer.writerow(
                [
                    _csv_cell(group["name"]),
                    _csv_cell(group.get("description", "")),
                    _csv_cell(group.get("os_flavor", "")),
                    _csv_cell(group.get("connection", "")),
                    _csv_cell(host.get("address", "")),
                    _csv_cell(host.get("label", "")),
                    _csv_cell(host.get("criticality", "")),
                    _csv_cell(host.get("created_at", "")),
                    _csv_cell(host.get("last_run", "")),
                    _csv_cell(host.get("last_result", "")),
                ]
            )
    response = Response(
        "\ufeff" + buffer.getvalue(),
        mimetype="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="inventario.csv"',
            "Cache-Control": "no-store",
        },
    )
    return response


@app.route("/inventario/importar", methods=["POST"])
@roles_required("operator")
def inventario_importar_csv():
    """Importa hosts a un grupo desde un CSV (solo añade, nunca borra)."""
    db_path = runtime.get_db_path()
    group_id = request.form.get("group_id", "").strip()
    group = HostGroup.get(int(group_id), db_path=db_path) if group_id.isdigit() else None
    if group is None:
        flash("Selecciona un grupo válido para importar.", "warning")
        return redirect(url_for("inventario"))
    if not _can_manage_owner(group.owner_user_id):
        abort(403)

    uploaded = request.files.get("csv_file")
    if uploaded is None or not uploaded.filename:
        flash("Selecciona un fichero CSV.", "warning")
        return redirect(url_for("inventario"))

    raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
    if len(raw) > MAX_CONTENT_LENGTH:
        flash("El CSV es demasiado grande.", "danger")
        return redirect(url_for("inventario"))
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        flash("El CSV debe estar codificado en UTF-8.", "danger")
        return redirect(url_for("inventario"))

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames or not any(
        (name or "").strip().lower() in _CSV_ADDRESS_KEYS
        for name in reader.fieldnames
    ):
        flash(
            "El CSV necesita una columna 'address' (o 'direccion'/'ip').",
            "danger",
        )
        return redirect(url_for("inventario"))

    added = 0
    skipped = 0
    errors: list[str] = []
    for line_number, row in enumerate(reader, start=2):
        if line_number - 1 > MAX_HOSTS + 1:
            errors.append(f"Se superó el máximo de {MAX_HOSTS} filas; resto ignorado.")
            break
        address = _csv_key(row, _CSV_ADDRESS_KEYS)
        if not address:
            skipped += 1
            continue
        label = _csv_key(row, _CSV_LABEL_KEYS)
        criticality = _csv_key(row, _CSV_CRITICALITY_KEYS)
        _host, error = InventoryHost.add(
            group.id,
            address,
            label=label,
            db_path=db_path,
            criticality=criticality,
        )
        if error:
            if "UNIQUE" in error or "duplic" in error.lower():
                skipped += 1
            else:
                if len(errors) < _MAX_IMPORT_ERRORS:
                    errors.append(f"Línea {line_number}: {error}")
                skipped += 1
        else:
            added += 1

    message = f"Importación CSV: {added} host(s) añadido(s), {skipped} omitido(s)."
    flash(message, "success" if added else "info")
    for error in errors:
        flash(error, "danger")
    return redirect(url_for("inventario"))


@app.route("/inventario", methods=["GET", "POST"])
@login_required
def inventario():
    db_path = runtime.get_db_path()
    if request.method == "POST":
        if not current_user.has_role("admin", "operator"):
            abort(403)
        action = request.form.get("action", "").strip()
        if action == "create_group":
            try:
                HostGroup.create(
                    request.form.get("name", ""),
                    description=request.form.get("description", ""),
                    os_flavor=request.form.get("os_flavor", "linux"),
                    db_path=db_path,
                    criticality=request.form.get("criticality", "medium"),
                    window_days=request.form.get("window_days", ""),
                    window_start=request.form.get("window_start", ""),
                    window_end=request.form.get("window_end", ""),
                    window_note=request.form.get("window_note", ""),
                    owner_user_id=_current_owner_id(),
                )
                flash("Grupo creado.", "success")
            except ValueError as exc:
                flash(str(exc), "danger")
        elif action == "update_group_safety":
            gid = request.form.get("group_id", "").strip()
            group = HostGroup.get(gid, db_path=db_path) if gid.isdigit() else None
            if group is None:
                flash("El grupo no existe.", "warning")
            elif not _can_manage_owner(group.owner_user_id):
                abort(403)
            else:
                try:
                    HostGroup.set_safety(
                        group.id,
                        request.form.get("criticality", "medium"),
                        window_days=request.form.get("window_days", ""),
                        window_start=request.form.get("window_start", ""),
                        window_end=request.form.get("window_end", ""),
                        window_note=request.form.get("window_note", ""),
                        db_path=db_path,
                    )
                    flash(
                        f"Política de seguridad de '{group.name}' actualizada.",
                        "success",
                    )
                except ValueError as exc:
                    flash(str(exc), "danger")
        elif action == "delete_group":
            group = (
                HostGroup.get(request.form.get("group_id", "0"), db_path=db_path)
                if request.form.get("group_id", "").isdigit()
                else None
            )
            if group is None:
                flash("El grupo no existe.", "warning")
            elif not _can_manage_owner(group.owner_user_id):
                abort(403)
            else:
                group.delete(db_path=db_path)
                flash(f"Grupo '{group.name}' eliminado.", "success")
        elif action == "add_host":
            group = (
                HostGroup.get(request.form.get("group_id", "0"), db_path=db_path)
                if request.form.get("group_id", "").isdigit()
                else None
            )
            if group is None:
                flash("El grupo no existe.", "warning")
            elif not _can_manage_owner(group.owner_user_id):
                abort(403)
            else:
                _host, host_error = InventoryHost.add(
                    group.id,
                    request.form.get("address", ""),
                    label=request.form.get("label", ""),
                    db_path=db_path,
                    criticality=request.form.get("criticality", ""),
                )
                if host_error:
                    flash(host_error, "danger")
                else:
                    flash("Host añadido al grupo.", "success")
        elif action in {"update_host_criticality", "delete_host"}:
            host_id = request.form.get("host_id", "").strip()
            if not host_id.isdigit():
                flash("Host no válido.", "warning")
            else:
                host = InventoryHost.get(int(host_id), db_path=db_path)
                group = (
                    HostGroup.get(host.group_id, db_path=db_path)
                    if host is not None
                    else None
                )
                if host is None or group is None:
                    flash("El host no existe.", "warning")
                elif not _can_manage_owner(group.owner_user_id):
                    abort(403)
                elif action == "delete_host":
                    host.delete(db_path=db_path)
                    flash("Host eliminado del grupo.", "success")
                else:
                    try:
                        InventoryHost.set_criticality(
                            host.id,
                            request.form.get("criticality", ""),
                            db_path=db_path,
                        )
                        flash("Criticidad del host actualizada.", "success")
                    except ValueError as exc:
                        flash(str(exc), "danger")
        elif action == "upload_key":
            gid = request.form.get("group_id", "").strip()
            group = HostGroup.get(gid, db_path=db_path) if gid.isdigit() else None
            uploaded = request.files.get("ssh_key")
            if group is None:
                flash("El grupo no existe.", "warning")
            elif not _can_manage_owner(group.owner_user_id):
                abort(403)
            elif group.os_flavor == "windows":
                flash(
                    "Los grupos Windows se autentican por usuario/contraseña "
                    "winrm, no admiten clave SSH.",
                    "warning",
                )
            elif uploaded is None or not uploaded.filename:
                flash("Selecciona el fichero de clave privada (.pem).", "warning")
            else:
                raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
                if len(raw) > MAX_CONTENT_LENGTH:
                    flash("La clave es demasiado grande.", "danger")
                elif b"OPENSSH PRIVATE KEY" not in raw and b"PRIVATE KEY" not in raw:
                    flash(
                        "El fichero no parece una clave privada OpenSSH/PEM.",
                        "danger",
                    )
                else:
                    # Cada propietario tiene su subdirectorio para que dos
                    # grupos con el mismo nombre no compartan fichero de clave.
                    owner_dir = (
                        INVENTORY_KEYS_DIR / f"user-{group.owner_user_id}"
                        if group.owner_user_id
                        else INVENTORY_KEYS_DIR / "shared"
                    )
                    try:
                        owner_dir.mkdir(parents=True, exist_ok=True)
                        os.chmod(owner_dir, 0o700)
                    except OSError:  # pragma: no cover - permisos locales
                        owner_dir.mkdir(parents=True, exist_ok=True)
                    slug = (
                        "".join(
                            ch if ch.isalnum() or ch in "-_" else "_"
                            for ch in group.name.lower()
                        )[:40]
                        or f"group-{group.id}"
                    )
                    key_path = owner_dir / f"{slug}.pem"
                    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as key_file:
                        key_file.write(raw)
                    HostGroup.set_ssh_key(group.id, str(key_path), db_path=db_path)
                    flash(
                        "Clave SSH guardada y asociada al grupo (0600).",
                        "success",
                    )
        else:
            flash("Acción de inventario no válida.", "warning")
        if request.form.get("view_all") == "1":
            return redirect(url_for("inventario", all=1))
        return redirect(url_for("inventario"))

    show_all = _view_all_requested()
    owner_scope = _owner_scope()
    groups = _inventario_group_and_hosts(owner_scope)
    for group_data in groups:
        group_data["can_manage"] = _can_manage_owner(group_data.get("owner_user_id", 0))
    schedule_counts: dict[int, int] = {}
    for schedule in Schedule.all(db_path=db_path, owner_user_id=owner_scope):
        raw_group_id = (schedule.params or {}).get("target_group_id")
        try:
            group_id = int(raw_group_id)
        except (TypeError, ValueError):
            continue
        schedule_counts[group_id] = schedule_counts.get(group_id, 0) + 1
    return render_template(
        "inventory.html",
        groups=groups,
        criticality_levels=CRITICALITY_LEVELS,
        criticality_labels=CRITICALITY_LABELS,
        schedule_counts=schedule_counts,
        show_all=show_all,
        can_create_group=current_user.has_role("admin", "operator"),
    )


def register(flask_app) -> None:
    app.apply(flask_app)
