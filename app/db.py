"""Base de datos de SIRA (SQLite en la demo; el mismo esquema sirve en PostgreSQL).

Separación clave:
- `calificacion` y `alumno` solo conocen el seudónimo del alumno.
- `identidad` guarda nombre y matrícula para que el tutor reconozca a sus tutorados.
  Ninguna consulta que alimenta a Gemma lee esta tabla.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager

from .config import DB_PATH, reticula

ESQUEMA = """
CREATE TABLE IF NOT EXISTS tutor (
    usuario TEXT PRIMARY KEY,
    nombre  TEXT NOT NULL,
    ultima_sincronizacion TEXT
);
CREATE TABLE IF NOT EXISTS materia (
    clave TEXT PRIMARY KEY,
    nombre TEXT NOT NULL,
    semestre INTEGER NOT NULL,
    creditos INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS seriacion (
    materia TEXT NOT NULL REFERENCES materia(clave),
    prerrequisito TEXT NOT NULL REFERENCES materia(clave),
    PRIMARY KEY (materia, prerrequisito)
);
CREATE TABLE IF NOT EXISTS alumno (
    seudonimo TEXT PRIMARY KEY,
    tutor TEXT NOT NULL REFERENCES tutor(usuario),
    programa TEXT,
    semestre INTEGER
);
CREATE TABLE IF NOT EXISTS identidad (
    seudonimo TEXT PRIMARY KEY REFERENCES alumno(seudonimo),
    nombre TEXT NOT NULL,
    matricula TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS calificacion (
    seudonimo TEXT NOT NULL REFERENCES alumno(seudonimo),
    clave TEXT NOT NULL REFERENCES materia(clave),
    periodo TEXT NOT NULL,
    oportunidad INTEGER NOT NULL,
    tipo TEXT NOT NULL,
    calificacion REAL,
    estatus TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_calif_alumno ON calificacion(seudonimo);
CREATE TABLE IF NOT EXISTS seguimiento (
    seudonimo TEXT PRIMARY KEY REFERENCES alumno(seudonimo),
    contactado INTEGER NOT NULL DEFAULT 0,
    nota TEXT NOT NULL DEFAULT '',
    actualizado TEXT
);
CREATE TABLE IF NOT EXISTS sugerencia (
    seudonimo TEXT PRIMARY KEY,
    texto TEXT NOT NULL,
    origen TEXT NOT NULL,
    creada TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS propuesta_semestral (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creada TEXT NOT NULL,
    origen TEXT NOT NULL,
    modelo TEXT,
    contenido TEXT NOT NULL,
    validacion TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS propuesta (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    creada TEXT NOT NULL,
    origen TEXT NOT NULL,
    modelo TEXT,
    contenido TEXT NOT NULL,
    validacion TEXT NOT NULL
);
"""


def conectar() -> sqlite3.Connection:
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    return con


@contextmanager
def conexion():
    con = conectar()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def inicializar(tutores: list[dict]) -> None:
    """Crea el esquema y carga el catálogo de la retícula (plan de estudios público)."""
    with conexion() as con:
        con.executescript(ESQUEMA)
        for m in reticula()["materias"]:
            con.execute(
                "INSERT OR IGNORE INTO materia(clave, nombre, semestre, creditos) VALUES (?,?,?,?)",
                (m["clave"], m["nombre"], m["semestre"], m["creditos"]),
            )
            for p in m["prerrequisitos"]:
                con.execute("INSERT OR IGNORE INTO seriacion(materia, prerrequisito) VALUES (?,?)", (m["clave"], p))
        for t in tutores:
            con.execute("INSERT OR IGNORE INTO tutor(usuario, nombre) VALUES (?,?)", (t["usuario"], t["nombre"]))


def reiniciar() -> None:
    """Borra lo sincronizado (útil para repetir la demo desde cero)."""
    with conexion() as con:
        for tabla in ("sugerencia", "seguimiento", "calificacion", "identidad", "alumno", "propuesta",
                      "propuesta_semestral"):
            con.execute(f"DELETE FROM {tabla}")
        con.execute("UPDATE tutor SET ultima_sincronizacion = NULL")
