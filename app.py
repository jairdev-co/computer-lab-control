# Import libraries
import atexit
import os
import re
import smtplib
import sys
import threading
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
from email.mime.text import MIMEText
from io import BytesIO
from zoneinfo import ZoneInfo
import pandas as pd
import psycopg
import psycopg.rows
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, flash, send_file, session
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_wtf.csrf import CSRFProtect, CSRFError
from psycopg_pool import ConnectionPool
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash, generate_password_hash
from helpers import login_required

# Load environment variables
load_dotenv()

# Flush logs immediately on Render
sys.stdout.reconfigure(line_buffering=True)

# Create the Flask app
app = Flask(__name__)

# Trust Render's proxy so remote_addr is the real client IP
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1)

# Store records in Colombia's local time (servers run in UTC)
LOCAL_TZ = ZoneInfo("America/Bogota")

# Laboratories available in the public form
LABORATORIOS = [
    "Laboratorio de Competencias Digitales 1",
    "Laboratorio de Competencias Digitales 3",
    "Laboratorio de Redes",
]

# Reports per page in the admin panel
PER_PAGE = 10

# Report fields, their labels in messages and the column limits in sql/schema.sql
REPORT_FIELDS = {
    'identificador_laboratorio': ("Laboratorio", 100),
    'nombre_docente': ("Nombre de docente", 150),
    'correo_electronico': ("Correo institucional", 150),
    'programa': ("Programa", 150),
    'hora_ingreso': ("Hora de ingreso", None),
    'hora_salida': ("Hora de salida", None),
    'observacion': ("Observaciones", None),
}
USERNAME_MAX_LENGTH = 100
NAME_RE = re.compile(r"[A-Za-zÁÉÍÓÚáéíóúÜüÑñ ]+")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@unicesmag\.edu\.co")

# Session and security settings
if not os.environ.get("SECRET_KEY"):
    print("Warning: SECRET_KEY is not set; sessions will end every time the app restarts.")
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY") or os.urandom(24).hex()
# Idle timeout: the session cookie is renewed on every request, so it expires after 30 min without activity
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(minutes=30)
# Absolute timeout: log in again after 8 hours even if the session stays active
SESSION_MAX_AGE = timedelta(hours=8)
app.config["WTF_CSRF_TIME_LIMIT"] = None
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = bool(os.environ.get("RENDER"))

# CSRF protection for all POST requests
csrf = CSRFProtect(app)

# Rate limiting per IP (in-memory: gunicorn runs a single worker)
limiter = Limiter(get_remote_address, app=app, storage_uri="memory://")

# Database connection pool
_db_conninfo = psycopg.conninfo.make_conninfo(
    host=os.environ.get("DB_HOST"),
    port=os.environ.get("DB_PORT"),
    dbname=os.environ.get("DB_NAME"),
    user=os.environ.get("DB_USER"),
    password=os.environ.get("DB_PASSWORD"),
)
# prepare_threshold=None: Supabase's transaction pooler doesn't support prepared statements
db_pool = ConnectionPool(
    conninfo=_db_conninfo, min_size=1, max_size=5, open=True,
    kwargs={"prepare_threshold": None},
    check=ConnectionPool.check_connection,
)
atexit.register(db_pool.close)


# Yield a cursor from a pooled connection; commit on success, roll back on error
@contextmanager
def get_db_cursor(row_factory=None):
    with db_pool.connection() as conn:
        cursor = conn.cursor(row_factory=row_factory) if row_factory else conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            cursor.close()


