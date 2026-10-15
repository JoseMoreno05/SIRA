"""Cálculos deterministas de SIRA: semáforo de riesgo y demanda por materia.

Todo número que aparece en pantalla o en la propuesta sale de aquí, no de la IA.
"""
from __future__ import annotations

import math
from collections import defaultdict
from functools import lru_cache
from statistics import mean

import networkx as nx

from .config import materias_por_clave, reglas, reticula
from .db import conexion

ORDEN_NIVEL = {"rojo": 0, "amarillo": 1, "verde": 2}
ETIQUETA_NIVEL = {"rojo": "Intervenir ya", "amarillo": "Dar seguimiento", "verde": "Sin alerta"}


@lru_cache
def grafo_seriacion() -> nx.DiGraph:
    g = nx.DiGraph()
    for m in reticula()["materias"]:
        g.add_node(m["clave"])
        for p in m["prerrequisitos"]:
            g.add_edge(p, m["clave"])
    return g


@lru_cache
def bloqueo(clave: str) -> int:
    """Cuántas materias posteriores dependen (directa o indirectamente) de esta."""
    g = grafo_seriacion()
    return len(nx.descendants(g, clave)) if clave in g else 0


def _registros_por_alumno(tutor: str | None = None) -> dict[str, list[dict]]:
    sql = ("SELECT c.* FROM calificacion c JOIN alumno a ON a.seudonimo=c.seudonimo"
           + (" WHERE a.tutor=?" if tutor else "") + " ORDER BY c.periodo, c.rowid")
    with conexion() as con:
        filas = con.execute(sql, (tutor,) if tutor else ()).fetchall()
    out: dict[str, list[dict]] = defaultdict(list)
    for f in filas:
        out[f["seudonimo"]].append(dict(f))
    return out


def evaluar_alumno(registros: list[dict]) -> dict:
    """Aplica las reglas del semáforo a un kárdex normalizado."""
    r = reglas()
    sem = r["semaforo"]
    max_op = r["oportunidades_max"]
    actual = r["periodo_actual"]
    catalogo = materias_por_clave()

    intentos: dict[str, int] = defaultdict(int)
    aprobadas: set[str] = set()
    en_curso: dict[str, int] = {}
    por_periodo: dict[str, list[float]] = defaultdict(list)
    creditos = 0
    for x in registros:
        if x["estatus"] == "En curso" or x["periodo"] == actual:
            en_curso[x["clave"]] = x["oportunidad"]
            continue
        intentos[x["clave"]] += 1
        if x["calificacion"] is not None:
            por_periodo[x["periodo"]].append(x["calificacion"])
        if x["estatus"] == "Aprobada" and x["clave"] not in aprobadas:
            aprobadas.add(x["clave"])
            creditos += catalogo.get(x["clave"], {}).get("creditos", 0)

    adeudadas = []
    for clave, n in intentos.items():
        if clave in aprobadas:
            continue
        cursando = clave in en_curso
        usadas = en_curso[clave] if cursando else n
        adeudadas.append({
            "clave": clave,
            "nombre": catalogo.get(clave, {}).get("nombre", clave),
            "usadas": usadas,
            "restantes": max(0, max_op - usadas) if cursando else max(0, max_op - n),
            "en_curso": cursando,
            "bloqueo": bloqueo(clave),
        })
    adeudadas.sort(key=lambda a: (a["restantes"], -a["bloqueo"]))

    periodos = sorted(por_periodo)
    promedio = round(mean(v for p in periodos for v in por_periodo[p]), 2) if periodos else None
    caida = None
    if len(periodos) >= 2:
        caida = round(mean(por_periodo[periodos[-2]]) - mean(por_periodo[periodos[-1]]), 2)

    razones_rojo, razones_amarillo = [], []
    for a in adeudadas:
        if a["en_curso"] and a["restantes"] == 0:
            razones_rojo.append(f"Cursa {a['nombre']} en su última oportunidad.")
        elif not a["en_curso"] and a["restantes"] == 1:
            razones_rojo.append(f"{a['nombre']} está en su última oportunidad: le queda 1 de {max_op}.")
    pendientes = [a for a in adeudadas if not a["en_curso"]]
    if len(adeudadas) >= sem["rojo_adeudadas_min"]:
        razones_rojo.append(f"Adeuda {len(adeudadas)} materias.")
    for a in adeudadas:
        if a["bloqueo"] >= sem["rojo_bloqueo_min"]:
            razones_rojo.append(f"{a['nombre']} bloquea {a['bloqueo']} materias posteriores de la retícula.")
    if not razones_rojo:
        if len(adeudadas) >= sem["amarillo_adeudadas_min"]:
            n = len(adeudadas)
            razones_amarillo.append(f"Adeuda {n} materia{'s' if n > 1 else ''}.")
        if caida is not None and caida >= sem["amarillo_caida_promedio"]:
            razones_amarillo.append(f"Su promedio bajó {caida:.1f} puntos respecto al periodo anterior.")

    nivel = "rojo" if razones_rojo else "amarillo" if razones_amarillo else "verde"
    return {
        "nivel": nivel,
        "etiqueta": ETIQUETA_NIVEL[nivel],
        "razones": razones_rojo or razones_amarillo or ["Sin adeudos y con avance regular."],
        "adeudadas": adeudadas,
        "pendientes_intersemestral": [a["clave"] for a in pendientes],
        "promedio": promedio,
        "caida_promedio": caida,
        "creditos": creditos,
        "en_curso": sorted(en_curso),
    }


