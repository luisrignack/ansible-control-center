# tests/test_tapiz_2026_11_07.py
# Pruebas de la tarjeta "Tapiz oficial": catálogo, validación de imágenes,
# selección del playbook y persistencia vía dconf.
from __future__ import annotations

import base64
import io
import json
from pathlib import Path

import pytest
import yaml

import app as app_module
import services.ansible_runner as runner
import services.wallpaper as wallpaper_module
from acc import runtime
from models.user import User
from services.task_catalog import get_task
from services.wallpaper import (
    detect_image_type,
    remove_wallpaper,
    save_wallpaper,
    wallpaper_dir,
)

ROOT = Path(__file__).resolve().parent.parent
PLAYBOOK = ROOT / "ansible" / "tapiz.yml"
INSPECT_PLAYBOOK = ROOT / "ansible" / "estado_tapiz.yml"
SESSION_SCRIPT = ROOT / "ansible" / "files" / "aplicar_tapiz_sesiones.sh"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _Upload:
    def __init__(self, raw: bytes, filename: str = "fondo.png"):
        self._raw = raw
        self.filename = filename

    def read(self, size: int = -1) -> bytes:
        return self._raw if size < 0 else self._raw[:size]


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr(runtime, "get_db_path", lambda: db_path)
    app_module.init_db()
    User.create_or_update("admin", "admin-password-123", db_path=db_path)
    app_module.app.config.update(TESTING=True)
    with app_module.app.test_client() as test_client:
        yield test_client


def _csrf_token(client) -> str:
    with client.session_transaction() as sess:
        token = sess.get("_csrf_token")
    if not token:
        client.get("/login")
        with client.session_transaction() as sess:
            token = sess.get("_csrf_token")
    return token


def _post(client, url, data=None, **kwargs):
    payload = dict(data or {})
    payload.setdefault("csrf_token", _csrf_token(client))
    return client.post(url, data=payload, **kwargs)


def _login(client):
    return _post(
        client,
        "/login",
        {"username": "admin", "password": "admin-password-123"},
    )


# ---------------------------------------------------------------------------
# Catálogo
# ---------------------------------------------------------------------------
def test_tapiz_card_is_active_and_schedulable():
    task = get_task("tapiz")
    assert task is not None
    assert task["status"] == "active"
    assert task["actions"] == ["WALLPAPER"]
    assert task["platforms"] == ["linux"]
    assert task["schedulable"] is True
    assert task["playbook"] == "tapiz.yml"


def test_tapiz_form_renders_image_upload(client):
    _login(client)
    body = client.get("/tarea/tapiz").get_data(as_text=True)
    assert 'name="wallpaper_file"' in body
    assert 'accept="image/png,image/jpeg,image/webp,image/gif,image/bmp"' in body
    assert 'value="WALLPAPER"' in body
    assert "Informe" in body or "informe" in body


def test_tapiz_form_renders_local_preview(client):
    _login(client)
    body = client.get("/tarea/tapiz").get_data(as_text=True)
    assert 'id="wallpaperPreviewField"' in body
    assert 'id="wallpaperPreviewImg"' in body
    assert 'id="wallpaperPreviewName"' in body
    assert "Vista previa del tapiz" in body
    # La miniatura se genera en el navegador y no requiere subir el archivo.
    assert "FileReader" in body
    assert "readAsDataURL" in body


# ---------------------------------------------------------------------------
# Validación de imágenes
# ---------------------------------------------------------------------------
def test_detect_image_type_by_signature():
    assert detect_image_type(PNG_BYTES) == ("png", "image/png")
    assert detect_image_type(b"\xff\xd8\xff\xe0" + b"\x00" * 8) == (
        "jpg",
        "image/jpeg",
    )
    assert detect_image_type(b"RIFF\x00\x00\x00\x00WEBP") == (
        "webp",
        "image/webp",
    )
    assert detect_image_type(b"GIF89a" + b"\x00" * 8) == ("gif", "image/gif")
    assert detect_image_type(b"BM" + b"\x00" * 8) == ("bmp", "image/bmp")
    # SVG y ejecutables no son imágenes raster admitidas.
    assert detect_image_type(b"<svg xmlns='http://www.w3.org/2000/svg'>") is None
    assert detect_image_type(b"MZ\x90\x00") is None


def test_save_wallpaper_persists_private_file(tmp_path):
    saved = save_wallpaper(_Upload(PNG_BYTES), data_dir=tmp_path)
    path = Path(saved["path"])
    assert path.is_file()
    assert path.parent == wallpaper_dir(tmp_path)
    assert path.read_bytes() == PNG_BYTES
    assert saved["mime"] == "image/png"
    assert saved["name"] == "fondo.png"
    assert (path.stat().st_mode & 0o777) == 0o600
    remove_wallpaper(str(path), data_dir=tmp_path)
    assert not path.exists()


def test_save_wallpaper_rejects_non_image(tmp_path):
    with pytest.raises(ValueError, match="no es una imagen admitida"):
        save_wallpaper(_Upload(b"<svg/>", "fondo.svg"), data_dir=tmp_path)


def test_save_wallpaper_rejects_oversized_file(tmp_path, monkeypatch):
    monkeypatch.setattr(wallpaper_module, "MAX_CONTENT_LENGTH", 10)
    with pytest.raises(ValueError, match="supera"):
        save_wallpaper(_Upload(PNG_BYTES), data_dir=tmp_path)