# Send an email in a background thread via Gmail SMTP
def send_email_async(subject, body, to_email):
    def send():
        try:
            email_user = os.environ.get("EMAIL_USER")
            # App Passwords are shown as "xxxx xxxx xxxx xxxx"; Gmail expects them without spaces
            email_pass = (os.environ.get("EMAIL_PASS") or "").replace(" ", "")
            if not email_user or not email_pass:
                print("Email credentials not configured.")
                return
            msg = MIMEText(body, "plain", "utf-8")
            msg["From"] = email_user
            msg["To"] = to_email
            msg["Subject"] = subject
            # The context manager closes the connection even when sending fails
            with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
                server.starttls()
                server.login(email_user, email_pass)
                server.sendmail(email_user, to_email, msg.as_string())
            print(f"Email sent successfully to {to_email}")
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as e:
            # Gmail usually drops the connection after rejecting credentials
            print(f"Error: Gmail authentication failed ({e}). Check EMAIL_USER and EMAIL_PASS (App Password).")
        except smtplib.SMTPException as e:
            print(f"SMTP error sending email: {e}")
        except Exception as e:
            print(f"Unexpected error sending email: {e}")

    thread = threading.Thread(target=send, daemon=False)
    thread.start()


# Read the given form fields without surrounding spaces
def read_form(fields):
    return {field: (request.form.get(field) or '').strip() for field in fields}


# Parse an HH:MM or HH:MM:SS time; None when it is not valid
def parse_time(value):
    try:
        parsed = time.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is None else None


# Validate report fields; return an error message or None
def validate_report(form_data):
    if not all(value and str(value).strip() for value in form_data.values()):
        return "Todos los campos son obligatorios."
    for field, (label, max_length) in REPORT_FIELDS.items():
        if max_length and len(form_data[field]) > max_length:
            return f"El campo «{label}» no puede superar {max_length} caracteres."
    if not NAME_RE.fullmatch(form_data['nombre_docente']):
        return "El nombre del docente solo puede contener letras y espacios."
    if not EMAIL_RE.fullmatch(form_data['correo_electronico']):
        return "Correo electrónico no válido. Debe ser un correo institucional."
    hora_ingreso = parse_time(form_data['hora_ingreso'])
    hora_salida = parse_time(form_data['hora_salida'])
    if hora_ingreso is None or hora_salida is None:
        return "Hora no válida. Use el formato HH:MM."
    if hora_ingreso >= hora_salida:
        return "La hora de salida no debe ser anterior o igual a la hora de ingreso."
    return None


# Date range kept in the session by /generate: (first day, day after the last) or None
def session_date_range():
    try:
        start = date.fromisoformat(session['start_date'])
        end = date.fromisoformat(session['end_date'])
    except (KeyError, TypeError, ValueError):
        return None
    return start, end + timedelta(days=1)


# Reports in chronological order, optionally limited to a date range
def fetch_reports(columns, date_range=None, row_factory=None):
    query = f"SELECT {columns} FROM control_laboratorios_sistemas"
    params = ()
    if date_range:
        query += " WHERE fecha_registro >= %s AND fecha_registro < %s"
        params = date_range
    with get_db_cursor(row_factory=row_factory) as cursor:
        cursor.execute(query + " ORDER BY fecha_registro, id", params)
        return cursor.fetchall()


# Treat None, empty and 'none' as the same value
def normalize_incidencia(value):
    if value is None:
        return ''
    value = str(value).strip()
    return '' if value.lower() == 'none' else value


# Log out sessions older than SESSION_MAX_AGE or whose user no longer exists
@app.before_request
def check_session():
    if request.endpoint == 'static' or session.get("user_id") is None:
        return
    login_at = session.get("login_at", 0)
    if datetime.now().timestamp() - login_at > SESSION_MAX_AGE.total_seconds():
        session.clear()
        if request.endpoint not in ('index', 'add_data', 'login_admin'):
            return redirect(url_for('login_admin', expirada=1))
        return
    with get_db_cursor() as cursor:
        cursor.execute("SELECT 1 FROM users WHERE id = %s", (session["user_id"],))
        if cursor.fetchone() is None:
            session.clear()


# Global template variables
@app.context_processor
def inject_laboratorios():
    return {"laboratorios": LABORATORIOS, "current_year": datetime.now(LOCAL_TZ).year}


