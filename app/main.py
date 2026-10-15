"""SIRA · Sistema Inteligente de Retención Académica (prototipo)."""
from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import conector, db, gemma, motor, oficio, propuesta, semestral
from .config import GEMMA_MODEL, GEMMA_MODEL_OFICIO, RAIZ, SIIAA_BASE_URL, reglas
from .datos_sinteticos import CONTRASENA_DEMO, asegurar_datos
from .siiaa_replica import router as replica

@asynccontextmanager
async def ciclo_de_vida(_app):
    datos = asegurar_datos()
    db.inicializar([{"usuario": t["usuario"], "nombre": t["nombre"]} for t in datos["tutores"]])
    yield


app = FastAPI(title="SIRA", docs_url="/api/docs", redoc_url=None, lifespan=ciclo_de_vida)
app.include_router(replica)
app.mount("/static", StaticFiles(directory=RAIZ / "app" / "static"), name="static")
plantillas = Jinja2Templates(directory=RAIZ / "app" / "templates")

MESES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]


def fecha_corta(iso: str | None) -> str:
    if not iso:
        return ""
    d = datetime.fromisoformat(iso)
    return f"{d.day} {MESES[d.month - 1]} {d.year}, {d:%H:%M}"


plantillas.env.filters["fecha"] = fecha_corta
plantillas.env.globals.update(
    gemma_activo=gemma.disponible, modelo=GEMMA_MODEL, modelo_oficio=GEMMA_MODEL_OFICIO,
    max_op=reglas()["oportunidades_max"], intersemestral=reglas()["intersemestral"],
    semestre_siguiente=reglas()["semestral"]["nombre"],
    nombres_oportunidad=reglas()["nombres_oportunidad"],
)


# ------------------------------------------------------------------ sesión de demo
def sesion(request: Request) -> dict:
    """La demo no tiene cuentas propias: el visitante elige un perfil."""
    rol = request.cookies.get("sira_rol", "")
    if rol.startswith("tutor:"):
        usuario = rol.split(":", 1)[1]
        with db.conexion() as con:
            t = con.execute("SELECT * FROM tutor WHERE usuario=?", (usuario,)).fetchone()
        if t:
            return {"rol": "tutor", "usuario": usuario, "nombre": t["nombre"],
                    "ultima_sincronizacion": t["ultima_sincronizacion"]}
    if rol == "gestor":
        return {"rol": "gestor", "nombre": "Jefatura del programa"}
    return {"rol": None}


def render(request: Request, plantilla: str, **ctx) -> HTMLResponse:
    return plantillas.TemplateResponse(request, plantilla, {"s": sesion(request), **ctx})


def exigir(request: Request, rol: str) -> dict:
    s = sesion(request)
    if s["rol"] != rol:
        raise HTTPException(status_code=303, headers={"Location": "/"})
    return s


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    with db.conexion() as con:
        tutores = [dict(t) for t in con.execute("SELECT usuario, nombre FROM tutor ORDER BY usuario")]
    return render(request, "inicio.html", tutores=tutores)


@app.post("/entrar")
def entrar(perfil: str = Form(...), tutor: str = Form("tutor01")):
    destino = "/tutor" if perfil == "tutor" else "/gestor"
    resp = RedirectResponse(destino, status_code=303)
    resp.set_cookie("sira_rol", f"tutor:{tutor}" if perfil == "tutor" else "gestor", samesite="lax", httponly=True)
    return resp


@app.get("/salir")
def salir():
    resp = RedirectResponse("/", status_code=303)
    resp.delete_cookie("sira_rol")
    return resp


# ------------------------------------------------------------------ tutor
@app.get("/tutor", response_class=HTMLResponse)
def tutor(request: Request, nivel: str = ""):
    s = exigir(request, "tutor")
    panel = motor.panel_tutor(s["usuario"])
    alumnos = [a for a in panel["alumnos"] if not nivel or a["nivel"] == nivel]
    return render(request, "tutor.html", panel=panel, alumnos=alumnos, filtro=nivel,
                  portal=SIIAA_BASE_URL, contrasena_demo=CONTRASENA_DEMO)