# ---------------------------------------------------------------------------
# Formulario y ejecución
# ---------------------------------------------------------------------------
def test_tapiz_form_stores_image_and_enqueues_wallpaper(client, monkeypatch):
    _login(client)
    calls = []

    class FakeQueue:
        def submit(self, queue_task_id, func, /, *args, **kwargs):
            calls.append((queue_task_id, func, args, kwargs))

    monkeypatch.setattr(runtime, "get_job_queue", lambda: FakeQueue())

    response = _post(
        client,
        "/tareas",
        {
            "operation_id": "op-tapiz-valida",
            "task": "tapiz",
            "target_mode": "manual",
            "target_ip": "127.0.0.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "WALLPAPER",
            "mode": "execute",
            "ntp_policy": "keep",
            "password_file": (io.BytesIO(b"clave\n"), "p.txt"),
            "wallpaper_file": (io.BytesIO(PNG_BYTES), "fondo.png"),
        },
        content_type="multipart/form-data",
        follow_redirects=False,
    )

    assert response.status_code == 302
    assert len(calls) == 1
    kwargs = calls[0][3]
    assert kwargs["wallpaper_path"]
    assert Path(kwargs["wallpaper_path"]).is_file()
    assert kwargs["wallpaper_uri"].startswith(
        "file:///usr/local/share/backgrounds/acc-tapiz."
    )
    assert kwargs["wallpaper_name"] == "fondo.png"
    # La imagen se borra manualmente: la cola falsa no ejecuta el lote.
    remove_wallpaper(kwargs["wallpaper_path"])


def test_tapiz_form_rejects_non_image(client):
    _login(client)
    response = _post(
        client,
        "/tareas",
        {
            "operation_id": "op-tapiz-invalida",
            "task": "tapiz",
            "target_mode": "manual",
            "target_ip": "127.0.0.1",
            "admin_user": "root",
            "password_origin": "temporal",
            "action": "WALLPAPER",
            "mode": "execute",
            "ntp_policy": "keep",
            "password_file": (io.BytesIO(b"clave\n"), "p.txt"),
            "wallpaper_file": (io.BytesIO(b"<svg/>"), "fondo.svg"),
        },
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "no es una imagen admitida" in body


def test_would_change_compares_wallpaper_uri():
    uri = "file:///usr/local/share/backgrounds/acc-tapiz.png"
    assert (
        app_module._would_change(
            {"wallpaper_uri": uri},
            "WALLPAPER",
            "fondo.png",
            "utc",
            "keep",
            False,
            wallpaper_uri=uri,
        )
        is False
    )
    assert (
        app_module._would_change(
            {"wallpaper_uri": "file:///otro.png"},
            "WALLPAPER",
            "fondo.png",
            "utc",
            "keep",
            False,
            wallpaper_uri=uri,
        )
        is True
    )


def test_apply_host_selects_wallpaper_playbook(tmp_path, monkeypatch):
    captured = {}
    state = {"wallpaper_uri": "file:///usr/local/share/backgrounds/acc-tapiz.png"}
    encoded = base64.b64encode(json.dumps(state).encode()).decode()

    class Process:
        returncode = 0
        stdout = (
            f"TAPIZ_BEFORE_B64={encoded}\n"
            f"TAPIZ_STATE_B64={encoded}\n"
            "TAPIZ_CHANGED=True\n"
            "TAPIZ_VERIFIED=True\n"
        )

    def fake_invoke(**kwargs):
        captured.update(kwargs)
        return Process()

    monkeypatch.setattr(runner, "_invoke_playbook", fake_invoke)
    monkeypatch.setattr(runner, "DRY_RUN", False)

    image = tmp_path / "fondo.png"
    image.write_bytes(PNG_BYTES)
    result = runner.apply_host(
        host="10.0.0.5",
        user="root",
        action="WALLPAPER",
        value="fondo.png",
        password="clave",
        wallpaper_path=str(image),
        wallpaper_uri=("file:///usr/local/share/backgrounds/acc-tapiz.png"),
    )

    assert captured["playbook_path"] == str(runner.WALLPAPER_APPLY_PLAYBOOK_FILE)
    extra = captured["extra_vars"]
    assert extra["accion"] == "WALLPAPER"
    assert extra["wallpaper_src"] == str(image)
    assert extra["wallpaper_uri"].startswith("file:///usr/local/share")
    assert result["success"] is True
    assert result["verified"] is True
    assert result["after_state"]["wallpaper_uri"].endswith("acc-tapiz.png")


# ---------------------------------------------------------------------------
# Playbook: persistencia tras reiniciar
# ---------------------------------------------------------------------------
def test_wallpaper_playbook_sets_system_default():
    data = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    tasks = data[0]["tasks"]
    names = [task.get("name", "") for task in tasks]
    serialized = PLAYBOOK.read_text(encoding="utf-8")

    assert any("valor predeterminado del sistema" in name for name in names)
    assert any("dconf" in name.lower() for name in names)
    assert "/etc/dconf/db/local.d/00-acc-tapiz" in serialized
    assert "picture-uri" in serialized
    assert "picture-uri-dark" in serialized
    assert "TAPIZ_VERIFIED" in serialized
    assert "TAPIZ_ERRORS" in serialized
    assert SESSION_SCRIPT.is_file()
    assert "gsettings set org.gnome.desktop.background picture-uri" in (
        SESSION_SCRIPT.read_text(encoding="utf-8")
    )


def test_wallpaper_inspect_playbook_is_read_only():
    serialized = INSPECT_PLAYBOOK.read_text(encoding="utf-8")
    assert "TAPIZ_STATE_B64" in serialized
    assert "dconf update" not in serialized
    assert "ansible.builtin.copy" not in serialized
