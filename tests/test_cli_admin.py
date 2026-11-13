# tests/test_cli_admin.py
# El CLI init-admin es una acción explícita: debe aplicar el rol pedido.
from __future__ import annotations

import app as app_module
from acc import cli as acc_cli
from acc import runtime
from models.ops import HostGroup, InventoryHost
from models.user import User


def test_init_admin_promotes_existing_local_user(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("viewer", "old-password-123", role="viewer", db_path=db_path)

    runner = app_module.app.test_cli_runner()
    result = runner.invoke(
        args=["init-admin", "--username", "viewer", "--role", "admin"],
        input="new-password-123\nnew-password-123\n",
    )

    assert result.exit_code == 0, result.output
    assert "rol cambiado de 'viewer' a 'admin'" in result.output
    user = User.get_by_username("viewer", db_path=db_path)
    assert user.role == "admin"
    assert user.check_password("new-password-123")


def test_sync_known_hosts_scans_inventory_ssh_hosts(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()

    linux = HostGroup.create("linux", connection="ssh", db_path=db_path)
    InventoryHost.add(linux.id, "10.0.0.10", db_path=db_path)
    InventoryHost.add(linux.id, "10.0.0.11", db_path=db_path)
    windows = HostGroup.create("windows", os_flavor="windows", db_path=db_path)
    InventoryHost.add(windows.id, "10.0.1.20", db_path=db_path)

    seen = {}

    def fake_ensure(hosts):
        seen["hosts"] = list(hosts)
        return {host: True for host in hosts}

    monkeypatch.setattr(acc_cli, "ensure_host_keys", fake_ensure)
    runner = app_module.app.test_cli_runner()
    result = runner.invoke(args=["sync-known-hosts"])

    assert result.exit_code == 0, result.output
    assert seen["hosts"] == ["10.0.0.10", "10.0.0.11"]
    assert "Huellas disponibles: 2/2" in result.output