# Date filter: 27/10/2026 18:10
@app.template_filter("fecha")
def format_fecha(value):
    return value.strftime("%d/%m/%Y %H:%M") if hasattr(value, "strftime") else (value or "")


# Time filter: 07:00
@app.template_filter("hora")
def format_hora(value):
    return value.strftime("%H:%M") if hasattr(value, "strftime") else (value or "")


# Rate limit exceeded
@app.errorhandler(429)
def ratelimit_handler(e):
    if request.endpoint == 'login_admin':
        return render_template("login.html", message=4), 429
    flash("Demasiados envíos seguidos. Espere un minuto e inténtelo de nuevo.", category='warning')
    return render_template('index.html', form_data=request.form), 429


# Friendly error pages
@app.errorhandler(404)
def not_found_handler(e):
    return render_template("error.html", message="La página que busca no existe."), 404


@app.errorhandler(500)
def server_error_handler(e):
    return render_template(
        "error.html", message="Ocurrió un error inesperado. Por favor, inténtelo de nuevo en unos minutos."
    ), 500


# Expired or missing CSRF token
@app.errorhandler(CSRFError)
def csrf_error_handler(e):
    if request.endpoint == 'add_data':
        flash("El formulario estuvo abierto mucho tiempo. Por seguridad, envíelo de nuevo.", category='warning')
        return render_template('index.html', form_data=request.form), 400
    if session.get("user_id") is None:
        return redirect(url_for('login_admin', expirada=1))
    flash("La página estuvo abierta mucho tiempo. Por seguridad, inténtelo de nuevo.", category='warning')
    return redirect(url_for('admin_data'))


# Home page (public form)
@app.route("/")
def index():
    return render_template("index.html")