def panel_tutor(tutor: str) -> dict:
    regs = _registros_por_alumno(tutor)
    with conexion() as con:
        alumnos = con.execute(
            "SELECT a.seudonimo, a.semestre, i.nombre, i.matricula, s.contactado, s.nota "
            "FROM alumno a JOIN identidad i ON i.seudonimo=a.seudonimo "
            "LEFT JOIN seguimiento s ON s.seudonimo=a.seudonimo WHERE a.tutor=?", (tutor,)
        ).fetchall()
    filas = []
    for a in alumnos:
        ev = evaluar_alumno(regs.get(a["seudonimo"], []))
        filas.append({**dict(a), **ev, "max_bloqueo": max((x["bloqueo"] for x in ev["adeudadas"]), default=0)})
    filas.sort(key=lambda f: (ORDEN_NIVEL[f["nivel"]], -f["max_bloqueo"], -len(f["adeudadas"]), f["nombre"]))
    conteo = {n: sum(1 for f in filas if f["nivel"] == n) for n in ORDEN_NIVEL}
    return {"alumnos": filas, "conteo": conteo}


def _tasas_historicas() -> dict[str, float]:
    """Proporción de inscripciones que terminan reprobadas, promedio de los últimos periodos cerrados."""
    r = reglas()
    with conexion() as con:
        filas = con.execute(
            "SELECT seudonimo, clave, periodo, estatus, tipo FROM calificacion WHERE periodo < ? ORDER BY rowid",
            (r["periodo_actual"],),
        ).fetchall()
    final: dict[tuple, str] = {}
    for f in filas:
        final[(f["seudonimo"], f["clave"], f["periodo"])] = f["estatus"]  # el extraordinario sobrescribe al ordinario
    periodos = sorted({k[2] for k in final})[-r["demanda"]["periodos_historia"]:]
    por_materia: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    for (s, clave, p), est in final.items():
        if p in periodos:
            por_materia[clave][p].append(1 if est == "Reprobada" else 0)
    return {
        clave: round(mean(mean(v) for v in pp.values()), 3)
        for clave, pp in por_materia.items()
    }


def cobertura() -> dict:
    with conexion() as con:
        total = con.execute("SELECT COUNT(*) FROM tutor").fetchone()[0]
        sinc = con.execute("SELECT COUNT(*) FROM tutor WHERE ultima_sincronizacion IS NOT NULL").fetchone()[0]
        alumnos = con.execute("SELECT COUNT(*) FROM alumno").fetchone()[0]
        tutores = [dict(t) for t in con.execute("SELECT usuario, nombre, ultima_sincronizacion FROM tutor ORDER BY usuario")]
    return {"tutores_total": total, "tutores_sincronizados": sinc, "alumnos": alumnos, "tutores": tutores}


