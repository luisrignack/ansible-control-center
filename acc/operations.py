"""Analisis y validacion de formularios de operacion.
"""

from __future__ import annotations

import ipaddress
import os
import re
from datetime import datetime
from typing import Any

from flask import request

from acc import runtime
from acc.runtime import (
    get_vault_file,
)
from acc.security import _can_use_group
from config import (
    APT_MIN_FREE_GB,
    APT_MIN_FREE_GB_CEILING,
    APT_MIN_FREE_GB_FLOOR,
    DEFAULT_NTP_SERVERS,
    MAX_CONTENT_LENGTH,
    MAX_HOSTS,
    MAX_NTP_SERVERS,
    MAX_PASSWORD_LENGTH,
    MAX_PASSWORDS,
    NTP_POLICIES,
    OS_PASSWORD_DEFAULT_USER,
    OS_PASSWORD_MAX_LENGTH,
    OS_PASSWORD_MIN_LENGTH,
    TIME_REQUIRE_NTP_OFF,
    TIME_VERIFY_TOLERANCE,
)
from models.ops import HostGroup
from services import task_catalog
from services.host_resolver import pin_target
from services.time_utils import (
    LOCAL_TIME_AMBIGUOUS,
    LOCAL_TIME_MISSING,
    local_time_epoch,
    local_time_status,
    timezone_exists,
    utc_time_epoch,
)
from services.vault_manager import VaultManager
from services.wallpaper import (
    remote_wallpaper_uri,
    remove_wallpaper,
    save_wallpaper,
)


def read_temporal_passwords(uploaded) -> tuple[list[str], str | None]:
    """Lee un .txt subido y devuelve contraseñas normalizadas o un error.

    El archivo se procesa en memoria: no se guarda en disco.
    """
    if uploaded is None or not uploaded.filename:
        return [], "Selecciona un archivo .txt con las contraseñas candidatas."

    extension = os.path.splitext(uploaded.filename)[1].lower()
    if extension != ".txt":
        return [], "El modo temporal requiere un archivo con extensión .txt."

    raw = uploaded.read(MAX_CONTENT_LENGTH + 1)
    if len(raw) > MAX_CONTENT_LENGTH:
        return [], "El archivo de contraseñas es demasiado grande."

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return [], "El archivo debe estar codificado en UTF-8."

    passwords: list[str] = []
    seen = set()
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        if len(candidate) > MAX_PASSWORD_LENGTH:
            return [], (
                f"Una contraseña supera el máximo de {MAX_PASSWORD_LENGTH} caracteres."
            )
        if candidate not in seen:
            seen.add(candidate)
            passwords.append(candidate)
            if len(passwords) > MAX_PASSWORDS:
                return [], f"Máximo {MAX_PASSWORDS} contraseñas por archivo."

    if not passwords:
        return [], "El archivo no contiene contraseñas válidas."

    return passwords, None


_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+\-]+(?:/[A-Za-z0-9_+\-]+)*$")


_OS_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9\-]{0,61}[A-Za-z0-9])?)*\.?$"
)


VALID_ACTIONS = {"TZ", "TIME", "WALLPAPER", "APT", "PASSWORD", "FIREWALL"}


def _form_bool(name: str, form_data=None) -> bool:
    """Interpreta un checkbox/valor booleano del formulario."""
    source = request.form if form_data is None else form_data
    return source.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "si",
        "sí",
    }


def _fold_value(raw: str) -> int | None:
    """Normaliza la desambiguación DST: ``auto``, ``0`` o ``1``."""
    value = (raw or "auto").strip().lower()
    if value in {"0", "1"}:
        return int(value)
    return None


