"""Conector de scraping ético al portal docente.

Hace lo mismo que el tutor haría a mano: inicia sesión con SU cuenta, abre SU
listado de tutorados y consulta cada kárdex. Reglas que cumple siempre:

* Usa solo las credenciales que el tutor escribe; nunca las guarda.
* Respeta robots.txt y espera entre páginas (SCRAPER_PAUSA_S).
* Se identifica con un User-Agent propio.
* Si detecta un CAPTCHA o un bloqueo, se detiene: no intenta evadirlo.
* Extrae solo lo necesario y seudonimiza la matrícula antes de guardar.
"""
from __future__ import annotations

import hashlib
import hmac
import threading
import time
import urllib.robotparser
from dataclasses import dataclass, field
from datetime import datetime
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from . import gemma
from .config import SCRAPER_PAUSA_S, SCRAPER_UA, SIIAA_BASE_URL, SIRA_SECRET, selectores
from .db import conexion


class ConectorDetenido(Exception):
    """El conector se detuvo a propósito (credenciales, CAPTCHA, bloqueo, robots.txt)."""


@dataclass
class EstadoSincronizacion:
    fase: str = "inactivo"          # inactivo | conectando | listado | kardex | listo | error
    total: int = 0
    hechos: int = 0
    mensaje: str = ""
    extraidos_con_gemma: int = 0
    inicio: float = field(default_factory=time.time)
    fin: float | None = None

    def como_dict(self) -> dict:
        return {
            "fase": self.fase, "total": self.total, "hechos": self.hechos,
            "mensaje": self.mensaje, "extraidos_con_gemma": self.extraidos_con_gemma,
            "segundos": round((self.fin or time.time()) - self.inicio, 1),
        }


ESTADOS: dict[str, EstadoSincronizacion] = {}
_candado = threading.Lock()


def seudonimo(matricula: str) -> str:
    digest = hmac.new(SIRA_SECRET.encode(), matricula.strip().encode(), hashlib.sha256).hexdigest()
    return "A-" + digest[:10]


def _texto(nodo, selector: str) -> str:
    el = nodo.select_one(selector)
    return el.get_text(strip=True) if el else ""


def _revisar_bloqueo(html: str) -> None:
    bajo = html.lower()
    if "captcha" in bajo or "recaptcha" in bajo or "acceso bloqueado" in bajo:
        raise ConectorDetenido(
            "El portal pidió un CAPTCHA o bloqueó el acceso. SIRA se detuvo y no intentará evadirlo; "
            "importa el kárdex exportado del portal o inténtalo más tarde."
        )


def parsear_listado(html: str) -> list[dict]:
    sel = selectores()["listado"]
    soup = BeautifulSoup(html, "html.parser")
    alumnos = []
    for fila in soup.select(sel["filas"]):
        enlace = fila.select_one(sel["enlace_kardex"])
        alumnos.append({
            "matricula": _texto(fila, sel["col_matricula"]),
            "nombre": _texto(fila, sel["col_nombre"]),
            "programa": _texto(fila, sel["col_programa"]),
            "semestre": int(_texto(fila, sel["col_semestre"]) or 0),
            "href": enlace["href"] if enlace else "",
        })
    return alumnos


