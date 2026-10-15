"""Propuesta de oferta semestral: el código calcula los grupos, Gemma redacta y SIRA verifica."""
from __future__ import annotations

import json
from datetime import datetime

from pydantic import BaseModel, Field, ValidationError

from . import gemma
from .config import GEMMA_MODEL_OFICIO
from .db import conexion
from .motor import oferta_semestral
from .propuesta import _numeros

CAMPOS = ("clave", "nombre", "semestre", "seguros", "condicionados", "demanda_esperada",
          "recursan", "en_riesgo", "grupos", "abre")


class Observacion(BaseModel):
    clave: str
    texto: str = Field(min_length=15)


class Redaccion(BaseModel):
    resumen: str = Field(min_length=30)
    observaciones: list[Observacion] = []


def entrada_para_gemma(oferta: dict) -> dict:
    """Solo cifras por materia y totales. Ni un nombre ni una matrícula."""
    cob = oferta["cobertura"]
    return {
        "periodo": oferta["nombre"],
        "totales": oferta["totales"],
        "parametros": oferta["parametros"],
        "cobertura": {"tutores_sincronizados": cob["tutores_sincronizados"], "tutores_total": cob["tutores_total"],
                      "alumnos_cubiertos": cob["alumnos"]},
        "nota": "No incluye alumnos de nuevo ingreso. 'condicionados' supone que acreditan lo que cursan hoy.",
        "materias": [{k: m[k] for k in CAMPOS} for m in oferta["materias"]],
    }


def _permitidos(fila: dict) -> set[str]:
    return {str(fila[k]) for k in ("semestre", "seguros", "condicionados", "demanda_esperada",
                                   "recursan", "en_riesgo", "grupos")}


def validar(redaccion: dict, oferta: dict) -> tuple[Redaccion | None, list[dict]]:
    checks: list[dict] = []

    def check(nombre: str, ok: bool, detalle: str) -> None:
        checks.append({"nombre": nombre, "ok": ok, "detalle": detalle})

    try:
        r = Redaccion.model_validate(redaccion)
        check("Estructura del JSON", True, "Cumple el esquema esperado.")
    except ValidationError as e:
        check("Estructura del JSON", False, f"No cumple el esquema: {e.errors()[0]['msg']}")
        return None, checks

    filas = {m["clave"]: m for m in oferta["materias"]}
    malas = [o.clave for o in r.observaciones if o.clave not in filas]
    check("Claves existentes", not malas,
          f"Claves que no están en la oferta: {', '.join(malas)}" if malas
          else f"Las {len(r.observaciones)} observaciones se refieren a materias de la oferta.")

    globales = {str(v) for v in oferta["totales"].values()} | {str(v) for v in oferta["parametros"].values()}
    cob = oferta["cobertura"]
    globales |= {str(cob["alumnos"]), str(cob["tutores_sincronizados"]), str(cob["tutores_total"])}
    globales |= _numeros(oferta["nombre"]) | {"1", "2", "3"}
    todas = set().union(*(_permitidos(f) for f in filas.values())) if filas else set()

    inventadas = []
    for o in r.observaciones:
        if o.clave in filas:
            sobra = _numeros(o.texto) - _permitidos(filas[o.clave]) - globales
            if sobra:
                inventadas.append(f"{o.clave}: {', '.join(sorted(sobra))}")
    sobra = _numeros(r.resumen) - globales - todas
    if sobra:
        inventadas.append(f"resumen: {', '.join(sorted(sobra))}")
    check("Cifras respaldadas", not inventadas,
          "Cifras que no aparecen en los datos: " + "; ".join(inventadas) if inventadas
          else "Cada cifra del texto aparece en la oferta calculada.")
    return (r if all(c["ok"] for c in checks) else None), checks


def plantilla(oferta: dict) -> dict:
    t = oferta["totales"]
    sin_grupo = [m for m in oferta["materias"] if not m["abre"]]
    resumen = (f"Para el {oferta['nombre'].lower()} se sugiere abrir {t['grupos']} grupos en "
               f"{t['materias_con_grupo']} materias, a partir de la carga sugerida de {t['alumnos']} alumnos. "
               f"La oferta incluye {t['recursamientos']} lugares de recursamiento. "
               "Los alumnos en riesgo alto llevan carga reducida y las materias condicionadas se ponderan por la "
               "probabilidad histórica de acreditar su prerrequisito.")
    textos: dict[str, list[str]] = {}
    for m in sorted(oferta["materias"], key=lambda m: -m["recursan"])[:2]:
        if m["recursan"]:
            textos.setdefault(m["clave"], []).append(
                f"Concentra {m['recursan']} recursamientos; conviene asignar asesoría académica desde la primera semana.")
    for m in sorted(oferta["materias"], key=lambda m: -m["en_riesgo"])[:2]:
        if m["en_riesgo"] and m["clave"] not in textos:
            textos.setdefault(m["clave"], []).append(
                f"La inscribirán {m['en_riesgo']} alumnos en riesgo alto; se sugiere un docente con experiencia en tutoría.")
    for m in sorted(sin_grupo, key=lambda m: -m["demanda_esperada"])[:2]:
        textos.setdefault(m["clave"], []).append(
            f"Su demanda esperada ({m['demanda_esperada']}) no alcanza el cupo mínimo: {m['recomendacion'].lower()}.")
    obs = [{"clave": c, "texto": " ".join(t)} for c, t in textos.items()]
    return {"resumen": resumen, "observaciones": obs}


def generar() -> dict:
    oferta = oferta_semestral()
    entrada = entrada_para_gemma(oferta)
    origen, modelo, intentos = "plantilla", None, []
    resultado, checks = None, []
    if gemma.disponible():
        correcciones = None
        for _ in range(2):
            try:
                crudo = gemma.redactar_semestral(entrada, correcciones)
            except (gemma.GemmaNoDisponible, json.JSONDecodeError) as e:
                intentos.append({"ok": False, "detalle": f"Gemma no respondió con JSON válido: {e}"})
                break
            resultado, checks = validar(crudo, oferta)
            intentos.append({"ok": resultado is not None, "verificaciones": checks})
            if resultado is not None:
                origen, modelo = "gemma", GEMMA_MODEL_OFICIO
                break
            correcciones = [c["detalle"] for c in checks if not c["ok"]]
    if resultado is None:
        resultado, checks = validar(plantilla(oferta), oferta)
        origen = "plantilla"
    contenido = {**resultado.model_dump(), "oferta": oferta}
    registro = {"origen": origen, "modelo": modelo, "verificaciones": checks, "intentos_gemma": intentos}
    with conexion() as con:
        cur = con.execute(
            "INSERT INTO propuesta_semestral(creada, origen, modelo, contenido, validacion) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), origen, modelo,
             json.dumps(contenido, ensure_ascii=False), json.dumps(registro, ensure_ascii=False)))
        pid = cur.lastrowid
    return ultima(pid)


def ultima(pid: int | None = None) -> dict | None:
    with conexion() as con:
        fila = con.execute(
            "SELECT * FROM propuesta_semestral WHERE id=?" if pid
            else "SELECT * FROM propuesta_semestral ORDER BY id DESC LIMIT 1",
            (pid,) if pid else ()).fetchone()
    if not fila:
        return None
    return {"id": fila["id"], "creada": fila["creada"], "origen": fila["origen"], "modelo": fila["modelo"],
            "contenido": json.loads(fila["contenido"]), "validacion": json.loads(fila["validacion"])}