def _parse_ntp_servers(raw: str) -> tuple[list[str], str | None]:
    """Valida y normaliza una lista de servidores NTP.

    Admite IPs (v4/v6) y nombres DNS separados por comas, espacios o saltos
    de línea. Devuelve ``(servidores, error)``.
    """
    servers: list[str] = []
    for token in re.split(r"[\s,;]+", raw or ""):
        token = token.strip()
        if not token:
            continue
        if len(token) > 253:
            return [], "Un servidor NTP es demasiado largo."
        try:
            ipaddress.ip_address(token)
        except ValueError:
            if not _HOSTNAME_RE.fullmatch(token):
                return [], f"Servidor NTP inválido: {token}"
        if token not in servers:
            servers.append(token)
        if len(servers) > MAX_NTP_SERVERS:
            return [], f"Máximo {MAX_NTP_SERVERS} servidores NTP."
    return servers, None


def _parse_min_free_gb(raw: str) -> tuple[float | None, str | None]:
    """Valida el umbral de espacio libre (GiB) de la tarjeta APT.

    Un valor vacío usa el umbral del servidor (``ACC_APT_MIN_FREE_GB``). Los
    límites evitan tanto umbrales absurdos como valores negativos.
    """
    text = (raw or "").strip()
    if not text:
        return float(APT_MIN_FREE_GB), None
    try:
        value = float(text.replace(",", "."))
    except ValueError:
        return None, "El umbral de espacio libre debe ser un número en GB."
    if value < APT_MIN_FREE_GB_FLOOR or value > APT_MIN_FREE_GB_CEILING:
        return None, (
            "El umbral de espacio libre debe estar entre "
            f"{APT_MIN_FREE_GB_FLOOR:g} y {APT_MIN_FREE_GB_CEILING:g} GB."
        )
    return value, None


def _parse_target_user(raw: str) -> tuple[str, str | None]:
    """Normaliza el usuario del sistema al que se cambiará la contraseña."""
    user = (raw or "").strip() or OS_PASSWORD_DEFAULT_USER
    if not _OS_USERNAME_RE.fullmatch(user):
        return "", (
            "El usuario del sistema no es válido: usa minúsculas, números, "
            "guion, punto o guion bajo (máximo 32 caracteres)."
        )
    return user, None


_FIREWALL_STATES = {"enable", "disable"}


def _parse_firewall_value(raw: str) -> tuple[str, str | None]:
    """Normaliza la operación de firewall a ``enable``, ``disable`` o
    ``allow:<ip>``.

    Se acepta una IP suelta o una red CIDR (IPv4 o IPv6). Se rechaza la red
    universal (``0.0.0.0/0`` y ``::/0``): abrir el firewall a cualquier origen
    no es una regla de confianza y anularía el propósito de la tarjeta.
    """
    text = (raw or "").strip()
    lowered = text.lower()
    if lowered in _FIREWALL_STATES:
        return lowered, None
    if lowered.startswith("allow:"):
        candidate = text.split(":", 1)[1].strip()
        if not candidate:
            return "", "Indica la IP o red que se quiere permitir."
        try:
            network = ipaddress.ip_network(candidate, strict=False)
        except ValueError:
            return "", f"IP o red no válida: {candidate}."
        if network.prefixlen == 0:
            return "", (
                "No se permite abrir el firewall a cualquier origen "
                "(0.0.0.0/0 o ::/0). Indica una IP o red concreta."
            )
        if network.prefixlen == network.max_prefixlen:
            source = str(network.network_address)
        else:
            source = str(network)
        return f"allow:{source}", None
    if not text:
        return "", "Selecciona una operación de firewall."
    return "", "Operación de firewall no válida."


def _validate_new_password(
    password: str,
    confirm: str,
    *,
    required: bool,
) -> str | None:
    """Valida la contraseña nueva y su repetición sin recortar el secreto.

    La repetición es obligatoria para evitar errores de escritura. La
    contraseña no se registra ni se devuelve en el mensaje de error.
    """
    password = password or ""
    confirm = confirm or ""
    if not password and not confirm:
        if required:
            return "La nueva contraseña es obligatoria."
        return None
    if password != confirm:
        return "La nueva contraseña y su repetición no coinciden."
    if len(password) < OS_PASSWORD_MIN_LENGTH:
        return (
            "La nueva contraseña debe tener al menos "
            f"{OS_PASSWORD_MIN_LENGTH} caracteres."
        )
    if len(password) > OS_PASSWORD_MAX_LENGTH:
        return (
            "La nueva contraseña supera el máximo de "
            f"{OS_PASSWORD_MAX_LENGTH} caracteres."
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in password):
        return "La nueva contraseña no puede contener caracteres de control."
    return None


