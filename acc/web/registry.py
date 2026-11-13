"""Registro de rutas y hooks para los modulos web.

Los modulos de ``acc.web`` declaran sus rutas con los mismos decoradores que
ofrece Flask (``@app.route``, ``@app.before_request``...), pero sobre una
instancia de :class:`RouteRegistry` que solo recuerda las declaraciones. Al
final del modulo, ``register(flask_app)`` las aplica a la aplicación real
conservando el nombre de *endpoint* original (el de la función), de modo que
``url_for`` y los enlaces de las plantillas siguen funcionando igual.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class RouteRegistry:
    """Acumula rutas, hooks y manipuladores de error sin tocar Flask."""

    def __init__(self) -> None:
        self._rules: list[tuple[str, str, Callable[..., Any], list[str] | None]] = []
        self._before: list[Callable[..., Any]] = []
        self._after: list[Callable[..., Any]] = []
        self._context_processors: list[Callable[..., Any]] = []
        self._error_handlers: list[tuple[int, Callable[..., Any]]] = []

    def route(
        self, rule: str, **options: Any
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        methods = options.get("methods")

        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._rules.append((rule, func.__name__, func, methods))
            return func

        return decorator

    def before_request(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self._before.append(func)
        return func

    def after_request(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self._after.append(func)
        return func

    def context_processor(self, func: Callable[..., Any]) -> Callable[..., Any]:
        self._context_processors.append(func)
        return func

    def errorhandler(
        self, code: int
    ) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
            self._error_handlers.append((code, func))
            return func

        return decorator

    def apply(self, flask_app: Any) -> None:
        """Aplica las declaraciones a la aplicación Flask."""
        for rule, endpoint, view_func, methods in self._rules:
            flask_app.add_url_rule(
                rule, endpoint=endpoint, view_func=view_func, methods=methods
            )
        for func in self._before:
            flask_app.before_request(func)
        for func in self._after:
            flask_app.after_request(func)
        for func in self._context_processors:
            flask_app.context_processor(func)
        for code, handler in self._error_handlers:
            flask_app.register_error_handler(code, handler)
