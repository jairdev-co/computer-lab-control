# Automated tests for the laboratory control system.
#
# They need an empty PostgreSQL database whose name ends in "_test" (see the
# README, "Pruebas"). The connection is set here, before importing the app, so
# the values in .env (the real database and the Gmail account) are never used.
import os
import re
import unittest
import zipfile
from datetime import datetime, time
from io import BytesIO
from unittest import mock

TEST_DB = {
    "DB_HOST": os.environ.get("TEST_DB_HOST", "127.0.0.1"),
    "DB_PORT": os.environ.get("TEST_DB_PORT", "55432"),
    "DB_NAME": os.environ.get("TEST_DB_NAME", "labs_test"),
    "DB_USER": os.environ.get("TEST_DB_USER", "postgres"),
    "DB_PASSWORD": os.environ.get("TEST_DB_PASSWORD", "test"),
}
if not TEST_DB["DB_NAME"].endswith("_test"):
    raise SystemExit("TEST_DB_NAME must end in '_test': the tests empty the database.")
# load_dotenv() never overrides variables that already exist
os.environ.update(TEST_DB, EMAIL_USER="", EMAIL_PASS="", SECRET_KEY="test-secret")

from werkzeug.security import generate_password_hash  # noqa: E402

import app as labs  # noqa: E402

LAB = labs.LABORATORIOS[0]
VALID_REPORT = {
    "identificador_laboratorio": LAB,
    "nombre_docente": "María Pérez",
    "correo_electronico": "maria.perez@unicesmag.edu.co",
    "programa": "Ingeniería de Sistemas",
    "hora_ingreso": "07:00",
    "hora_salida": "09:00",
    "observacion": "El computador 5 no enciende",
}
ADMIN_PASSWORD = "clave-admin-1"


def run_sql(query, params=None, fetch=False):
    with labs.get_db_cursor() as cursor:
        cursor.execute(query, params)
        return cursor.fetchall() if fetch else None


def xlsx_rows(content):
    """Number of rows in the first sheet of an .xlsx file (header included)."""
    with zipfile.ZipFile(BytesIO(content)) as xlsx:
        sheet = xlsx.read("xl/worksheets/sheet1.xml").decode()
    return len(re.findall(r"<row ", sheet)), sheet


# Validation helpers (no database needed)
class HelpersTests(unittest.TestCase):

    def test_valid_report_passes(self):
        self.assertIsNone(labs.validate_report(dict(VALID_REPORT)))

    def test_report_rejects_empty_fields(self):
        self.assertEqual(labs.validate_report({**VALID_REPORT, "programa": ""}),
                         "Todos los campos son obligatorios.")

    def test_report_rejects_name_with_digits(self):
        self.assertIn("solo puede contener letras", labs.validate_report({**VALID_REPORT, "nombre_docente": "Ana 2"}))

    def test_report_requires_institutional_email(self):
        for email in ("ana@gmail.com", "ana@unicesmag.edu.co.evil.com", "a b@unicesmag.edu.co"):
            with self.subTest(email=email):
                self.assertIn("institucional", labs.validate_report({**VALID_REPORT, "correo_electronico": email}))

    def test_report_rejects_exit_before_entry(self):
        for salida in ("07:00", "06:59"):
            with self.subTest(salida=salida):
                self.assertIn("hora de salida", labs.validate_report({**VALID_REPORT, "hora_salida": salida}))

    def test_normalize_incidencia(self):
        for value in (None, "", "  ", "None", "none"):
            self.assertEqual(labs.normalize_incidencia(value), "")
        self.assertEqual(labs.normalize_incidencia("  Listo "), "Listo")

    def test_date_and_time_filters(self):
        self.assertEqual(labs.format_fecha(datetime(2026, 10, 27, 18, 10)), "27/10/2026 18:10")
        self.assertEqual(labs.format_hora(time(7, 0)), "07:00")
        self.assertEqual(labs.format_fecha(None), "")


