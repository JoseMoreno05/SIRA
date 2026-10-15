"""Réplica del portal docente (SIIAA) con datos sintéticos.

Imita el recorrido que hace un tutor en el portal: iniciar sesión, abrir su listado
de tutorados y consultar el kárdex de cada uno. Sirve para que los jueces usen el
prototipo sin credenciales reales y sin exponer datos de ningún alumno.
"""
from __future__ import annotations

import html
import secrets

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from .datos_sinteticos import asegurar_datos

router = APIRouter(prefix="/siiaa")
_sesiones: dict[str, str] = {}

ESTILO = """
<style>
body{font-family:Verdana,Arial,sans-serif;font-size:13px;margin:0;background:#e9edf2;color:#222}
.barra{background:#1d3f6e;color:#fff;padding:10px 16px;font-weight:bold}
.aviso{background:#fff3cd;border-bottom:1px solid #e0c97a;padding:6px 16px;font-size:12px}
.caja{background:#fff;margin:16px;padding:16px;border:1px solid #c7ced8}
table{border-collapse:collapse;width:100%}th,td{border:1px solid #c7ced8;padding:4px 6px;text-align:left}
th{background:#dfe6ef}a{color:#1d3f6e}label{display:block;margin-top:8px}
input{padding:4px;margin-top:2px}.err{color:#a00}
</style>
"""


def _pagina(titulo: str, cuerpo: str) -> HTMLResponse:
    return HTMLResponse(
        f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>{titulo}</title>{ESTILO}</head>"
        f"<body><div class='barra'>Intranet Docentes · SIIAA (réplica)</div>"
        f"<div class='aviso'>Réplica de demostración para el prototipo SIRA. Todos los datos son sintéticos.</div>"
        f"{cuerpo}</body></html>"
    )


def _usuario(request: Request) -> str | None:
    return _sesiones.get(request.cookies.get("PHPSESSID", ""))


@router.get("/robots.txt", response_class=PlainTextResponse)
def robots() -> str:
    return "User-agent: *\nAllow: /\n"


@router.get("/", response_class=HTMLResponse)
def login_form(error: int = 0):
    msg = "<p class='err' role='alert'>Usuario o contraseña incorrectos.</p>" if error else ""
    return _pagina("Intranet Docentes", f"""
<div class='caja' style='max-width:320px'>
<h1 style='font-size:16px'>Acceso docentes</h1>{msg}
<form method='post' action='login.php'>
<label for='usuario'>Usuario</label><input id='usuario' name='usuario' autocomplete='username'>
<label for='contrasena'>Contraseña</label><input id='contrasena' name='contrasena' type='password' autocomplete='current-password'>
<p><button type='submit'>Entrar</button></p>
</form>
<p style='font-size:11px'>Demo: usuarios tutor01 a tutor06, contraseña demo2026.</p>
</div>""")


@router.post("/login.php")
def login(usuario: str = Form(""), contrasena: str = Form("")):
    datos = asegurar_datos()
    ok = any(t["usuario"] == usuario and t["contrasena"] == contrasena for t in datos["tutores"])
    if not ok:
        return RedirectResponse("/siiaa/?error=1", status_code=303)
    token = secrets.token_hex(16)
    _sesiones[token] = usuario
    resp = RedirectResponse("/siiaa/inicio.php", status_code=303)
    resp.set_cookie("PHPSESSID", token, httponly=True, samesite="lax")
    return resp


@router.get("/inicio.php", response_class=HTMLResponse)
def inicio(request: Request):
    usuario = _usuario(request)
    if not usuario:
        return RedirectResponse("/siiaa/", status_code=303)
    nombre = next(t["nombre"] for t in asegurar_datos()["tutores"] if t["usuario"] == usuario)
    return _pagina("Inicio", f"""
<div class='caja'><p>Bienvenido(a), {html.escape(nombre)}</p>
<ul id='menuPrincipal'><li><a href='tutorias/listado.php'>Tutorías: listado de tutorados</a></li>
<li><a href='#'>Captura de calificaciones</a></li><li><a href='#'>Horario</a></li></ul></div>""")


@router.get("/tutorias/listado.php", response_class=HTMLResponse)
def listado(request: Request):
    usuario = _usuario(request)
    if not usuario:
        return RedirectResponse("/siiaa/", status_code=303)
    filas = []
    for a in asegurar_datos()["alumnos"]:
        if a["tutor"] != usuario:
            continue
        filas.append(
            f"<tr><td class='matricula'>{a['matricula']}</td><td class='nombre'>{html.escape(a['nombre'])}</td>"
            f"<td class='programa'>{html.escape(a['programa'])}</td><td class='semestre'>{a['semestre']}</td>"
            f"<td><a class='verKardex' href='kardex.php?mat={a['matricula']}'>Ver kárdex</a></td></tr>"
        )
    return _pagina("Tutorados", f"""
<div class='caja'><h1 style='font-size:16px'>Listado de tutorados</h1>
<table id='tablaTutorados'><thead><tr><th>Matrícula</th><th>Nombre</th><th>Programa</th><th>Semestre</th><th></th></tr></thead>
<tbody>{''.join(filas)}</tbody></table></div>""")


@router.get("/tutorias/kardex.php", response_class=HTMLResponse)
def kardex(request: Request, mat: str = ""):
    usuario = _usuario(request)
    if not usuario:
        return RedirectResponse("/siiaa/", status_code=303)
    alumno = next((a for a in asegurar_datos()["alumnos"] if a["matricula"] == mat), None)
    if alumno is None or alumno["tutor"] != usuario:
        return HTMLResponse("<p>No autorizado para consultar este kárdex.</p>", status_code=403)
    filas = "".join(
        f"<tr><td>{r['clave']}</td><td>{html.escape(r['asignatura'])}</td><td>{r['creditos']}</td>"
        f"<td>{r['semestre']}</td><td>{r['periodo']}</td><td>{r['tipo']}</td>"
        f"<td>{r['calificacion']}</td><td>{r['estatus']}</td></tr>"
        for r in alumno["kardex"]
    )
    return _pagina("Kárdex", f"""
<div class='caja'><div class='datosAlumno'><b>Alumno:</b> {html.escape(alumno['nombre'])}
&nbsp; <b>Matrícula:</b> {alumno['matricula']} &nbsp; <b>Programa:</b> {html.escape(alumno['programa'])}</div>
<table class='kardex'><thead><tr><th>Clave</th><th>Asignatura</th><th>Créditos</th><th>Sem.</th><th>Periodo</th>
<th>Tipo</th><th>Calif.</th><th>Estatus</th></tr></thead><tbody>{filas}</tbody></table>
<p><a href='listado.php'>Regresar</a></p></div>""")