def _a_numero(valor: str) -> float | None:
    try:
        return float(valor.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def parsear_kardex(html: str) -> list[dict]:
    """Extrae las filas del kárdex con los selectores configurados.

    Lanza ValueError si la estructura no coincide (por ejemplo, si el portal cambió su HTML).
    """
    sel = selectores()["kardex"]
    soup = BeautifulSoup(html, "html.parser")
    if soup.select_one(sel["tabla"]) is None:
        raise ValueError("No se encontró la tabla del kárdex con los selectores configurados.")
    filas = []
    for tr in soup.select(sel["filas"]):
        celdas = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(celdas) < len(sel["columnas"]):
            raise ValueError("La tabla del kárdex tiene menos columnas de las esperadas.")
        filas.append(dict(zip(sel["columnas"], celdas)))
    return filas


def quitar_identidad(html: str) -> str:
    """Devuelve solo la tabla del kárdex, sin el bloque con nombre y matrícula.

    Es lo único que se le puede pasar a Gemma cuando los selectores fallan.
    """
    sel = selectores()["kardex"]
    soup = BeautifulSoup(html, "html.parser")
    for bloque in soup.select(sel["bloque_identidad"]):
        bloque.decompose()
    tabla = soup.find("table")
    return str(tabla) if tabla else soup.get_text(" ", strip=True)[:20000]


def normalizar(filas: list[dict]) -> list[dict]:
    """Convierte las filas crudas en registros con número de oportunidad."""
    vistos: dict[str, int] = {}
    salida = []
    for f in filas:
        clave = f["clave"].strip().upper()
        vistos[clave] = vistos.get(clave, 0) + 1
        salida.append({
            "clave": clave,
            "periodo": f["periodo"].strip(),
            "tipo": f["tipo"].strip(),
            "oportunidad": vistos[clave],
            "calificacion": _a_numero(str(f.get("calificacion", ""))),
            "estatus": f["estatus"].strip(),
        })
    return salida


def _guardar(usuario: str, alumnos: list[dict]) -> None:
    with conexion() as con:
        viejos = [r["seudonimo"] for r in con.execute("SELECT seudonimo FROM alumno WHERE tutor=?", (usuario,))]
        for s in viejos:
            con.execute("DELETE FROM calificacion WHERE seudonimo=?", (s,))
        claves_validas = {r["clave"] for r in con.execute("SELECT clave FROM materia")}
        for a in alumnos:
            s = a["seudonimo"]
            con.execute(
                "INSERT INTO alumno(seudonimo, tutor, programa, semestre) VALUES (?,?,?,?) "
                "ON CONFLICT(seudonimo) DO UPDATE SET tutor=excluded.tutor, programa=excluded.programa, semestre=excluded.semestre",
                (s, usuario, a["programa"], a["semestre"]),
            )
            con.execute(
                "INSERT INTO identidad(seudonimo, nombre, matricula) VALUES (?,?,?) "
                "ON CONFLICT(seudonimo) DO UPDATE SET nombre=excluded.nombre",
                (s, a["nombre"], a["matricula"]),
            )
            for r in a["registros"]:
                if r["clave"] not in claves_validas:
                    continue
                con.execute(
                    "INSERT INTO calificacion(seudonimo, clave, periodo, oportunidad, tipo, calificacion, estatus) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (s, r["clave"], r["periodo"], r["oportunidad"], r["tipo"], r["calificacion"], r["estatus"]),
                )
        con.execute("UPDATE tutor SET ultima_sincronizacion=? WHERE usuario=?",
                    (datetime.now().isoformat(timespec="seconds"), usuario))


def sincronizar(usuario: str, contrasena: str, base_url: str = SIIAA_BASE_URL,
                cliente: httpx.Client | None = None, pausa: float = SCRAPER_PAUSA_S) -> EstadoSincronizacion:
    """Recorre el listado y los kárdex del tutor. Bloqueante: llamar desde un hilo."""
    estado = EstadoSincronizacion(fase="conectando", mensaje="Iniciando sesión en el portal")
    ESTADOS[usuario] = estado
    sel = selectores()
    base = base_url.rstrip("/") + "/"
    propio = cliente is None
    cliente = cliente or httpx.Client(follow_redirects=True, timeout=20.0)
    cliente.headers["User-Agent"] = SCRAPER_UA

    def get(ruta: str) -> str:
        url = urljoin(base, ruta.lstrip("/"))
        if robots is not None and not robots.can_fetch(SCRAPER_UA, url):
            raise ConectorDetenido(f"robots.txt del portal no permite leer {ruta}. SIRA se detuvo.")
        time.sleep(pausa)
        r = cliente.get(url, follow_redirects=True)
        if r.status_code in (403, 429):
            raise ConectorDetenido(f"El portal respondió {r.status_code} (acceso denegado o demasiadas peticiones). SIRA se detuvo.")
        r.raise_for_status()
        _revisar_bloqueo(r.text)
        return r.text

    try:
        robots = urllib.robotparser.RobotFileParser()
        r = cliente.get(urljoin(base, "robots.txt"))
        robots.parse(r.text.splitlines()) if r.status_code == 200 else None
        if r.status_code != 200:
            robots = None

        cliente.get(urljoin(base, sel["login"]["ruta_formulario"].lstrip("/")))
        resp = cliente.post(
            urljoin(base, sel["login"]["ruta_envio"].lstrip("/")),
            data={sel["login"]["campo_usuario"]: usuario, sel["login"]["campo_contrasena"]: contrasena},
            follow_redirects=True,
        )
        _revisar_bloqueo(resp.text)
        if BeautifulSoup(resp.text, "html.parser").select_one(sel["login"]["indicador_sesion_ok"]) is None:
            raise ConectorDetenido("No se pudo iniciar sesión: revisa usuario y contraseña del portal.")

        estado.fase, estado.mensaje = "listado", "Leyendo tu listado de tutorados"
        lista = parsear_listado(get(sel["listado"]["ruta"]))
        if not lista:
            raise ConectorDetenido("El listado de tutorados llegó vacío o con otra estructura. Revisa los selectores.")
        estado.total = len(lista)
        ruta_listado = sel["listado"]["ruta"]

        estado.fase = "kardex"
        alumnos = []
        for a in lista:
            estado.mensaje = f"Leyendo kárdex {estado.hechos + 1} de {estado.total}"
            html_k = get(urljoin(ruta_listado, a["href"]))
            try:
                filas = parsear_kardex(html_k)
            except ValueError:
                # Los selectores fallaron: Gemma interpreta la tabla ya sin nombre ni matrícula.
                filas = gemma.extraer_kardex(quitar_identidad(html_k))
                estado.extraidos_con_gemma += 1
            alumnos.append({
                "seudonimo": seudonimo(a["matricula"]), "matricula": a["matricula"], "nombre": a["nombre"],
                "programa": a["programa"], "semestre": a["semestre"], "registros": normalizar(filas),
            })
            estado.hechos += 1

        _guardar(usuario, alumnos)
        estado.fase = "listo"
        estado.mensaje = f"Listo: {estado.total} kárdex sincronizados"
    except ConectorDetenido as e:
        estado.fase, estado.mensaje = "error", str(e)
    except httpx.HTTPError as e:
        estado.fase, estado.mensaje = "error", f"No se pudo conectar con el portal ({e.__class__.__name__}). Intenta de nuevo."
    except gemma.GemmaNoDisponible as e:
        estado.fase, estado.mensaje = "error", f"El portal cambió su formato y Gemma no está disponible para interpretarlo: {e}"
    finally:
        estado.fin = time.time()
        if propio:
            cliente.close()
    return estado


def sincronizar_en_segundo_plano(usuario: str, contrasena: str) -> bool:
    """Lanza la sincronización en un hilo. Devuelve False si ya hay una en curso."""
    with _candado:
        actual = ESTADOS.get(usuario)
        if actual and actual.fase in ("conectando", "listado", "kardex"):
            return False
        ESTADOS[usuario] = EstadoSincronizacion(fase="conectando", mensaje="Iniciando sesión en el portal")
    threading.Thread(target=sincronizar, args=(usuario, contrasena), daemon=True).start()
    return True