def _validate_operation_core(
    *,
    task: dict[str, Any] | None,
    task_id: str,
    admin_user: str,
    action: str,
    value: str,
    ntp_policy: str,
    ntp_servers_raw: str,
    platform: str = "",
    group_name: str = "",
    reject_destructive: bool = False,
    enforce_time_ntp_off: bool = True,
    for_schedule: bool = False,
    min_free_gb_raw: str = "",
    target_user_raw: str = "",
    new_password: str = "",
    new_password_confirm: str = "",
    require_password: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Reglas comunes a la ejecución manual y las programaciones.

    Centralizar esta validación evita que una programación acepte algo que el
    panel rechaza (o al revés): tarea activa, acción admitida, valor fijo,
    plataforma del grupo, zona/hora válida, política NTP y usuario.
    """
    errors: list[str] = []
    admin_user = (admin_user or "").strip()
    action = (action or "").strip().upper()
    value = (value or "").strip()
    ntp_policy = (ntp_policy or "keep").strip().lower() or "keep"

    if task is None or task.get("status") != "active":
        errors.append("Selecciona una tarea activa del catálogo.")
    else:
        if action not in (task.get("actions") or ["TZ", "TIME"]):
            errors.append(
                f"La tarea '{task.get('name', task_id)}' no admite la acción "
                f"{action}."
            )
        if task.get("fixed_value") and action == "TZ":
            value = str(task["fixed_value"])
        if reject_destructive and task.get("destructive"):
            errors.append(
                "Las tareas destructivas requieren aprobación manual; no se "
                "pueden programar."
            )
        if for_schedule and not task.get("schedulable", True):
            errors.append(
                f"La tarea '{task.get('name', task_id)}' no se puede "
                "programar: ejecútala manualmente desde su tarjeta."
            )
        platforms = task.get("platforms") or []
        if platform and platforms and platform not in platforms:
            platform_label = "Windows" if platform == "windows" else "Linux"
            objective = f"el grupo '{group_name}'" if group_name else "el objetivo"
            errors.append(
                f"La tarea '{task.get('name', task_id)}' no está disponible "
                f"para {objective} ({platform_label})."
            )

    if not admin_user:
        errors.append("El usuario administrador es obligatorio.")
    if action not in VALID_ACTIONS:
        errors.append("Acción no válida.")

    ntp_servers: list[str] = []
    min_free_gb: float | None = float(APT_MIN_FREE_GB)
    if action in {"TZ", "TIME"}:
        if ntp_policy not in NTP_POLICIES:
            errors.append("Política NTP no válida.")
        if ntp_policy == "configure":
            ntp_servers, ntp_error = _parse_ntp_servers(ntp_servers_raw)
            if ntp_error:
                errors.append(ntp_error)
            if not ntp_servers:
                ntp_servers = list(DEFAULT_NTP_SERVERS)
                if not ntp_servers:
                    errors.append(
                        "La política NTP 'configure' requiere al menos un "
                        "servidor NTP."
                    )
    else:
        # El tapiz y la actualización no tocan el reloj ni NTP: se normaliza
        # la política para no arrastrar opciones sin sentido al playbook.
        ntp_policy = "keep"

    if action == "APT":
        # ``safe`` equivale a apt-get upgrade: nunca se elimina ni se instala
        # un paquete nuevo. No se acepta otro modo desde el formulario.
        value = "safe"
        min_free_gb, min_error = _parse_min_free_gb(min_free_gb_raw)
        if min_error:
            errors.append(min_error)

    if action == "PASSWORD":
        # El valor visible del lote es el usuario del sistema, nunca el
        # secreto. La contraseña se valida aquí y se envía al playbook por el
        # fichero temporal de variables, fuera de la línea de comandos.
        value, user_error = _parse_target_user(target_user_raw)
        if user_error:
            errors.append(user_error)
        password_error = _validate_new_password(
            new_password,
            new_password_confirm,
            required=require_password,
        )
        if password_error:
            errors.append(password_error)

    if action == "FIREWALL":
        # ``value`` transporta la operación completa para que la validación,
        # la aprobación y el historial compartan un único campo:
        # ``enable``, ``disable`` o ``allow:<ip|red>``.
        value, firewall_error = _parse_firewall_value(value)
        if firewall_error:
            errors.append(firewall_error)

    if action == "TZ":
        if not value:
            errors.append("La zona horaria es obligatoria.")
        elif not _TIMEZONE_RE.fullmatch(value):
            errors.append("Formato de zona horaria inválido.")
        elif not timezone_exists(value):
            errors.append(f"Zona horaria desconocida: {value}.")
    elif action == "TIME":
        if not value:
            errors.append("La hora exacta es obligatoria.")
        else:
            try:
                parsed = datetime.strptime(value.replace(" ", "T"), "%Y-%m-%dT%H:%M")
                value = parsed.strftime("%Y-%m-%dT%H:%M")
            except ValueError:
                errors.append("Formato de hora inválido; use AAAA-MM-DDTHH:MM.")
        if enforce_time_ntp_off and ntp_policy == "keep" and TIME_REQUIRE_NTP_OFF:
            errors.append(
                "TIME con NTP en política keep no está permitido al "
                "ejecutar: el reloj podría revertirse. Usa off, restore o "
                "configure."
            )
    # ``WALLPAPER`` no valida un valor de texto: la imagen la valida y guarda
    # ``_parse_operation_form`` antes de llegar aquí.

    if errors:
        return None, errors
    return (
        {
            "admin_user": admin_user,
            "action": action,
            "value": value,
            "ntp_policy": ntp_policy,
            "ntp_servers": ntp_servers,
            "min_free_gb": min_free_gb,
        },
        [],
    )


def _validate_scheduled_params(
    task: dict[str, Any] | None,
    admin_user: str,
    action: str,
    value: str,
    ntp_policy: str,
    ntp_servers_raw: str,
    platform: str = "",
    group_name: str = "",
    min_free_gb_raw: str = "",
    target_user_raw: str = "",
    new_password: str = "",
    new_password_confirm: str = "",
    require_password: bool = False,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Valida una programación con las mismas reglas que la ejecución manual.

    Las tareas destructivas solo llegan aquí si el catálogo las marca como
    ``schedulable``: la propia programación (admin, contraseña maestra y
    ventana de mantenimiento) sustituye a la aprobación manual interactiva.
    """
    return _validate_operation_core(
        task=task,
        task_id=(task or {}).get("id", ""),
        admin_user=admin_user,
        action=action,
        value=value,
        ntp_policy=ntp_policy,
        ntp_servers_raw=ntp_servers_raw,
        platform=platform,
        group_name=group_name,
        reject_destructive=False,
        enforce_time_ntp_off=True,
        for_schedule=True,
        min_free_gb_raw=min_free_gb_raw,
        target_user_raw=target_user_raw,
        new_password=new_password,
        new_password_confirm=new_password_confirm,
        require_password=require_password,
    )


def _parse_operation_form(
    default_mode: str = "execute",
    form_data=None,
    files=None,
) -> tuple[dict[str, Any] | None, list[str]]:
    """Valida el formulario completo de vista previa/ejecución (P4).

    Devuelve un diccionario normalizado o una lista de errores. La vista previa
    y la ejecución comparten exactamente la misma validación para que lo que se
    previsualiza sea lo que después se ejecuta. ``form_data``/``files``
    permiten validar un envío reconstruido (ejecución de una aprobación ya
    concedida) sin pasar por el formulario.
    """
    submitted = request.form if form_data is None else form_data
    uploads = request.files if files is None else files
    target_mode = submitted.get("target_mode", "manual").strip().lower() or "manual"
    target_group_raw = submitted.get("target_group", "").strip()
    # Tarea del catálogo: determina permisos y si es destructiva. Para no
    # romper envíos anteriores (que no enviaban el campo oculto), si falta se
    # asume la tarea operativa por defecto del catálogo.
    task_id = submitted.get("task", "").strip()
    target = submitted.get("target_ip", "").strip()
    admin_user = submitted.get("admin_user", "").strip()
    origin = submitted.get("password_origin", "stored").strip()
    master_password = submitted.get("master_password", "").strip()
    action = submitted.get("action", "").strip().upper()
    value = submitted.get("value", "").strip()
    mode = submitted.get("mode", default_mode).strip().lower() or default_mode
    time_mode = submitted.get("time_mode", "utc").strip().lower() or "utc"
    time_zone = submitted.get("time_zone", "").strip()
    time_fold_raw = submitted.get("time_fold", "auto").strip().lower()
    ntp_policy = submitted.get("ntp_policy", "keep").strip().lower() or "keep"
    ntp_servers_raw = submitted.get("ntp_servers", "")
    min_free_gb_raw = submitted.get("min_free_gb", "")
    target_user_raw = submitted.get("target_user", "")
    new_password = submitted.get("new_password", "")
    new_password_confirm = submitted.get("new_password_confirm", "")
    sync_rtc = _form_bool("sync_rtc", submitted)
    force = _form_bool("force", submitted)

    errors: list[str] = []

    # Tarea del catálogo: determina permisos y si es destructiva. Para no
    # romper envíos anteriores (que no enviaban el campo oculto), si falta se
    # asume la tarea operativa por defecto del catálogo.
    if not task_id:
        for candidate in task_catalog.TASK_CATALOG:
            if candidate.get("status") == "active" and not candidate.get("destructive"):
                task_id = candidate["id"]
                break
    task = task_catalog.get_task(task_id) if task_id else None

    # Si el envío no trae ``action`` (clientes antiguos o un formulario de una
    # sola acción), se usa la única acción de la tarea; en su defecto, TZ.
    if task is not None and not action:
        task_actions = task.get("actions") or ["TZ", "TIME"]
        action = (
            str(task_actions[0]).strip().upper() if len(task_actions) == 1 else "TZ"
        )
    elif not action:
        action = "TZ"

    # Firewall: el formulario separa «operación» e «IP» por claridad, pero el
    # motor, el historial y la validación comparten un único campo ``value``
    # (``enable``, ``disable`` o ``allow:<ip|red>``).
    if action == "FIREWALL":
        firewall_op = submitted.get("firewall_op", "").strip().lower()
        firewall_ip = submitted.get("firewall_ip", "").strip()
        value = f"allow:{firewall_ip}" if firewall_op == "allow" else firewall_op

    # Imagen del tapiz: se valida por firma binaria y se guarda fuera de la
    # raíz web. El archivo se borra al terminar el lote (o si el envío falla).
    wallpaper_path = ""
    wallpaper_uri = ""
    wallpaper_name = ""
    if action == "WALLPAPER" and task is not None and task.get("status") == "active":
        try:
            saved_wallpaper = save_wallpaper(uploads.get("wallpaper_file"))
        except ValueError as exc:
            errors.append(str(exc))
        else:
            wallpaper_path = saved_wallpaper["path"]
            wallpaper_name = saved_wallpaper["name"]
            wallpaper_uri = remote_wallpaper_uri(saved_wallpaper["ext"])
            # El nombre del archivo es el valor visible en el lote y el PDF.
            value = wallpaper_name

    if mode not in {"preview", "execute"}:
        errors.append("Modo de operación no válido.")

    # Objetivo: manual (IP literal) o grupo del inventario persistente.
    target_group_id: int | None = None
    group_name = ""
    group_target_hosts: list[str] = []
    group_os_flavor = ""
    group_connection = ""
    if target_mode not in {"manual", "group"}:
        errors.append("Modo de objetivo no válido.")
        target_mode = "manual"
    if target_mode == "group":
        try:
            target_group_id = int(target_group_raw)
        except ValueError:
            target_group_id = None
        group = (
            HostGroup.get(target_group_id, db_path=runtime.get_db_path())
            if target_group_id is not None
            else None
        )
        if group is None:
            errors.append("Selecciona un grupo del inventario válido.")
            target_group_id = None
        elif not _can_use_group(group):
            errors.append(
                "El grupo seleccionado pertenece a otro usuario. Elige uno "
                "de tu inventario o un grupo compartido."
            )
        else:
            group_name = group.name
            group_os_flavor = group.os_flavor
            group_connection = group.connection
            # Instantánea ordenada de hosts para que una aprobación quede
            # ligada al conjunto concreto que el admin autorizó. Si el grupo
            # cambia después, el hash de la aprobación deja de coincidir.
            group_target_hosts = sorted(
                {
                    host.address
                    for host in HostGroup.hosts_of(group.id, db_path=runtime.get_db_path())
                }
            )
            if not group.host_count:
                errors.append(
                    f"El grupo '{group.name}' no tiene hosts; añádelos en el inventario."
                )
            elif group.host_count > MAX_HOSTS:
                errors.append(
                    f"El grupo '{group.name}' tiene {group.host_count} hosts "
                    f"y el máximo es {MAX_HOSTS}."
                )
        # El objetivo real se resuelve en caliente al ejecutar (los hosts del
        # grupo pueden cambiar entre el envío y el disparo del lote).
        target = f"inventory:{target_group_id}" if target_group_id is not None else ""
    else:
        target_error = None
        pinned_target, target_error = pin_target(target)
        if target_error:
            errors.append(target_error)
        else:
            # Guardar la IP literal evita que Ansible vuelva a resolver DNS.
            target = pinned_target
    # Mismas reglas que una programación: tarea, acción, valor fijo,
    # plataforma del grupo, zona/hora, NTP y usuario.
    validated, core_errors = _validate_operation_core(
        task=task,
        task_id=task_id,
        admin_user=admin_user,
        action=action,
        value=value,
        ntp_policy=ntp_policy,
        ntp_servers_raw=ntp_servers_raw,
        platform=group_os_flavor,
        group_name=group_name,
        reject_destructive=False,
        enforce_time_ntp_off=mode == "execute",
        min_free_gb_raw=min_free_gb_raw,
        target_user_raw=target_user_raw,
        new_password=new_password,
        new_password_confirm=new_password_confirm,
        require_password=action == "PASSWORD" and mode == "execute",
    )
    errors.extend(core_errors)
    if validated is not None:
        admin_user = validated["admin_user"]
        action = validated["action"]
        value = validated["value"]
        ntp_policy = validated["ntp_policy"]
        ntp_servers = list(validated["ntp_servers"])
        min_free_gb = float(validated["min_free_gb"])
    else:
        ntp_servers = []
        min_free_gb = float(APT_MIN_FREE_GB)

    if action == "TIME":
        if time_mode not in {"utc", "local"}:
            errors.append("Semántica de hora no válida (utc o local).")
        # El formato de ``value`` ya lo normalizó el validador común.

        if time_mode == "local":
            # La hora de pared necesita una zona real y resolver el hueco o el
            # solapamiento de DST. Así la vista previa y el playbook calculan
            # exactamente el mismo epoch.
            if not time_zone:
                errors.append(
                    "La zona horaria es obligatoria para la hora local (DST)."
                )
            elif not timezone_exists(time_zone):
                errors.append(f"Zona horaria desconocida: {time_zone}.")
            else:
                status, message = local_time_status(
                    value,
                    time_zone,
                    fold=_fold_value(time_fold_raw),
                )
                if (
                    status == LOCAL_TIME_MISSING
                    or status == LOCAL_TIME_AMBIGUOUS
                    or status not in {"ok"}
                ):
                    errors.append(message)
        else:
            # En UTC no se usa la zona del host; se calcula el instante en
            # Python para que la comprobación sea determinista.
            time_zone = ""
            time_fold_raw = "auto"

        if time_mode in {"utc", "local"} and not errors:
            fold = _fold_value(time_fold_raw)
            if time_mode == "local":
                # ``auto`` solo puede resolverse a 0 cuando la hora no es
                # ambigua (las ambiguas se rechazan arriba). Fijamos el valor
                # para que vista previa y ejecución compartan el mismo fold.
                fold = 0 if fold is None else fold
                time_fold_raw = str(fold)
            target_epoch = (
                utc_time_epoch(value)
                if time_mode == "utc"
                else local_time_epoch(value, time_zone, fold or 0)
            )
            if target_epoch is None:
                errors.append("No se pudo calcular el instante objetivo.")
        else:
            target_epoch = None
    else:
        # La semántica UTC/local solo aplica a TIME.
        time_mode = "utc"
        time_zone = ""
        time_fold_raw = "auto"
        target_epoch = None

    temporal_passwords: list[str] = []
    vault_file: str | None = None
    if origin not in {"stored", "temporal"}:
        errors.append("Origen de contraseñas no válido.")
    elif origin == "stored":
        try:
            vault_file = get_vault_file()
        except RuntimeError as exc:
            errors.append(str(exc))

        if vault_file and not os.path.exists(vault_file):
            errors.append("No existe el archivo del vault (passwords_vault.yml).")
        if not master_password:
            errors.append("La contraseña maestra del Vault es obligatoria.")
        elif vault_file and os.path.exists(vault_file):
            valid, message = VaultManager.validate_master_password(
                vault_file=vault_file,
                test_password=master_password,
            )
            if not valid:
                errors.append(message or "Contraseña maestra incorrecta.")
    else:
        temporal_passwords, file_error = read_temporal_passwords(
            uploads.get("password_file")
        )
        if file_error:
            errors.append(file_error)

    if errors:
        # Nunca dejar imágenes huérfanas si el formulario no llega a ejecutarse.
        if wallpaper_path:
            remove_wallpaper(wallpaper_path)
        return None, errors

    return (
        {
            "target": target,
            "target_mode": target_mode,
            "target_group_id": target_group_id,
            "group_name": group_name,
            "group_os_flavor": group_os_flavor,
            "group_connection": group_connection,
            "target_hosts": group_target_hosts,
            "task_id": task_id,
            "task_name": task.get("name", task_id) if task else task_id,
            "admin_user": admin_user,
            "origin": origin,
            "master_password": master_password,
            "action": action,
            "value": value,
            "target_user": value if action == "PASSWORD" else "",
            "new_password": new_password if action == "PASSWORD" else "",
            "mode": mode,
            "time_mode": time_mode,
            "time_zone": time_zone,
            "time_fold": _fold_value(time_fold_raw),
            "target_epoch": target_epoch,
            "ntp_policy": ntp_policy,
            "ntp_servers": ntp_servers,
            "sync_rtc": sync_rtc,
            "force": force,
            "min_free_gb": min_free_gb,
            "temporal_passwords": temporal_passwords,
            "wallpaper_path": wallpaper_path,
            "wallpaper_uri": wallpaper_uri,
            "wallpaper_name": wallpaper_name,
        },
        [],
    )


def _state_bool(value: Any) -> bool:
    """Interpreta un booleano del playbook aunque llegue serializado.

    Ansible con Jinja nativa conserva ``bool``; con versiones anteriores un
    ``set_fact`` entrecomillado puede producir ``"True"``/``"False"``. Un
    ``bool("False")`` de Python sería verdadero, así que se normaliza aquí.
    """
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "si", "sí", "on"}
    return bool(value)