@app.post("/tutor/sincronizar")
def tutor_sincronizar(request: Request, usuario_portal: str = Form(...), contrasena_portal: str = Form(...)):
    s = exigir(request, "tutor")
    if usuario_portal != s["usuario"]:
        return JSONResponse({"ok": False, "mensaje": "Usa tu propio usuario del portal: SIRA solo sincroniza a tus tutorados."},
                            status_code=400)
    iniciada = conector.sincronizar_en_segundo_plano(usuario_portal, contrasena_portal)
    return {"ok": True, "iniciada": iniciada}


@app.get("/api/sincronizacion")
def estado_sincronizacion(request: Request):
    s = exigir(request, "tutor")
    e = conector.ESTADOS.get(s["usuario"])
    return e.como_dict() if e else {"fase": "inactivo"}


def _alumno_del_tutor(s: dict, seudonimo: str) -> dict:
    for a in motor.panel_tutor(s["usuario"])["alumnos"]:
        if a["seudonimo"] == seudonimo:
            return a
    raise HTTPException(status_code=404, detail="Ese alumno no está entre tus tutorados.")


@app.get("/tutor/alumno/{seudonimo}", response_class=HTMLResponse)
def alumno(request: Request, seudonimo: str):
    s = exigir(request, "tutor")
    a = _alumno_del_tutor(s, seudonimo)
    with db.conexion() as con:
        kardex = [dict(r) for r in con.execute(
            "SELECT c.*, m.nombre FROM calificacion c JOIN materia m ON m.clave=c.clave "
            "WHERE seudonimo=? ORDER BY periodo, c.rowid", (seudonimo,))]
        sug = con.execute("SELECT * FROM sugerencia WHERE seudonimo=?", (seudonimo,)).fetchone()
    regs = [dict(k) for k in kardex]
    carga = motor.carga_sugerida(regs, a["nivel"])
    return render(request, "alumno.html", a=a, kardex=kardex, sugerencia=dict(sug) if sug else None, carga=carga)


