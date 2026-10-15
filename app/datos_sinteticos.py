"""Genera el contenido de la réplica del portal docente (kárdex sintéticos).

Todo lo que produce este módulo es ficticio: nombres, matrículas y calificaciones.
SIRA no lee este archivo directamente; lo obtiene raspando la réplica del portal,
igual que haría con el portal real.

Uso:  python -m app.datos_sinteticos
"""
from __future__ import annotations

import json
import random

from .config import MOCK_PATH, reglas, reticula

PERIODOS_CERRADOS = ["2023-1", "2023-2", "2024-1", "2024-2", "2025-1", "2025-2", "2026-1"]

NOMBRES = [
    "Ana Sofía", "Luis Ángel", "María José", "Jorge", "Fernanda", "Diego", "Valeria", "Kevin",
    "Daniela", "José Manuel", "Andrea", "Ángel", "Karla", "Emilio", "Ximena", "Rodrigo", "Itzel",
    "Brandon", "Paola", "Ricardo", "Montserrat", "Saúl", "Alondra", "Eduardo", "Abril", "Gael",
    "Renata", "Iván", "Citlali", "Óscar", "Mariana", "Héctor", "Lizbeth", "Axel", "Dulce",
]
APELLIDOS = [
    "Pech", "Canul", "Chi", "Uc", "May", "Ek", "Dzib", "Cauich", "Hernández", "López", "Martínez",
    "Gómez", "Pérez", "Sánchez", "Ramírez", "Cruz", "Morales", "Torres", "Ortiz", "Herrera",
    "Medina", "Aguilar", "Can", "Kú", "Ceh", "Novelo", "Zapata", "Rosado", "Cetina", "Ávila",
]
TUTORES = [
    ("tutor01", "Mtra. Laura Pech Canul"),
    ("tutor02", "Dr. Jorge Alberto Uc May"),
    ("tutor03", "Mtro. Ricardo Zapata Ek"),
    ("tutor04", "Dra. Silvia Cetina Dzib"),
    ("tutor05", "Mtro. Fernando Novelo Kú"),
    ("tutor06", "Mtra. Karina Rosado Chi"),
]
CONTRASENA_DEMO = "demo2026"
MAX_CARGA = 6


def _prob_reprobar(dificultad: float, habilidad: float) -> float:
    p = dificultad * (2.1 - 1.9 * habilidad) * 0.62
    return max(0.01, min(0.85, p))


def _calif(aprueba: bool, habilidad: float, rnd: random.Random) -> int:
    minimo = reglas()["escala"]["minima_aprobatoria"]
    if aprueba:
        return max(minimo, min(10, round(minimo + (10 - minimo) * min(1.0, habilidad + rnd.uniform(-0.2, 0.25)))))
    return rnd.randint(2, minimo - 1)


