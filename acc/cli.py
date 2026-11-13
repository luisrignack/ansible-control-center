"""Comandos del CLI de Flask.
"""

from __future__ import annotations

import click
from flask import Flask

from acc import runtime
from acc.runtime import (
    init_db,
)
from acc.scheduling import launch_scheduled
from acc.task_state import _purge_retention
from config import (
    ADMIN_PASSWORD_MIN_LENGTH,
    ADMIN_USERNAME,
    RETENTION_INTERVAL_HOURS,
    ROLES,
)
from models.batch import ExecutionBatch
from models.ops import HostGroup
from models.user import User
from services import scheduler
from services.ssh_host_keys import ensure_host_keys


def register_cli(flask_app: Flask) -> None:
    @flask_app.cli.command("init-db")
    def init_db_command():
        """Crea las tablas de la base de datos."""
        init_db()
        click.echo(f"Base de datos inicializada en {runtime.get_db_path()}")

    @flask_app.cli.command("init-admin")
    @click.option("--username", default=ADMIN_USERNAME, show_default=True)
    @click.option(
        "--role",
        type=click.Choice(list(ROLES)),
        default="admin",
        show_default=True,
    )
    @click.password_option(confirmation_prompt=True)
    def init_admin_command(username: str, password: str, role: str):
        """Crea o actualiza el usuario administrador."""
        if len(password) < ADMIN_PASSWORD_MIN_LENGTH:
            raise click.ClickException(
                f"La contraseña debe tener al menos {ADMIN_PASSWORD_MIN_LENGTH} caracteres."
            )
        db_path = runtime.get_db_path()
        user = User.create_or_update(username, password, db_path=db_path, role=role)
        # ``create_or_update`` no cambia el rol de una cuenta existente para
        # no degradar roles de forma implícita (p. ej. en el primer login
        # LDAPS). El CLI sí es una acción explícita del operador: aplica el
        # rol pedido y lo informa.
        if user.role != role:
            User.set_role(username, role, db_path=db_path)
            click.echo(
                f"Usuario '{username}' actualizado: rol cambiado de "
                f"'{user.role}' a '{role}'."
            )
        else:
            click.echo(f"Usuario '{username}' creado/actualizado con rol '{role}'.")

    @flask_app.cli.command("scheduler")
    @click.option(
        "--once",
        is_flag=True,
        help="Ejecuta una sola revisión de programaciones y termina.",
    )
    def scheduler_command(once: bool):
        """Ejecuta el programador de tareas en primer plano.

        Útil para un proceso/servicio dedicado (``ACC_ROLE=worker``) cuando
        el servidor web corre con ``ACC_ROLE=web``. El lock de instancia
        impide que dos procesos programen a la vez.
        """
        db_path = str(runtime.get_db_path())
        if once:
            launched = scheduler.scan_once(db_path, launch_scheduled)
            click.echo(f"Programaciones lanzadas: {launched}")
            return
        thread = scheduler.start(
            launch_scheduled,
            lambda: db_path,
            maintenance=_purge_retention,
            maintenance_interval=RETENTION_INTERVAL_HOURS * 3600,
        )
        if thread is None:
            raise click.ClickException(
                "El scheduler no se inició: revisa ACC_SCHEDULER_ENABLED, "
                "ACC_ROLE o si otro proceso ya tiene el lock."
            )
        click.echo("Scheduler en ejecución (Ctrl+C para detener).")
        try:
            while thread.is_alive():
                thread.join(timeout=1)
        except KeyboardInterrupt:
            scheduler.stop()
            click.echo("Scheduler detenido.")

    @flask_app.cli.command("reconcile-batches")
    def reconcile_batches_command():
        """Marca como error los lotes que quedaron en curso."""
        count = ExecutionBatch.reconcile_orphans(db_path=runtime.get_db_path())
        click.echo(f"Lotes huérfanos reconciliados: {count}")

    @flask_app.cli.command("sync-known-hosts")
    def sync_known_hosts_command():
        """Precarga en known_hosts las huellas SSH de todo el inventario.

        Es opcional: con ``ACC_SSH_HOST_KEYS_AUTO=1`` cada ejecución ya
        aprovisiona las huellas que falten, incluidas IPs nuevas. Este comando
        sirve para dejar el despliegue listo de una vez.
        """
        db_path = runtime.get_db_path()
        hosts = []
        for group in HostGroup.all(db_path=db_path):
            if (group.connection or "ssh") != "ssh":
                continue
            for inv_host in HostGroup.hosts_of(group.id, db_path=db_path):
                hosts.append(inv_host.address)
        hosts = sorted(set(hosts))
        if not hosts:
            click.echo("No hay hosts SSH en el inventario.")
            return

        status = ensure_host_keys(hosts)
        ok = sum(1 for value in status.values() if value)
        click.echo(f"Huellas disponibles: {ok}/{len(status)}")
        missing = sorted(host for host, value in status.items() if not value)
        if missing:
            click.echo("Sin huella (host caído o SSH no accesible):")
            for host in missing:
                click.echo(f"  - {host}")
            raise click.ClickException("No se pudieron obtener todas las huellas SSH.")