@app.post("/tutor/alumno/{seudonimo}/sugerencia")
def generar_sugerencia(request: Request, seudonimo: str):
    s = exigir(request, "tutor")
    a = _alumno_del_tutor(s, seudonimo)
    texto, origen = gemma.sugerir_intervencion(a, a["semestre"])
    with db.conexion() as con:
        con.execute("INSERT INTO sugerencia(seudonimo, texto, origen, creada) VALUES (?,?,?,?) "
                    "ON CONFLICT(seudonimo) DO UPDATE SET texto=excluded.texto, origen=excluded.origen, creada=excluded.creada",
                    (seudonimo, texto, origen, datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse(f"/tutor/alumno/{seudonimo}#sugerencia", status_code=303)


@app.post("/tutor/alumno/{seudonimo}/seguimiento")
def seguimiento(request: Request, seudonimo: str, contactado: str = Form(""), nota: str = Form("")):
    s = exigir(request, "tutor")
    _alumno_del_tutor(s, seudonimo)
    with db.conexion() as con:
        con.execute("INSERT INTO seguimiento(seudonimo, contactado, nota, actualizado) VALUES (?,?,?,?) "
                    "ON CONFLICT(seudonimo) DO UPDATE SET contactado=excluded.contactado, nota=excluded.nota, actualizado=excluded.actualizado",
                    (seudonimo, 1 if contactado else 0, nota.strip()[:2000], datetime.now().isoformat(timespec="seconds")))
    return RedirectResponse(f"/tutor/alumno/{seudonimo}?guardado=1#seguimiento", status_code=303)


# ------------------------------------------------------------------ gestor / jefatura
@app.get("/gestor", response_class=HTMLResponse)
def gestor(request: Request, beta: float | None = None, gamma: float | None = None):
    exigir(request, "gestor")
    ranking = motor.demanda(beta=beta, gamma=gamma)
    maximo = max((m["indice"] for m in ranking["materias"]), default=1) or 1
    return render(request, "gestor.html", r=ranking, maximo=maximo)


_sinc_todos = {"activo": False}


@app.post("/gestor/sincronizar-demo")
def sincronizar_todos(request: Request):
    """Solo en la demo: simula que cada tutor sincroniza sus tutorados con su propia cuenta."""
    exigir(request, "gestor")
    if not _sinc_todos["activo"]:
        def correr():
            _sinc_todos["activo"] = True
            try:
                for t in asegurar_datos()["tutores"]:
                    conector.sincronizar(t["usuario"], t["contrasena"], pausa=0.02)
            finally:
                _sinc_todos["activo"] = False
        threading.Thread(target=correr, daemon=True).start()
    return {"ok": True}


@app.get("/api/sincronizacion-demo")
def estado_sinc_todos(request: Request):
    exigir(request, "gestor")
    cob = motor.cobertura()
    return {"activo": _sinc_todos["activo"], "sincronizados": cob["tutores_sincronizados"], "total": cob["tutores_total"]}


@app.get("/gestor/propuesta", response_class=HTMLResponse)
def ver_propuesta(request: Request, prueba: int = 0):
    exigir(request, "gestor")
    p = propuesta.ultima()
    rechazo = propuesta.demostrar_rechazo(p) if (p and prueba) else None
    return render(request, "propuesta.html", p=p, rechazo=rechazo, cobertura=motor.cobertura())


@app.post("/gestor/propuesta")
def crear_propuesta(request: Request):
    exigir(request, "gestor")
    if motor.cobertura()["alumnos"] == 0:
        return RedirectResponse("/gestor?sin_datos=1", status_code=303)
    propuesta.generar()
    return RedirectResponse("/gestor/propuesta", status_code=303)


@app.get("/gestor/propuesta/oficio.{formato}")
def descargar_oficio(request: Request, formato: str):
    exigir(request, "gestor")
    p = propuesta.ultima()
    if not p:
        raise HTTPException(404, "Primero genera una propuesta.")
    if formato == "pdf":
        return Response(oficio.pdf(p), media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=propuesta-intersemestral.pdf"})
    if formato == "docx":
        return Response(oficio.docx(p),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": "attachment; filename=propuesta-intersemestral.docx"})
    raise HTTPException(404)


# ------------------------------------------------------------------ propuesta semestral
@app.get("/gestor/semestral", response_class=HTMLResponse)
def ver_semestral(request: Request):
    exigir(request, "gestor")
    return render(request, "semestral.html", p=semestral.ultima(), oferta=motor.oferta_semestral())


@app.post("/gestor/semestral")
def crear_semestral(request: Request):
    exigir(request, "gestor")
    if motor.cobertura()["alumnos"] == 0:
        return RedirectResponse("/gestor/semestral", status_code=303)
    semestral.generar()
    return RedirectResponse("/gestor/semestral#propuesta", status_code=303)


@app.get("/gestor/semestral/oficio.{formato}")
def descargar_semestral(request: Request, formato: str):
    exigir(request, "gestor")
    p = semestral.ultima()
    if not p:
        raise HTTPException(404, "Primero genera la propuesta semestral.")
    if formato == "pdf":
        return Response(oficio.pdf_semestral(p), media_type="application/pdf",
                        headers={"Content-Disposition": "attachment; filename=propuesta-semestral.pdf"})
    if formato == "docx":
        return Response(oficio.docx_semestral(p),
                        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                        headers={"Content-Disposition": "attachment; filename=propuesta-semestral.docx"})
    raise HTTPException(404)


# ------------------------------------------------------------------ transparencia
@app.get("/privacidad", response_class=HTMLResponse)
def privacidad(request: Request):
    ranking = motor.demanda()
    entrada = propuesta.entrada_para_gemma(ranking) if ranking["materias"] else None
    ejemplo = conector.seudonimo("259000123")
    return render(request, "privacidad.html",
                  entrada=json.dumps(entrada, ensure_ascii=False, indent=2) if entrada else None, ejemplo=ejemplo)


@app.get("/accesibilidad", response_class=HTMLResponse)
def accesibilidad(request: Request):
    ruta = RAIZ / "docs" / "accesibilidad-resultados.json"
    resultados = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else None
    return render(request, "accesibilidad.html", resultados=resultados)


@app.post("/demo/reiniciar")
def reiniciar(request: Request):
    db.reiniciar()
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@app.get("/salud")
def salud():
    return {"ok": True, "gemma": gemma.disponible()}
