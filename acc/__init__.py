"""Ansible Control Center — paquete de la aplicación Flask.

``app.py`` se mantiene como fachada delgada: crea la aplicación y reexporta los
símbolos públicos para conservar la compatibilidad con ``flask --app app``,
``gunicorn app:app`` y las pruebas existentes.
"""