def demanda(beta: float | None = None, gamma: float | None = None) -> dict:
    """Ranking de materias para el intersemestral con el índice de impacto I = D·(1+βB)·(1+γU/D)."""
    r = reglas()
    cfg = r["demanda"]
    beta = cfg["beta"] if beta is None else beta
    gamma = cfg["gamma"] if gamma is None else gamma
    catalogo = materias_por_clave()
    regs = _registros_por_alumno()
    tasas = _tasas_historicas()

    D: dict[str, int] = defaultdict(int)
    U: dict[str, int] = defaultdict(int)
    cursando: dict[str, int] = defaultdict(int)
    for s, rr in regs.items():
        ev = evaluar_alumno(rr)
        for clave in ev["pendientes_intersemestral"]:
            D[clave] += 1
            if ev["nivel"] == "rojo":
                U[clave] += 1
        for clave in ev["en_curso"]:
            cursando[clave] += 1

    filas = []
    for clave in set(D) | set(cursando):
        d, u, b = D[clave], U[clave], bloqueo(clave)
        tasa = tasas.get(clave, 0.0)
        proyectada = round(cursando[clave] * tasa)
        total = d + proyectada
        indice = round(d * (1 + beta * b) * (1 + gamma * (u / d)), 2) if d else 0.0
        if total == 0:
            continue
        filas.append({
            "clave": clave,
            "nombre": catalogo[clave]["nombre"],
            "semestre": catalogo[clave]["semestre"],
            "demanda_directa": d,
            "urgencia": u,
            "bloqueo": b,
            "cursando": cursando[clave],
            "tasa_historica": tasa,
            "demanda_proyectada": proyectada,
            "demanda_total": total,
            "indice": indice,
            "grupos_sugeridos": max(1, math.ceil(total / cfg["cupo_maximo_grupo"])),
            "suficiente": total >= cfg["cupo_minimo"],
        })
    filas.sort(key=lambda f: (-f["indice"], -f["demanda_total"], f["clave"]))
    for i, f in enumerate(filas, 1):
        f["posicion"] = i
    return {
        "materias": filas,
        "parametros": {"beta": beta, "gamma": gamma, **{k: cfg[k] for k in ("cupo_minimo", "cupo_maximo_grupo", "grupos_max")}},
        "cobertura": cobertura(),
    }


# ------------------------------------------------------------------ propuesta semestral
def carga_sugerida(registros: list[dict], nivel: str) -> dict:
    """Materias que el alumno debería inscribir el próximo semestre.

    Supuesto explícito: acredita lo que cursa hoy. Las materias que dependen de algo
    que cursa hoy se marcan como condicionadas, y la oferta las pondera por la
    probabilidad histórica de acreditar ese prerrequisito.
    """
    r = reglas()
    cfg = r["semestral"]
    max_op = r["oportunidades_max"]
    actual = r["periodo_actual"]
    catalogo = materias_por_clave()

    aprobadas: set[str] = set()
    intentos: dict[str, int] = defaultdict(int)
    en_curso: dict[str, int] = {}
    for x in registros:
        if x["estatus"] == "En curso" or x["periodo"] == actual:
            en_curso[x["clave"]] = x["oportunidad"]
            continue
        intentos[x["clave"]] += 1
        if x["estatus"] == "Aprobada":
            aprobadas.add(x["clave"])
    previstas = aprobadas | set(en_curso)

    limite = cfg["carga_max_materias_riesgo"] if nivel == "rojo" else cfg["carga_max_materias"]
    elegidas: list[dict] = []
    creditos = 0

    def agregar(clave: str, tipo: str, motivo: str) -> bool:
        nonlocal creditos
        m = catalogo[clave]
        if len(elegidas) >= limite or creditos + m["creditos"] > cfg["carga_max_creditos"]:
            return False
        condicion = [p for p in m["prerrequisitos"] if p in en_curso and p not in aprobadas]
        elegidas.append({
            "clave": clave, "nombre": m["nombre"], "semestre": m["semestre"], "creditos": m["creditos"],
            "tipo": tipo, "motivo": motivo, "condicionada_a": condicion,
            "condicionada_nombres": [catalogo[p]["nombre"] for p in condicion],
        })
        creditos += m["creditos"]
        return True

    # 1. Adeudadas que no cursa hoy: primero las de menos oportunidades restantes
    pendientes = [c for c in intentos if c not in aprobadas and c not in en_curso and c in catalogo]
    pendientes.sort(key=lambda c: (max_op - intentos[c], -bloqueo(c)))
    for c in pendientes:
        restantes = max_op - intentos[c]
        motivo = ("Recursamiento en su última oportunidad: requiere asesoría desde el inicio."
                  if restantes == 1 else "Materia adeudada: recuperarla libera la seriación.")
        agregar(c, "recursamiento", motivo)

    # 2. Materias nuevas con prerrequisitos cubiertos, de los semestres más bajos primero
    nuevas = [m for m in reticula()["materias"]
              if m["clave"] not in previstas and m["clave"] not in intentos
              and all(p in previstas for p in m["prerrequisitos"])]
    nuevas.sort(key=lambda m: (m["semestre"], -bloqueo(m["clave"]), m["clave"]))
    for m in nuevas:
        if not agregar(m["clave"], "nueva", f"Siguiente en la retícula (semestre {m['semestre']})."):
            if len(elegidas) >= limite:
                break

    return {
        "periodo": cfg["periodo"],
        "materias": elegidas,
        "creditos": creditos,
        "limite_materias": limite,
        "limite_creditos": cfg["carga_max_creditos"],
        "carga_reducida": nivel == "rojo",
    }