def _would_change(
    state: dict[str, Any] | None,
    action: str,
    value: str,
    time_mode: str,
    ntp_policy: str,
    force: bool,
    ntp_servers: list[str] | None = None,
    target_epoch: int | None = None,
    wallpaper_uri: str = "",
) -> bool | None:
    """Estima si hace falta cambiar algo según el estado leído (vista previa).

    Devuelve ``None`` si el estado no permite decidirlo (por ejemplo, un host
    inaccesible o una salida incompleta del playbook de inspección).

    Para ``TIME`` se compara el epoch calculado en Python (``target_epoch``),
    no la cadena local del host: así la vista previa y el playbook usan el
    mismo instante incluso en cambios de horario de verano.
    """
    if force:
        return True
    if not isinstance(state, dict):
        return None

    if action == "WALLPAPER":
        current_uri = str(state.get("wallpaper_uri") or "").strip()
        return current_uri != (wallpaper_uri or "").strip()

    if action == "APT":
        # Una estación marcada para migración o sin espacio no se tocará.
        if state.get("attention"):
            return False
        try:
            pending = int(state.get("pending") or 0)
        except (TypeError, ValueError):
            return None
        return pending > 0

    if action == "PASSWORD":
        # El cambio de contraseña siempre se aplica si el usuario existe; la
        # inspección ya falla cuando no existe.
        return bool(state.get("exists"))

    if action == "FIREWALL":
        mode, _, source = str(value or "").partition(":")
        active = _state_bool(state.get("active"))
        if mode == "enable":
            return not active
        if mode == "disable":
            return active
        if mode == "allow":
            sources = state.get("allowed_sources") or []
            if not isinstance(sources, list):
                return None
            return source not in {str(item) for item in sources}
        return None

    changed = False
    if action == "TZ":
        changed = (state.get("timezone") or "") != value
    elif action == "TIME":
        if target_epoch is None:
            return None
        try:
            current_epoch = int(state.get("epoch") or 0)
        except (TypeError, ValueError):
            return None
        changed = current_epoch == 0 or (
            abs(current_epoch - int(target_epoch)) > TIME_VERIFY_TOLERANCE
        )

    if ntp_policy == "off":
        changed = changed or _state_bool(state.get("ntp"))
    elif ntp_policy == "configure":
        current_servers = state.get("ntp_servers")
        desired_servers = {str(server) for server in (ntp_servers or [])}
        # El playbook escribe los servidores deseados en su propio fichero,
        # pero no elimina los que ya existan en la configuración del sistema.
        # Exigir igualdad exacta hacía que la vista previa siguiera indicando
        # "cambiará" después de aplicar. Basta con que estén todos los deseados.
        servers_match = isinstance(current_servers, list) and desired_servers.issubset(
            {str(server) for server in current_servers}
        )
        changed = changed or not _state_bool(state.get("ntp")) or not servers_match
    # ``restore`` devuelve NTP a su estado previo, así que no añade cambios
    # netos por sí mismo.

    return changed


def _batch_message(batch: Any) -> str:
    """Resumen legible del estado de un lote."""
    if batch is None:
        return "Lote sin resultado"
    if batch.status == "preview":
        return (
            f"Vista previa: {batch.preview_count} host(s) inspeccionado(s), "
            f"{batch.error_count} con error, {batch.skipped_count} omitido(s)"
        )
    prefix = ""
    if getattr(batch, "dry_run", False):
        prefix = "SIMULACIÓN (sin cambios reales): "
    return (
        prefix + f"Lote {batch.status}: {batch.success_count} éxito(s), "
        f"{batch.failure_count} fallo(s), {batch.skipped_count} omitido(s), "
        f"{batch.error_count} error(es), "
        f"{batch.attention_count} requieren atención"
    )
