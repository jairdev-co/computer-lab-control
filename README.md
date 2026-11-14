# Control de Laboratorios de Sistemas

Aplicación web para reportar y dar seguimiento a las incidencias de las salas de cómputo de la Universidad CESMAG. Los docentes reportan incidencias desde un formulario público, sin cuenta, y el personal de Infraestructura TI las gestiona desde un panel protegido: responde, edita o elimina reportes, avisa por correo al docente y exporta los registros a Excel.

## Stack

| Capa          | Tecnología                                            |
| ------------- | ----------------------------------------------------- |
| Backend       | Python 3.12, Flask 3.1, Gunicorn                      |
| Base de datos | PostgreSQL con `psycopg` 3 y `psycopg-pool` (sin ORM) |
| Seguridad     | Flask-WTF (CSRF), Flask-Limiter, Werkzeug             |
| Reportes      | pandas, XlsxWriter                                    |
| Correo        | Gmail SMTP                                            |
| UI            | Jinja2, Bootstrap 4 (Bootswatch Materia), JavaScript  |
| Despliegue    | Render                                                |

## Características

- **Formulario público**: el docente elige el laboratorio y registra su nombre, correo institucional (`@unicesmag.edu.co`), programa, horas de ingreso y salida y la observación. Todos los campos se validan.
- **Panel de administración**: lista los reportes del más reciente al más antiguo, de 10 en 10 con paginación, y permite editarlos, responderlos o eliminarlos.
- **Aviso por correo**: cuando se guarda una respuesta nueva, el docente la recibe por correo.
- **Administradores**: crear cuentas, restablecer contraseñas y eliminarlas. Nadie puede eliminar su propia cuenta.
- **Reportes en Excel**: vista previa de todas las incidencias o de un rango de fechas y descarga en `.xlsx`.
- **Seguridad**: protección CSRF, límite de 10 peticiones por minuto por IP en el login y el formulario, contraseñas con hash, consultas parametrizadas y sesiones que se cierran tras 30 minutos de inactividad o 8 horas desde el inicio de sesión.

## Estructura

```
computer-lab-control/
├── app.py              # Configuración y rutas
├── helpers.py          # Decorador login_required
├── test_app.py         # Pruebas automáticas
├── sql/
│   └── schema.sql      # Tablas de la base de datos
├── templates/          # Vistas Jinja2
├── static/             # CSS, JavaScript e imágenes
├── requirements.txt
├── runtime.txt         # Versión de Python
└── Procfile            # Comando de arranque
```

## Requisitos

- Python 3.12
- PostgreSQL (local, Render, Supabase, etc.)
- Una cuenta de Gmail con verificación en dos pasos y una [contraseña de aplicación](https://myaccount.google.com/apppasswords)

## Instalación

```bash
git clone https://github.com/jairdev-co/computer-lab-control.git
cd computer-lab-control
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Configuración

Crea un archivo `.env` en la raíz del proyecto:

```env
SECRET_KEY=una_clave_larga_y_aleatoria
DB_HOST=localhost
DB_PORT=5432
DB_NAME=nombre_de_la_base
DB_USER=usuario
DB_PASSWORD=contraseña
EMAIL_USER=cuenta@gmail.com
EMAIL_PASS=contraseña_de_aplicacion
```

| Variable      | Descripción                                                          |
| ------------- | -------------------------------------------------------------------- |
| `SECRET_KEY`  | Firma las sesiones                                                   |
| `DB_HOST`     | Servidor de PostgreSQL                                               |
| `DB_PORT`     | Puerto de PostgreSQL                                                 |
| `DB_NAME`     | Nombre de la base de datos                                           |
| `DB_USER`     | Usuario de la base de datos                                          |
| `DB_PASSWORD` | Contraseña de la base de datos                                       |
| `EMAIL_USER`  | Cuenta de Gmail que envía los avisos                                 |
| `EMAIL_PASS`  | Contraseña de aplicación de Gmail. Si falta, no se envían correos    |

Genera la `SECRET_KEY` con:

```bash
python -c "import secrets; print(secrets.token_hex(32))"
```

### Base de datos

Crea las tablas (también se puede volver a ejecutar sobre una base existente para añadir el índice por fecha):

```bash
psql -h <host> -p <puerto> -U <usuario> -d <base> -f sql/schema.sql
```

### Primer administrador

Las cuentas nuevas se crean desde el panel, así que la primera se inserta directamente en la base. Genera el hash de la contraseña:

```bash
python -c "from werkzeug.security import generate_password_hash; print(generate_password_hash('tu_contraseña'))"
```

E insértalo:

```sql
INSERT INTO users (username, hashed_password) VALUES ('admin', '<hash generado>');
```

### Laboratorios

La lista de laboratorios del formulario está en la constante `LABORATORIOS` de `app.py`.

## Uso

```bash
python app.py
```

Formulario en <http://localhost:5000> y panel en <http://localhost:5000/login>.

En producción:

```bash
gunicorn app:app
```

### Pruebas

Las pruebas usan una base de PostgreSQL vacía cuyo nombre termina en `_test` (se vacía en cada prueba, por eso nunca se apuntan a la base real ni leen el `.env`). Con Docker:

```bash
docker run -d --rm --name labs_test_db -e POSTGRES_PASSWORD=test -e POSTGRES_DB=labs_test -p 127.0.0.1:55432:5432 postgres:16-alpine
python -m unittest -v
docker stop labs_test_db
```

Para usar otra base, define `TEST_DB_HOST`, `TEST_DB_PORT`, `TEST_DB_NAME`, `TEST_DB_USER` y `TEST_DB_PASSWORD`. Los correos nunca se envían durante las pruebas.

### Despliegue en Render

1. Crea un **Web Service** en [Render](https://dashboard.render.com) y conecta el repositorio.
2. Configura el servicio:

   | Opción        | Valor                                           |
   | ------------- | ----------------------------------------------- |
   | Runtime       | Python                                          |
   | Build Command | `pip install -r requirements.txt`               |
   | Start Command | `gunicorn app:app`                              |
   | Variables     | Las del `.env` y `PYTHON_VERSION=3.12.7`        |

3. Crea las tablas y el primer administrador en la base de producción.
4. Pulsa **Create Web Service**. Cada `git push` a `main` vuelve a desplegar.

## Rutas

| Método    | Ruta               | Descripción                                  | Auth |
| --------- | ------------------ | -------------------------------------------- | ---- |
| GET       | `/`                | Formulario público                           | No   |
| POST      | `/add_data`        | Guarda una incidencia                        | No   |
| GET, POST | `/login`           | Inicio de sesión                             | No   |
| GET       | `/admin`           | Listado de reportes                          | Sí   |
| GET       | `/edit/<id>`       | Editar o responder un reporte                | Sí   |
| POST      | `/update/<id>`     | Guarda los cambios y avisa por correo        | Sí   |
| POST      | `/delete/<id>`     | Elimina un reporte                           | Sí   |
| GET       | `/users`           | Listado de administradores                   | Sí   |
| GET, POST | `/register`        | Crea un administrador                        | Sí   |
| GET, POST | `/restore/<id>`    | Restablece una contraseña                    | Sí   |
| POST      | `/deleteuser/<id>` | Elimina un administrador                     | Sí   |
| GET       | `/reports`         | Selección del rango de fechas                | Sí   |
| GET, POST | `/generate`        | Vista previa de los reportes                 | Sí   |
| GET       | `/download`        | Descarga el Excel                            | Sí   |
| GET       | `/logout`          | Cierra la sesión                             | Sí   |

## Autor

Brayan Jair Enriquez