# Save a new report from the public form
@app.route("/add_data", methods=['POST'])
@limiter.limit("10 per minute")
def add_data():
    form_data = read_form(REPORT_FIELDS)
    try:
        fecha_registro = datetime.now(LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')
        error = validate_report(form_data)
        if not error and form_data['identificador_laboratorio'] not in LABORATORIOS:
            error = "Seleccione un laboratorio válido."
        if error:
            flash(error, category='warning')
            return render_template('index.html', form_data=form_data)

        with get_db_cursor() as cursor:
            query = """
            INSERT INTO control_laboratorios_sistemas
                (fecha_registro, identificador_laboratorio, nombre_docente,
                 correo_electronico, programa, hora_ingreso, hora_salida, observacion)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """
            cursor.execute(query, (fecha_registro, *form_data.values()))

        flash("Incidente enviado exitosamente.", category='success')
        return redirect(url_for('index'))
    except Exception as e:
        print(f"Error saving incident report: {e}")
        flash("Ocurrió un error al guardar el incidente. Por favor, inténtelo de nuevo.", category='danger')
        return render_template('index.html', form_data=form_data)


# Admin login
@app.route("/login", methods=["GET", "POST"])
@limiter.limit("10 per minute", methods=["POST"])
def login_admin():
    # An admin who is already logged in goes straight to the panel
    if request.method == "GET" and session.get("user_id"):
        return redirect(url_for('admin_data'))
    session.clear()
    if request.method == "POST":
        input_username = (request.form.get("username") or "").strip()
        input_password = request.form.get("password")
        if not input_username:
            return render_template("login.html", message=1)
        elif not input_password:
            return render_template("login.html", message=2)

        with get_db_cursor(row_factory=psycopg.rows.dict_row) as cursor:
            cursor.execute('SELECT * FROM users WHERE username = %s', (input_username,))
            user = cursor.fetchone()

        if user is None or not check_password_hash(user["hashed_password"], input_password):
            return render_template("login.html", message=3)

        session["user_id"] = user["id"]
        session["login_at"] = datetime.now().timestamp()
        session.permanent = True
        return redirect("/admin")
    return render_template("login.html", message=5 if request.args.get("expirada") else None)


# Admin dashboard: records from newest to oldest, PER_PAGE at a time
@app.route("/admin")
@login_required
def admin_data():
    page = request.args.get('page', 1, type=int)
    with get_db_cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute('SELECT count(*) AS total FROM control_laboratorios_sistemas')
        total = cursor.fetchone()['total']
        total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
        page = min(max(page, 1), total_pages)
        cursor.execute(
            'SELECT * FROM control_laboratorios_sistemas ORDER BY fecha_registro DESC, id DESC'
            ' LIMIT %s OFFSET %s',
            (PER_PAGE, (page - 1) * PER_PAGE)
        )
        data = cursor.fetchall()
    return render_template("/admin.html", data=data, page=page, total_pages=total_pages)


# Edit form for a record
@app.route("/edit/<int:id>")
@login_required
def get_data(id):
    with get_db_cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute('SELECT * FROM control_laboratorios_sistemas WHERE id = %s', (id,))
        data = cursor.fetchone()
    if not data:
        flash("Registro no encontrado.", category='warning')
        return redirect(url_for('admin_data'))
    return render_template("edit.html", record=data, page=request.args.get('page', 1, type=int))


# Update a record and email the teacher when the response changes
@app.route("/update/<int:id>", methods=['POST'])
@login_required
def update_data(id):
    page = request.form.get('page', 1, type=int)
    form_data = read_form(['fecha_registro', *REPORT_FIELDS])
    respuesta_incidencia_db = (request.form.get('respuesta_incidencia') or '').strip() or None

    error = validate_report(form_data)
    if not error:
        try:
            fecha_registro = datetime.fromisoformat(form_data['fecha_registro'])
            if fecha_registro.tzinfo is not None:
                raise ValueError
        except ValueError:
            error = "Fecha de registro no válida. Use el formato AAAA-MM-DD HH:MM:SS."
    if error:
        flash(error, category='warning')
        return redirect(url_for('get_data', id=id, page=page))

    try:
        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT respuesta_incidencia, identificador_laboratorio"
                " FROM control_laboratorios_sistemas WHERE id = %s", (id,)
            )
            resultado = cursor.fetchone()
            if resultado is None:
                flash("Registro no encontrado.", category='warning')
                return redirect(url_for('admin_data'))
            respuesta_incidencia_anterior, laboratorio_anterior = resultado
            # Same rule as the public form, but old records may keep a laboratory no longer listed
            if form_data['identificador_laboratorio'] not in (*LABORATORIOS, laboratorio_anterior):
                flash("Seleccione un laboratorio válido.", category='warning')
                return redirect(url_for('get_data', id=id, page=page))

            query = """
                UPDATE control_laboratorios_sistemas
                SET fecha_registro = %s,
                    identificador_laboratorio = %s,
                    nombre_docente = %s,
                    correo_electronico = %s,
                    programa = %s,
                    hora_ingreso = %s,
                    hora_salida = %s,
                    observacion = %s,
                    respuesta_incidencia = %s
                WHERE id = %s
            """
            cursor.execute(query, (*form_data.values(), respuesta_incidencia_db, id))
    except Exception as e:
        print(f"Error updating record {id}: {e}")
        flash("Ocurrió un error al guardar los cambios. Por favor, inténtelo de nuevo.", category='danger')
        return redirect(url_for('get_data', id=id, page=page))

    correo_electronico = form_data['correo_electronico']

    # Email only when there is a new or changed response
    if respuesta_incidencia_db is not None and normalize_incidencia(respuesta_incidencia_anterior) != normalize_incidencia(respuesta_incidencia_db):
        subject = "Actualización: Respuesta a su reporte de incidencia - Laboratorios de Sistemas"
        body = f"""Paz y Bien,

Le informamos que se ha registrado una respuesta al incidente reportado en el Sistema de Control de Laboratorios:

----------------------------------------------------------------------
RESPUESTA:
{respuesta_incidencia_db}
----------------------------------------------------------------------

Si tiene alguna duda o requiere asistencia adicional, por favor responda a este mensaje.


Cordialmente,
Brayan Jair Enriquez
Infraestructura TI"""
        send_email_async(subject, body, correo_electronico)
        flash("Respuesta de incidencia enviada exitosamente.", category='success')
    else:
        flash('Datos actualizados exitosamente.', category='success')

    # Back to the page the record was edited from
    return redirect(url_for('admin_data', page=page))