def oferta_semestral() -> dict:
    """Agrega las cargas sugeridas: demanda esperada y grupos por materia para el próximo semestre."""
    cfg = reglas()["semestral"]
    catalogo = materias_por_clave()
    tasas = _tasas_historicas()
    regs = _registros_por_alumno()

    seguros: dict[str, int] = defaultdict(int)
    condicionados: dict[str, int] = defaultdict(int)
    esperada: dict[str, float] = defaultdict(float)
    recursan: dict[str, int] = defaultdict(int)
    riesgo: dict[str, int] = defaultdict(int)
    alumnos_con_carga = 0
    for s, rr in regs.items():
        ev = evaluar_alumno(rr)
        carga = carga_sugerida(rr, ev["nivel"])
        if carga["materias"]:
            alumnos_con_carga += 1
        for m in carga["materias"]:
            p = 1.0
            for pre in m["condicionada_a"]:
                p *= 1 - tasas.get(pre, 0.0)
            esperada[m["clave"]] += p
            if m["condicionada_a"]:
                condicionados[m["clave"]] += 1
            else:
                seguros[m["clave"]] += 1
            if m["tipo"] == "recursamiento":
                recursan[m["clave"]] += 1
            if ev["nivel"] == "rojo":
                riesgo[m["clave"]] += 1

    filas = []
    for clave in esperada:
        e = round(esperada[clave])
        abre = e >= cfg["cupo_minimo"]
        filas.append({
            "clave": clave,
            "nombre": catalogo[clave]["nombre"],
            "semestre": catalogo[clave]["semestre"],
            "seguros": seguros[clave],
            "condicionados": condicionados[clave],
            "demanda_esperada": e,
            "recursan": recursan[clave],
            "en_riesgo": riesgo[clave],
            "grupos": math.ceil(e / cfg["cupo_maximo_grupo"]) if abre else 0,
            "abre": abre,
            "recomendacion": (
                f"Abrir {math.ceil(e / cfg['cupo_maximo_grupo'])} "
                f"{'grupo' if math.ceil(e / cfg['cupo_maximo_grupo']) == 1 else 'grupos'}" if abre
                else "Integrar a los alumnos al grupo de nuevo ingreso" if catalogo[clave]["semestre"] == 1
                else "Integrar a un grupo existente u ofrecer asesoría dirigida"
            ),
        })
    filas.sort(key=lambda f: (f["semestre"], f["clave"]))
    return {
        "periodo": cfg["periodo"],
        "nombre": cfg["nombre"],
        "materias": filas,
        "totales": {
            "materias_con_grupo": sum(1 for f in filas if f["abre"]),
            "grupos": sum(f["grupos"] for f in filas),
            "alumnos": alumnos_con_carga,
            "recursamientos": sum(f["recursan"] for f in filas),
        },
        "parametros": {k: cfg[k] for k in ("cupo_minimo", "cupo_maximo_grupo", "carga_max_materias",
                                            "carga_max_materias_riesgo", "carga_max_creditos")},
        "cobertura": cobertura(),
    }
