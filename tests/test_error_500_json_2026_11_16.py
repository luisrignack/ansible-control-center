# tests/test_error_500_json_2026_11_16.py
# El error interno respeta el formato que espera el cliente.
#
# Igual que 400/403/404/429, una llamada ``fetch``/API recibe JSON en el 500.
# El mensaje es genérico: el detalle técnico solo va al log del servidor.
from __future__ import annotations

import app as app_module


def test_error_500_devuelve_json_para_clientes_api():
    from acc.web import errors

    with app_module.app.test_request_context(
        "/api/x", headers={"X-Requested-With": "XMLHttpRequest"}
    ):
        response, status = errors.internal_error(RuntimeError("fallo simulado"))

    assert status == 500
    assert response.is_json
    assert response.get_json() == {
        "success": False,
        "message": "Error interno del servidor.",
    }
    # El detalle técnico no se filtra al cliente.
    assert "fallo simulado" not in response.get_data(as_text=True)


def test_error_500_sigue_siendo_html_para_navegacion():
    from acc.web import errors

    with app_module.app.test_request_context("/panel", headers={"Accept": "text/html"}):
        response, status = errors.internal_error(RuntimeError("fallo simulado"))

    assert status == 500
    assert isinstance(response, str)
    assert "500" in response
