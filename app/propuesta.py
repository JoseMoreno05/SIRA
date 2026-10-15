"""Propuesta de oferta intersemestral: Gemma argumenta, el código verifica cada dato."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from . import gemma
from .config import GEMMA_MODEL_OFICIO, reglas
from .db import conexion
from .motor import demanda


class MateriaPropuesta(BaseModel):
    clave: str
    grupos: int = Field(ge=1)
    cupo_estimado: int = Field(ge=1)
    prioridad: Literal["alta", "media", "baja"]
    justificacion: str = Field(min_length=20)


class Descartada(BaseModel):
    clave: str
    motivo: str


class Propuesta(BaseModel):
    resumen: str = Field(min_length=20)
    materias: list[MateriaPropuesta] = Field(min_length=1)
    descartadas: list[Descartada] = []


CAMPOS_FILA = ("posicion", "clave", "nombre", "semestre", "demanda_directa", "urgencia", "bloqueo",
               "cursando", "demanda_proyectada", "demanda_total", "indice", "grupos_sugeridos", "suficiente")


def entrada_para_gemma(ranking: dict) -> dict:
    """Lo único que ve Gemma: cifras por materia y restricciones. Ni un nombre ni una matrícula."""
    cfg = reglas()["demanda"]
    filas = [{k: m[k] for k in CAMPOS_FILA} for m in ranking["materias"][: cfg["top_n"]]]
    cob = ranking["cobertura"]
    return {
        "intersemestral": reglas()["intersemestral"],
        "restricciones": {"grupos_max": cfg["grupos_max"], "cupo_minimo": cfg["cupo_minimo"],
                          "cupo_maximo_grupo": cfg["cupo_maximo_grupo"]},
        "cobertura": {"tutores_sincronizados": cob["tutores_sincronizados"], "tutores_total": cob["tutores_total"],
                      "alumnos_cubiertos": cob["alumnos"]},
        "glosario": {
            "demanda_directa": "alumnos que adeudan la materia y no la cursan este periodo",
            "urgencia": "de ellos, alumnos en riesgo alto (semáforo rojo)",
            "bloqueo": "materias posteriores que dependen de esta en la retícula",
            "demanda_proyectada": "alumnos que la cursan hoy y probablemente la reprobarán, según la tasa histórica",
            "demanda_total": "demanda_directa + demanda_proyectada",
            "indice": "índice de impacto con que se ordena la tabla",
        },
        "materias": filas,
    }


_NUM = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?![\w])")


def _numeros(texto: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUM.findall(texto)}


def _permitidos_fila(f: dict) -> set[str]:
    base = {str(f[k]) for k in ("posicion", "semestre", "demanda_directa", "urgencia", "bloqueo", "cursando",
                                "demanda_proyectada", "demanda_total", "grupos_sugeridos")}
    base |= {str(f["indice"]), f"{f['indice']:.1f}", str(round(f["indice"]))}
    base |= {str(round(f["tasa_historica"] * 100))} if "tasa_historica" in f else set()
    return base


def validar(propuesta: dict, ranking: dict) -> tuple[Propuesta | None, list[dict]]:
    """Revisa la propuesta contra la base. Devuelve (propuesta, lista de verificaciones)."""
    cfg = reglas()["demanda"]
    checks: list[dict] = []

    def check(nombre: str, ok: bool, detalle: str = "") -> None:
        checks.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    try:
        p = Propuesta.model_validate(propuesta)
        check("Estructura del JSON", True, "Cumple el esquema esperado.")
    except ValidationError as e:
        check("Estructura del JSON", False, f"No cumple el esquema: {e.errors()[0]['msg']}")
        return None, checks

    filas = {m["clave"]: m for m in ranking["materias"]}
    claves = [m.clave for m in p.materias]

    inexistentes = [c for c in claves if c not in filas]
    check("Claves existentes", not inexistentes,
          f"Claves que no están en la tabla de demanda: {', '.join(inexistentes)}" if inexistentes
          else f"Las {len(claves)} claves existen en la tabla de demanda.")

    repetidas = sorted({c for c in claves if claves.count(c) > 1})
    check("Sin materias repetidas", not repetidas, f"Repetidas: {', '.join(repetidas)}" if repetidas else "Ninguna repetida.")

    insuf = [c for c in claves if c in filas and not filas[c]["suficiente"]]
    check("Cupo mínimo", not insuf,
          f"No alcanzan el cupo mínimo de {cfg['cupo_minimo']}: {', '.join(insuf)}" if insuf
          else f"Todas alcanzan el cupo mínimo de {cfg['cupo_minimo']} alumnos.")

    total_grupos = sum(m.grupos for m in p.materias)
    check("Grupos disponibles", total_grupos <= cfg["grupos_max"],
          f"Propone {total_grupos} grupos; el máximo es {cfg['grupos_max']}.")

    cupos_mal = [f"{m.clave} ({m.cupo_estimado} en lugar de {filas[m.clave]['demanda_total']})"
                 for m in p.materias if m.clave in filas and m.cupo_estimado != filas[m.clave]["demanda_total"]]
    check("Cupos contra la base", not cupos_mal,
          "Cupos distintos a la demanda calculada: " + "; ".join(cupos_mal) if cupos_mal
          else "Cada cupo coincide con la demanda total calculada.")

    inventadas = []
    genericos = {"1", "2", "3"}
    for m in p.materias:
        if m.clave not in filas:
            continue
        sobrantes = _numeros(m.justificacion) - _permitidos_fila(filas[m.clave]) - genericos - {str(m.grupos)}
        if sobrantes:
            inventadas.append(f"{m.clave}: {', '.join(sorted(sobrantes))}")
    globales = {str(total_grupos), str(len(p.materias)), str(ranking["cobertura"]["alumnos"]),
                str(ranking["cobertura"]["tutores_sincronizados"]), str(ranking["cobertura"]["tutores_total"]),
                str(sum(filas[c]["demanda_total"] for c in claves if c in filas)),
                str(sum(filas[c]["urgencia"] for c in claves if c in filas)),
                str(sum(filas[c]["demanda_directa"] for c in claves if c in filas))}
    globales |= {n for c in claves if c in filas for n in _permitidos_fila(filas[c])} | genericos
    globales |= _numeros(reglas()["intersemestral"])
    sobrantes_resumen = _numeros(p.resumen) - globales
    if sobrantes_resumen:
        inventadas.append(f"resumen: {', '.join(sorted(sobrantes_resumen))}")
    check("Cifras respaldadas", not inventadas,
          "Cifras que no aparecen en los datos: " + "; ".join(inventadas) if inventadas
          else "Cada cifra del texto aparece en los datos calculados.")

    return (p if all(c["ok"] for c in checks) else None), checks


def plantilla(ranking: dict) -> dict:
    """Propuesta determinista: se usa sin API key o si Gemma no pasa la validación."""
    cfg = reglas()["demanda"]
    elegidas, grupos = [], 0
    for m in ranking["materias"]:
        if not m["suficiente"] or grupos + m["grupos_sugeridos"] > cfg["grupos_max"]:
            continue
        grupos += m["grupos_sugeridos"]
        prioridad = "alta" if m["urgencia"] >= 3 or m["bloqueo"] >= 5 else "media" if m["urgencia"] >= 1 else "baja"
        d, u, b, pr = m["demanda_directa"], m["urgencia"], m["bloqueo"], m["demanda_proyectada"]
        texto = f"La {'adeuda 1 alumno' if d == 1 else f'adeudan {d} alumnos'}"
        if u:
            texto += f", {u} en riesgo alto" if d > 1 else ", en riesgo alto"
        texto += f", y bloquea {b} {'materia posterior' if b == 1 else 'materias posteriores'} de la retícula."
        if pr:
            texto += (f" Se suma{'' if pr == 1 else 'n'} {pr} {'alumno' if pr == 1 else 'alumnos'} que "
                      f"probablemente no la {'acredite' if pr == 1 else 'acrediten'} este periodo.")
        elegidas.append({"clave": m["clave"], "grupos": m["grupos_sugeridos"], "cupo_estimado": m["demanda_total"],
                         "prioridad": prioridad, "justificacion": texto})
    claves = {e["clave"] for e in elegidas}
    descartadas = [{"clave": m["clave"], "motivo": "No alcanza el cupo mínimo." if not m["suficiente"]
                    else "Sin grupos disponibles tras priorizar materias de mayor impacto."}
                   for m in ranking["materias"][: cfg["top_n"]] if m["clave"] not in claves]
    alumnos = sum(m["cupo_estimado"] for m in elegidas)
    resumen = (f"Se propone abrir {len(elegidas)} materias con {grupos} grupos en total, que atienden a {alumnos} "
               "alumnos. La prioridad se asignó por número de alumnos que adeudan cada materia, alumnos en riesgo "
               "alto y materias bloqueadas por seriación.")
    return {"resumen": resumen, "materias": elegidas, "descartadas": descartadas}


def generar() -> dict:
    """Corre el flujo completo y guarda el resultado."""
    ranking = demanda()
    entrada = entrada_para_gemma(ranking)
    origen, modelo, intentos = "plantilla", None, []
    resultado, checks = None, []

    if gemma.disponible():
        correcciones = None
        for _ in range(2):
            try:
                crudo = gemma.proponer_oferta(entrada, correcciones)
            except (gemma.GemmaNoDisponible, json.JSONDecodeError) as e:
                intentos.append({"ok": False, "detalle": f"Gemma no respondió con JSON válido: {e}"})
                break
            resultado, checks = validar(crudo, ranking)
            intentos.append({"ok": resultado is not None, "verificaciones": checks})
            if resultado is not None:
                origen, modelo = "gemma", GEMMA_MODEL_OFICIO
                break
            correcciones = [c["detalle"] for c in checks if not c["ok"]]

    if resultado is None:
        resultado, checks = validar(plantilla(ranking), ranking)
        origen = "plantilla"

    filas = {m["clave"]: m for m in ranking["materias"]}
    contenido = {
        **resultado.model_dump(),
        "datos": {m.clave: filas[m.clave] for m in resultado.materias},
        "cobertura": ranking["cobertura"],
        "parametros": ranking["parametros"],
        "intersemestral": reglas()["intersemestral"],
    }
    registro = {"origen": origen, "modelo": modelo, "verificaciones": checks, "intentos_gemma": intentos}
    with conexion() as con:
        cur = con.execute(
            "INSERT INTO propuesta(creada, origen, modelo, contenido, validacion) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), origen, modelo,
             json.dumps(contenido, ensure_ascii=False), json.dumps(registro, ensure_ascii=False)),
        )
        pid = cur.lastrowid
    return ultima(pid)


def ultima(pid: int | None = None) -> dict | None:
    with conexion() as con:
        fila = con.execute(
            "SELECT * FROM propuesta WHERE id=?" if pid else "SELECT * FROM propuesta ORDER BY id DESC LIMIT 1",
            (pid,) if pid else (),
        ).fetchone()
    if not fila:
        return None
    return {"id": fila["id"], "creada": fila["creada"], "origen": fila["origen"], "modelo": fila["modelo"],
            "contenido": json.loads(fila["contenido"]), "validacion": json.loads(fila["validacion"])}


def demostrar_rechazo(prop: dict) -> list[dict]:
    """Inserta una cifra inventada en la primera justificación y muestra cómo la validación la rechaza."""
    alterada = {k: prop["contenido"][k] for k in ("resumen", "materias", "descartadas")}
    alterada = json.loads(json.dumps(alterada))
    alterada["materias"][0]["justificacion"] += " Además, 87 alumnos ya se preinscribieron."
    _, checks = validar(alterada, demanda())
    return checks
