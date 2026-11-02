-- Run once on an empty database: psql -d database_name -f sql/schema.sql

-- Laboratory reports
CREATE TABLE IF NOT EXISTS control_laboratorios_sistemas (
    id                        SERIAL PRIMARY KEY,
    fecha_registro            TIMESTAMP    NOT NULL,
    identificador_laboratorio VARCHAR(100) NOT NULL,
    nombre_docente            VARCHAR(150) NOT NULL,
    correo_electronico        VARCHAR(150) NOT NULL,
    programa                  VARCHAR(150) NOT NULL,
    hora_ingreso              TIME         NOT NULL,
    hora_salida               TIME         NOT NULL,
    observacion               TEXT         NOT NULL,
    respuesta_incidencia      TEXT
);

-- Admin users
CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    username        VARCHAR(100) NOT NULL UNIQUE,
    hashed_password TEXT         NOT NULL
);