# Routes against the test database
class AppTestCase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(os.path.dirname(__file__), "sql", "schema.sql"), encoding="utf-8") as schema:
            run_sql(schema.read())

    def setUp(self):
        run_sql("TRUNCATE control_laboratorios_sistemas, users RESTART IDENTITY")
        labs.app.config.update(TESTING=True, WTF_CSRF_ENABLED=False)
        labs.limiter.enabled = False
        self.client = labs.app.test_client()
        self.admin_id = self.create_user("admin", ADMIN_PASSWORD)

    def create_user(self, username, password):
        return run_sql("INSERT INTO users (username, hashed_password) VALUES (%s, %s) RETURNING id",
                       (username, generate_password_hash(password)), fetch=True)[0][0]

    def create_report(self, fecha="2026-10-15 08:00:00", **changes):
        data = {**VALID_REPORT, **changes}
        return run_sql(
            "INSERT INTO control_laboratorios_sistemas (fecha_registro, identificador_laboratorio, nombre_docente,"
            " correo_electronico, programa, hora_ingreso, hora_salida, observacion)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (fecha, *data.values()), fetch=True,
        )[0][0]

    def login(self, username="admin", password=ADMIN_PASSWORD):
        return self.client.post("/login", data={"username": username, "password": password})

    def report_count(self):
        return run_sql("SELECT count(*) FROM control_laboratorios_sistemas", fetch=True)[0][0]


