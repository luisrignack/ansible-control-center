# tests/test_services.py
# Tests unitarios para los servicios de la aplicación
import os
import sys
import tempfile
import hashlib

# Asegurar que el directorio padre esté en el path para imports de proyecto
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from unittest.mock import patch, MagicMock, mock_open

from models.password import PasswordRecord
from models.execution import ExecutionRecord


class TestPasswordModel(unittest.TestCase):
    """Tests para el modelo de PasswordRecord."""

    def setUp(self):
        """Configurar base de datos temporal antes de cada test."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        PasswordRecord.init_db(self.db_path)

    def tearDown(self):
        """Limpiar base de datos después de cada test."""
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        # Limpiar file descriptor si existe
        try:
            os.unlink(self.db_path)
        except:
            pass

    def test_init_db_creates_table(self):
        """Test que init_db crea la tabla correctamente."""
        self.assertTrue(os.path.exists(self.db_path))

    def test_store_and_retrieve_password(self):
        """Test almacenar y recuperar una contraseña."""
        record = PasswordRecord.store_password(
            password="test_pass_123", origin="manual", db_path=self.db_path
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.id, 1)
        # Hash SHA256 de "test_pass_123" primeros 8 chars
        self.assertEqual(record.password_hash, "a126d949")

        # Recuperar
        records = PasswordRecord.get_all_passwords(limit=10, db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].password_hash, "a126d949")

    def test_delete_password(self):
        """Test eliminar una contraseña."""
        # Crear primera
        rec1 = PasswordRecord.store_password(
            password="pass_1", origin="manual", db_path=self.db_path
        )
        # Crear segunda
        rec2 = PasswordRecord.store_password(
            password="pass_2", origin="file", db_path=self.db_path
        )

        # Eliminar la primera
        success = PasswordRecord.delete_password(rec1.id, db_path=self.db_path)
        self.assertTrue(success)

        # Verificar que solo queda una
        records = PasswordRecord.get_all_passwords(limit=10, db_path=self.db_path)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].origin, "file")

    def test_get_password_count(self):
        """Test contar contraseñas."""
        count = PasswordRecord.get_password_count(db_path=self.db_path)
        self.assertEqual(count, 0)

        PasswordRecord.store_password(
            password="pass_1", origin="manual", db_path=self.db_path
        )
        count = PasswordRecord.get_password_count(db_path=self.db_path)
        self.assertEqual(count, 1)


class TestExecutionModel(unittest.TestCase):
    """Tests para el modelo ExecutionRecord."""

    def setUp(self):
        """Configurar base de datos temporal antes de cada test."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        ExecutionRecord.init_db(self.db_path)

    def tearDown(self):
        """Limpiar base de datos después de cada test."""
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        try:
            os.unlink(self.db_path)
        except:
            pass

    def test_init_db_creates_tables(self):
        """Test que init_db crea ambas tablas."""
        self.assertTrue(os.path.exists(self.db_path))

    def test_record_execution(self):
        """Test registrar una ejecución."""
        record = ExecutionRecord.record_execution(
            action="TZ",
            value="America/Argentina/Buenos_Aires",
            host="192.168.1.10",
            result="success",
            password_hash="a1b2c3d4",
            password_origin="manual",
            db_path=self.db_path,
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.action, "TZ")
        self.assertEqual(record.host, "192.168.1.10")
        self.assertEqual(record.result, "success")

    def test_get_all_executions(self):
        """Test obtener ejecuciones."""
        # Registrar una ejecución
        ExecutionRecord.record_execution(
            action="TIME",
            value="2024-01-15T10:30:00",
            host="192.168.1.11",
            result="failure",
            password_hash="d4c3b2a1",
            password_origin="file",
            db_path=self.db_path,
        )

        executions = ExecutionRecord.get_all_executions(limit=10, db_path=self.db_path)
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0].action, "TIME")
        self.assertEqual(executions[0].result, "failure")

    def test_get_recent_executions(self):
        """Test obtener ejecuciones recientes."""
        # Registrar ejecución
        ExecutionRecord.record_execution(
            action="TZ",
            value="Europe/Madrid",
            host="192.168.1.12",
            result="success",
            password_hash="1234abcd",
            password_origin="manual",
            db_path=self.db_path,
        )

        recent = ExecutionRecord.get_recent_executions(hours=168, db_path=self.db_path)
        self.assertEqual(len(recent), 1)


class TestModelsIntegration(unittest.TestCase):
    """Tests de integración entre modelos."""

    def setUp(self):
        """Configurar base de datos temporal."""
        self.db_fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(self.db_fd)
        PasswordRecord.init_db(self.db_path)
        ExecutionRecord.init_db(self.db_path)

    def tearDown(self):
        """Limpiar después del test."""
        if os.path.exists(self.db_path):
            os.unlink(self.db_path)
        try:
            os.unlink(self.db_path)
        except:
            pass

    def test_end_to_end_password_storage(self):
        """Test flujo completo: almacenar contraseña y registrar ejecución."""
        # Almacenar contraseña
        password_record = PasswordRecord.store_password(
            password="secret_pass", origin="manual", db_path=self.db_path
        )
        self.assertEqual(password_record.id, 1)

        # Registrar ejecución con hash de esa contraseña
        password_hash_full = hashlib.sha256(
            "secret_pass".encode("utf-8")
        ).hexdigest()
        password_hash_short = password_hash_full[:8]

        exec_record = ExecutionRecord.record_execution(
            action="TZ",
            value="UTC",
            host="192.168.1.10",
            result="success",
            password_hash=password_hash_short,
            password_origin="manual",
            db_path=self.db_path,
        )
        self.assertIsNotNone(exec_record)


if __name__ == "__main__":
    unittest.main()