# Delete a record
@app.route("/delete/<int:id>", methods=['POST'])
@login_required
def delete(id):
    with get_db_cursor() as cursor:
        cursor.execute('DELETE FROM control_laboratorios_sistemas WHERE id = %s', (id,))
    flash("Datos eliminados correctamente.", category='success')
    # Back to the same page
    return redirect(url_for('admin_data', page=request.form.get('page', 1, type=int)))


# Register a new admin user
@app.route("/register", methods=["GET", "POST"])
@login_required
def register():
    if request.method == "POST":
        input_username = (request.form.get("username") or "").strip()
        input_password = request.form.get("password")
        input_confirmation = request.form.get("confirmation")
        if not input_username:
            return render_template("register.html", message=1)
        elif len(input_username) > USERNAME_MAX_LENGTH:
            return render_template("register.html", message=7)
        elif not input_password:
            return render_template("register.html", message=2)
        elif not input_confirmation:
            return render_template("register.html", message=4)
        elif input_password != input_confirmation:
            return render_template("register.html", message=3)
        elif len(input_password) < 8:
            return render_template("register.html", message=6)

        try:
            with get_db_cursor() as cursor:
                cursor.execute(
                    'INSERT INTO users (username, hashed_password) VALUES (%s, %s)',
                    (input_username, generate_password_hash(input_password))
                )
        except psycopg.errors.UniqueViolation:
            return render_template("register.html", message=5)

        flash(f"Usuario {input_username} registrado correctamente.", category='success')
        return redirect("/admin")
    return render_template("register.html")


# List users (without password hashes)
@app.route("/users")
@login_required
def users():
    with get_db_cursor(row_factory=psycopg.rows.dict_row) as cursor:
        cursor.execute('SELECT id, username FROM users ORDER BY id')
        data = cursor.fetchall()
    return render_template("/users.html", data=data)


# Reset a user's password
@app.route("/restore/<int:id>", methods=['GET', 'POST'])
@login_required
def restore(id):
    with get_db_cursor() as cursor:
        cursor.execute("SELECT username FROM users WHERE id = %s", (id,))
        user = cursor.fetchone()
    if user is None:
        flash("Usuario no encontrado.", category='warning')
        return redirect("/users")
    username = user[0]

    if request.method == 'POST':
        new_password = request.form.get('password')
        confirmation = request.form.get('confirmation')

        if not new_password:
            return render_template("restore.html", message=2, id=id, username=username)
        elif not confirmation:
            return render_template("restore.html", message=4, id=id, username=username)
        elif new_password != confirmation:
            return render_template("restore.html", message=3, id=id, username=username)
        elif len(new_password) < 8:
            return render_template("restore.html", message=6, id=id, username=username)

        with get_db_cursor() as cursor:
            cursor.execute("UPDATE users SET hashed_password = %s WHERE id = %s",
                           (generate_password_hash(new_password), id))

        flash("Datos actualizados exitosamente.", category='success')
        return redirect("/users")

    return render_template("restore.html", message=0, id=id, username=username)


# Delete a user (never your own account)
@app.route("/deleteuser/<int:id>", methods=['POST'])
@login_required
def deleteuser(id):
    if id == session.get("user_id"):
        flash("No puede eliminar su propio usuario.", category='warning')
        return redirect("/users")
    with get_db_cursor() as cursor:
        cursor.execute('DELETE FROM users WHERE id = %s', (id,))
    flash("Usuario eliminado correctamente.", category='success')
    return redirect("/users")