class PublicFormTests(AppTestCase):

    def test_home_lists_the_laboratories(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        for lab in labs.LABORATORIOS:
            self.assertIn(lab, response.get_data(as_text=True))

    def test_valid_report_is_saved_in_local_time(self):
        response = self.client.post("/add_data", data=VALID_REPORT, follow_redirects=True)
        self.assertIn("Incidente enviado exitosamente.", response.get_data(as_text=True))
        fecha, nombre = run_sql("SELECT fecha_registro, nombre_docente FROM control_laboratorios_sistemas",
                                fetch=True)[0]
        self.assertEqual(nombre, "María Pérez")
        # Stored in Colombia's time, not the server's UTC
        now = datetime.now(labs.LOCAL_TZ).replace(tzinfo=None)
        self.assertLess(abs((now - fecha).total_seconds()), 60)

    def test_invalid_report_is_not_saved_and_keeps_the_form(self):
        response = self.client.post("/add_data", data={**VALID_REPORT, "correo_electronico": "maria@gmail.com"})
        body = response.get_data(as_text=True)
        self.assertIn("institucional", body)
        self.assertIn("El computador 5 no enciende", body)
        self.assertEqual(self.report_count(), 0)

    def test_unknown_laboratory_is_rejected(self):
        self.client.post("/add_data", data={**VALID_REPORT, "identificador_laboratorio": "Sala falsa"})
        self.assertEqual(self.report_count(), 0)

    def test_public_form_requires_csrf_token(self):
        labs.app.config["WTF_CSRF_ENABLED"] = True
        response = self.client.post("/add_data", data=VALID_REPORT)
        self.assertEqual(response.status_code, 400)
        self.assertIn("envíelo de nuevo", response.get_data(as_text=True))
        self.assertEqual(self.report_count(), 0)

    def test_public_form_is_rate_limited(self):
        labs.limiter.enabled = True
        labs.limiter.reset()
        statuses = [self.client.post("/add_data", data=VALID_REPORT).status_code for _ in range(11)]
        self.assertEqual(statuses[-1], 429)
        self.assertEqual(self.report_count(), 10)


class AuthTests(AppTestCase):

    def test_admin_pages_require_login(self):
        report_id = self.create_report()
        for method, path in (("get", "/admin"), ("get", f"/edit/{report_id}"), ("post", f"/update/{report_id}"),
                             ("post", f"/delete/{report_id}"), ("get", "/users"), ("get", "/register"),
                             ("get", f"/restore/{self.admin_id}"), ("post", f"/deleteuser/{self.admin_id}"),
                             ("get", "/reports"), ("get", "/generate"), ("get", "/download"), ("get", "/logout")):
            with self.subTest(path=path):
                response = getattr(self.client, method)(path)
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/login?expirada=1"))
        self.assertEqual(self.report_count(), 1)

    def test_login_with_wrong_password_fails(self):
        response = self.login(password="otra-clave")
        self.assertEqual(response.status_code, 200)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_login_and_logout(self):
        response = self.login()
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/admin"))
        self.assertEqual(self.client.get("/admin").status_code, 200)
        self.client.get("/logout")
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_login_is_rate_limited(self):
        labs.limiter.enabled = True
        labs.limiter.reset()
        statuses = [self.login(password="otra-clave").status_code for _ in range(11)]
        self.assertEqual(statuses[-1], 429)

    def test_session_ends_when_the_user_is_deleted(self):
        self.login()
        run_sql("DELETE FROM users WHERE id = %s", (self.admin_id,))
        self.assertEqual(self.client.get("/admin").status_code, 302)

    def test_session_expires_after_max_age(self):
        self.login()
        with self.client.session_transaction() as session:
            session["login_at"] -= labs.SESSION_MAX_AGE.total_seconds() + 1
        response = self.client.get("/admin")
        self.assertEqual(response.status_code, 302)
        self.assertIn("expirada=1", response.location)
        with self.client.session_transaction() as session:
            self.assertNotIn("user_id", session)

    def test_session_has_idle_timeout(self):
        self.assertEqual(labs.app.permanent_session_lifetime.total_seconds(), 30 * 60)


class ReportManagementTests(AppTestCase):

    def setUp(self):
        super().setUp()
        self.login()

    def update_form(self, **changes):
        return {**VALID_REPORT, "fecha_registro": "2026-10-15 08:00:00", "respuesta_incidencia": "", **changes}

    def test_dashboard_lists_reports(self):
        self.create_report(nombre_docente="Carlos Ruiz")
        self.assertIn("Carlos Ruiz", self.client.get("/admin").get_data(as_text=True))

    def test_edit_missing_report_redirects(self):
        response = self.client.get("/edit/9999", follow_redirects=True)
        self.assertIn("Registro no encontrado.", response.get_data(as_text=True))

    def test_update_emails_the_teacher_only_when_the_response_changes(self):
        report_id = self.create_report()
        with mock.patch.object(labs, "send_email_async") as send_email:
            self.client.post(f"/update/{report_id}", data=self.update_form(respuesta_incidencia="Se cambió la fuente"))
            self.client.post(f"/update/{report_id}", data=self.update_form(respuesta_incidencia="Se cambió la fuente"))
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[2], VALID_REPORT["correo_electronico"])
        self.assertIn("Se cambió la fuente", send_email.call_args.args[1])

    def test_update_without_response_does_not_email(self):
        report_id = self.create_report()
        with mock.patch.object(labs, "send_email_async") as send_email:
            self.client.post(f"/update/{report_id}", data=self.update_form(programa="Derecho"))
        send_email.assert_not_called()
        self.assertEqual(run_sql("SELECT programa FROM control_laboratorios_sistemas", fetch=True)[0][0], "Derecho")

    def test_update_validates_like_the_public_form(self):
        report_id = self.create_report()
        for changes in ({"fecha_registro": "ayer"}, {"hora_salida": "06:00"}, {"nombre_docente": ""}):
            with self.subTest(changes=changes):
                self.client.post(f"/update/{report_id}", data=self.update_form(**changes))
                row = run_sql("SELECT nombre_docente, hora_salida FROM control_laboratorios_sistemas", fetch=True)[0]
                self.assertEqual(row, ("María Pérez", time(9, 0)))

    def test_delete_report_only_by_post(self):
        report_id = self.create_report()
        self.assertEqual(self.client.get(f"/delete/{report_id}").status_code, 405)
        self.client.post(f"/delete/{report_id}")
        self.assertEqual(self.report_count(), 0)