def _generar_alumno(rnd: random.Random, inicio: int, tutor: str, matricula: str) -> dict | None:
    materias = reticula()["materias"]
    por_clave = {m["clave"]: m for m in materias}
    max_op = reglas()["oportunidades_max"]

    riesgo = rnd.random() < 0.15
    habilidad = rnd.betavariate(2.2, 2.8) if riesgo else rnd.betavariate(5.5, 2.0)

    aprobadas: set[str] = set()
    intentos: dict[str, int] = {}
    pendientes: set[str] = set()
    kardex: list[dict] = []

    def fila(clave: str, periodo: str, tipo: str, calif: int | str, estatus: str) -> dict:
        m = por_clave[clave]
        return {
            "clave": clave, "asignatura": m["nombre"], "creditos": m["creditos"],
            "semestre": m["semestre"], "periodo": periodo, "tipo": tipo,
            "calificacion": calif, "estatus": estatus,
        }

    def carga_del_periodo(nominal: int) -> list[str]:
        carga: list[str] = []
        for c in sorted(pendientes):
            if len(carga) < 2 and rnd.random() < 0.35:
                carga.append(c)
        disponibles = [
            m["clave"] for m in materias
            if m["semestre"] <= nominal and m["clave"] not in aprobadas
            and m["clave"] not in pendientes
            and all(p in aprobadas for p in m["prerrequisitos"])
        ]
        disponibles.sort(key=lambda c: por_clave[c]["semestre"])
        for c in disponibles:
            if len(carga) >= MAX_CARGA:
                break
            carga.append(c)
        return carga

    periodos = PERIODOS_CERRADOS[inicio:]
    for i, periodo in enumerate(periodos):
        for clave in carga_del_periodo(i + 1):
            m = por_clave[clave]
            intentos[clave] = intentos.get(clave, 0) + 1
            tipo = "Ordinario" if intentos[clave] == 1 else "Recursamiento"
            aprueba = rnd.random() > _prob_reprobar(m["dificultad"], habilidad)
            kardex.append(fila(clave, periodo, tipo, _calif(aprueba, habilidad, rnd),
                               "Aprobada" if aprueba else "Reprobada"))
            if aprueba:
                aprobadas.add(clave)
                pendientes.discard(clave)
                continue
            # Extraordinario en el mismo periodo (no todos lo presentan)
            if intentos[clave] < max_op and rnd.random() < 0.55:
                intentos[clave] += 1
                aprueba = rnd.random() < 0.6
                kardex.append(fila(clave, periodo, "Extraordinario", _calif(aprueba, habilidad, rnd),
                                   "Aprobada" if aprueba else "Reprobada"))
                if aprueba:
                    aprobadas.add(clave)
                    pendientes.discard(clave)
                    continue
            if intentos[clave] >= max_op:
                return None  # agotó oportunidades: baja por reglamento, ya no aparece en el listado
            pendientes.add(clave)

    # Periodo en curso
    nominal = len(periodos) + 1
    for clave in carga_del_periodo(nominal):
        intentos[clave] = intentos.get(clave, 0) + 1
        tipo = "Ordinario" if intentos[clave] == 1 else "Recursamiento"
        kardex.append(fila(clave, reglas()["periodo_actual"], tipo, "", "En curso"))

    return {
        "matricula": matricula,
        "programa": reticula()["programa"].split(" (")[0],
        "semestre": min(9, nominal),
        "tutor": tutor,
        "kardex": kardex,
    }


def generar(semilla: int = 2026, por_tutor: int = 32) -> dict:
    rnd = random.Random(semilla)
    alumnos: list[dict] = []
    usados: set[str] = set()
    consecutivo = 0
    for usuario, _ in TUTORES:
        creados = 0
        while creados < por_tutor:
            inicio = rnd.randint(0, len(PERIODOS_CERRADOS) - 1)
            consecutivo += 1
            anio = PERIODOS_CERRADOS[inicio][2:4]
            matricula = f"{anio}{rnd.randint(1, 9)}{consecutivo:05d}"
            alumno = _generar_alumno(rnd, inicio, usuario, matricula)
            if alumno is None:
                continue
            while True:
                nombre = f"{rnd.choice(NOMBRES)} {rnd.choice(APELLIDOS)} {rnd.choice(APELLIDOS)}"
                if nombre not in usados:
                    usados.add(nombre)
                    break
            alumno["nombre"] = nombre
            alumnos.append(alumno)
            creados += 1
    return {
        "aviso": "Datos 100 % sintéticos generados para el prototipo SIRA.",
        "tutores": [{"usuario": u, "nombre": n, "contrasena": CONTRASENA_DEMO} for u, n in TUTORES],
        "alumnos": alumnos,
    }


def asegurar_datos() -> dict:
    if not MOCK_PATH.exists():
        MOCK_PATH.write_text(json.dumps(generar(), ensure_ascii=False, indent=1), encoding="utf-8")
    return json.loads(MOCK_PATH.read_text(encoding="utf-8"))


if __name__ == "__main__":
    datos = generar()
    MOCK_PATH.write_text(json.dumps(datos, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"{len(datos['alumnos'])} alumnos sintéticos escritos en {MOCK_PATH}")