# Reports page
@app.route("/reports", methods=['GET'])
@login_required
def reports():
    return render_template("/reports.html")


# Records filtered by date range (GET shows all)
@app.route("/generate", methods=['GET', 'POST'])
@login_required
def generate():
    session.pop('start_date', None)
    session.pop('end_date', None)
    if request.method == 'POST':
        start_date = request.form.get('start_date')
        end_date = request.form.get('end_date')
        if not start_date or not end_date:
            flash("Debe seleccionar una fecha de inicio y una fecha de fin.", category='warning')
            return redirect(url_for('reports'))
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError:
            flash("Fechas no válidas. Use el formato AAAA-MM-DD.", category='warning')
            return redirect(url_for('reports'))
        if start > end:
            flash("La fecha de inicio no puede ser posterior a la fecha de fin.", category='warning')
            return redirect(url_for('reports'))
        # Download reuses this range
        session['start_date'] = start.isoformat()
        session['end_date'] = end.isoformat()

    date_range = session_date_range()
    data = fetch_reports("*", date_range, row_factory=psycopg.rows.dict_row)
    return render_template("/generate.html", data=data,
                           start=date_range and date_range[0],
                           end=date_range and date_range[1] - timedelta(days=1))


# Download the report as an Excel file
@app.route("/download")
@login_required
def download():
    data = fetch_reports(
        "fecha_registro, identificador_laboratorio, nombre_docente, correo_electronico,"
        " programa, hora_ingreso, hora_salida, observacion, respuesta_incidencia",
        session_date_range()
    )

    columns = [
        'Fecha', 'Laboratorio', 'Nombres',
        'Correo electrónico', 'Programa',
        'Hora de ingreso', 'Hora de salida',
        'Observaciones', 'Respuesta'
    ]

    df = pd.DataFrame(data, columns=columns)
    df['Fecha'] = pd.to_datetime(df['Fecha'], errors='coerce').dt.strftime('%Y-%m-%d')
    df['Hora de ingreso'] = df['Hora de ingreso'].map(format_hora)
    df['Hora de salida'] = df['Hora de salida'].map(format_hora)
    df['Respuesta'] = df['Respuesta'].fillna('')

    output = BytesIO()
    # strings_to_formulas=False: user text is never evaluated as an Excel formula
    with pd.ExcelWriter(output, engine='xlsxwriter',
                        engine_kwargs={'options': {'strings_to_formulas': False}}) as writer:
        df.to_excel(writer, sheet_name='Reporte', index=False)
        workbook = writer.book
        worksheet = writer.sheets['Reporte']
        header_format = workbook.add_format({
            'bold': True,
            'font_size': 12,
            'bg_color': '#4F81BD',
            'font_color': 'white',
            'align': 'center',
            'valign': 'vcenter'
        })
        # Long texts wrap instead of making the column endless
        wrap_format = workbook.add_format({'text_wrap': True, 'valign': 'top'})
        for col_num, value in enumerate(df.columns):
            worksheet.write(0, col_num, value, header_format)
        for idx, col in enumerate(df.columns):
            width = max([len(col), *df[col].astype(str).map(len)]) + 2
            worksheet.set_column(idx, idx, min(width, 60), wrap_format if width > 60 else None)
    output.seek(0)
    timestamp = datetime.now(LOCAL_TZ).strftime('%Y%m%d_%H%M%S')
    filename = f'reporte_control_laboratorios_sistemas_{timestamp}.xlsx'
    return send_file(
        output,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name=filename
    )


# Log out
@app.route("/logout")
@login_required
def logout():
    session.clear()
    return redirect("/")


# Run the development server
if __name__ == "__main__":
    app.run(debug=False)