class UserManagementTests(AppTestCase):

    def setUp(self):
        super().setUp()
        self.login()

    def register(self, username="soporte", password="clave-nueva-1", confirmation=None):
        return self.client.post("/register", data={
            "username": username, "password": password,
            "confirmation": password if confirmation is None else confirmation,
        })

    def test_register_rejects_bad_passwords_and_duplicates(self):
        for args in ({"password": "corta"}, {"confirmation": "distinta-123"}, {"username": "admin"}):
            with self.subTest(args=args):
                self.assertEqual(self.register(**args).status_code, 200)
        self.assertEqual(run_sql("SELECT count(*) FROM users", fetch=True)[0][0], 1)

    def test_register_keeps_the_current_admin_logged_in(self):
        self.register()
        self.assertEqual(run_sql("SELECT count(*) FROM users", fetch=True)[0][0], 2)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], self.admin_id)

    def test_users_page_never_shows_password_hashes(self):
        body = self.client.get("/users").get_data(as_text=True)
        self.assertIn("admin", body)
        self.assertNotIn("pbkdf2", body)
        self.assertNotIn("scrypt", body)

    def test_restore_password(self):
        user_id = self.create_user("soporte", "clave-vieja-1")
        self.client.post(f"/restore/{user_id}", data={"password": "clave-nueva-1", "confirmation": "clave-nueva-1"})
        self.client.get("/logout")
        self.assertEqual(self.login("soporte", "clave-nueva-1").status_code, 302)

    def test_cannot_delete_own_account(self):
        self.client.post(f"/deleteuser/{self.admin_id}")
        self.assertEqual(run_sql("SELECT count(*) FROM users", fetch=True)[0][0], 1)

    def test_delete_other_user(self):
        user_id = self.create_user("soporte", "clave-vieja-1")
        self.client.post(f"/deleteuser/{user_id}")
        self.assertEqual(run_sql("SELECT count(*) FROM users", fetch=True)[0][0], 1)


class ReportsTests(AppTestCase):

    def setUp(self):
        super().setUp()
        self.login()
        self.create_report(fecha="2026-09-10 08:00:00", nombre_docente="Septiembre")
        self.create_report(fecha="2026-10-10 08:00:00", nombre_docente="Octubre")

    def test_generate_filters_by_date_range(self):
        body = self.client.post("/generate", data={"start_date": "2026-10-01", "end_date": "2026-10-31"}) \
            .get_data(as_text=True)
        self.assertIn("Octubre", body)
        self.assertNotIn("Septiembre", body)

    def test_generate_requires_both_dates(self):
        body = self.client.post("/generate", data={"start_date": "2026-10-01"}).get_data(as_text=True)
        self.assertIn("Debe seleccionar una fecha de inicio y una fecha de fin.", body)

    def test_download_uses_the_last_date_range(self):
        self.client.post("/generate", data={"start_date": "2026-10-01", "end_date": "2026-10-31"})
        response = self.client.get("/download")
        self.assertEqual(response.mimetype, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        rows, _ = xlsx_rows(response.data)
        self.assertEqual(rows, 2)  # header + October

    def test_download_without_filter_exports_everything(self):
        # An earlier search must not stay applied after opening the unfiltered list
        self.client.post("/generate", data={"start_date": "2026-10-01", "end_date": "2026-10-31"})
        self.client.get("/generate")
        rows, _ = xlsx_rows(self.client.get("/download").data)
        self.assertEqual(rows, 3)

    def test_download_never_writes_formulas(self):
        self.create_report(observacion="=HYPERLINK(\"http://evil\",\"x\")")
        _, sheet = xlsx_rows(self.client.get("/download").data)
        self.assertNotIn("<f>", sheet)


if __name__ == "__main__":
    unittest.main()
