"""Manipuladores de error HTML/JSON.
"""

from __future__ import annotations

import logging

from flask import jsonify, make_response, render_template, request

from acc.security import _wants_json

logger = logging.getLogger("acc")


from acc.web.registry import RouteRegistry

app = RouteRegistry()


@app.errorhandler(400)
def bad_request(error):
    message = getattr(error, "description", None) or "Petición no válida."
    if _wants_json():
        return jsonify({"success": False, "message": message}), 400
    return render_template("400.html", message=message), 400


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(403)
def forbidden(_error):
    if _wants_json():
        return jsonify({"success": False, "message": "Acceso denegado."}), 403
    return render_template("403.html"), 403


@app.errorhandler(429)
def too_many_requests(error):
    message = getattr(error, "description", None) or "Demasiadas peticiones."
    retry_after = request.headers.get("Retry-After", "60")
    if _wants_json():
        return jsonify({"success": False, "message": message}), 429
    response = make_response(
        render_template("429.html", message=message, retry_after=retry_after), 429
    )
    response.headers["Retry-After"] = str(retry_after)
    return response


@app.errorhandler(500)
def internal_error(error):
    logger.error("Error interno: %s", error)
    return render_template("500.html"), 500

def register(flask_app) -> None:
    app.apply(flask_app)

