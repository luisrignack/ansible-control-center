# tests/test_cli_admin.py
# El CLI init-admin es una acción explícita: debe aplicar el rol pedido.
from __future__ import annotations

import app as app_module
from models.user import User


def test_init_admin_promotes_existing_local_user(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(app_module, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update(
        "viewer", "old-password-123", role="viewer", db_path=db_path
    )

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
