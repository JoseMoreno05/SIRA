"""Pruebas de extremo a extremo: réplica del portal → conector → semáforo → demanda → propuesta → oficio."""
import os
import tempfile

os.environ["SIRA_DATA_DIR"] = tempfile.mkdtemp(prefix="sira-test-")

import pytest
from fastapi.testclient import TestClient

from app import conector, gemma, motor, oficio, propuesta
from app.main import app


@pytest.fixture(scope="module")
def cliente():
    with TestClient(app) as c:  # dispara el arranque: datos sintéticos + esquema
        yield c


@pytest.fixture(scope="module")
def sincronizado(cliente):
    from app.datos_sinteticos import asegurar_datos
    for t in asegurar_datos()["tutores"]:
        e = conector.sincronizar(t["usuario"], t["contrasena"], base_url="http://testserver/siiaa",
                                 cliente=cliente, pausa=0)
        assert e.fase == "listo", e.mensaje
    return True


def test_login_incorrecto_se_detiene(cliente):
    e = conector.sincronizar("tutor01", "mala", base_url="http://testserver/siiaa", cliente=cliente, pausa=0)
    assert e.fase == "error" and "iniciar sesión" in e.mensaje


def test_el_tutor_solo_ve_sus_tutorados(cliente, sincronizado):
    r = cliente.get("/siiaa/tutorias/kardex.php?mat=000")
    assert r.status_code in (200, 403)  # sin sesión válida no hay kárdex
    panel = motor.panel_tutor("tutor01")
    assert 20 <= len(panel["alumnos"]) <= 40


def test_captcha_detiene_el_conector():
    with pytest.raises(conector.ConectorDetenido):
        conector._revisar_bloqueo("<div class='g-recaptcha'></div>")


def test_seudonimo_estable_y_sin_matricula():
    s = conector.seudonimo("259000123")
    assert s == conector.seudonimo("259000123") and "259000123" not in s


def test_semaforo_ultima_oportunidad():
    regs = [
        {"clave": "MAT101", "periodo": "2025-1", "oportunidad": 1, "tipo": "Ordinario", "calificacion": 4, "estatus": "Reprobada"},
        {"clave": "MAT101", "periodo": "2025-1", "oportunidad": 2, "tipo": "Extraordinario", "calificacion": 5, "estatus": "Reprobada"},
    ]
    ev = motor.evaluar_alumno(regs)
    assert ev["nivel"] == "rojo"
    assert "última oportunidad" in ev["razones"][0]


def test_semaforo_verde():
    regs = [{"clave": "MAT101", "periodo": "2025-1", "oportunidad": 1, "tipo": "Ordinario", "calificacion": 9, "estatus": "Aprobada"}]
    assert motor.evaluar_alumno(regs)["nivel"] == "verde"


def test_indice_de_impacto(sincronizado):
    r = motor.demanda()
    m = r["materias"][0]
    esperado = round(m["demanda_directa"] * (1 + 0.5 * m["bloqueo"]) * (1 + 0.5 * m["urgencia"] / m["demanda_directa"]), 2)
    assert m["indice"] == esperado
    assert [x["indice"] for x in r["materias"]] == sorted((x["indice"] for x in r["materias"]), reverse=True)


def test_entrada_de_gemma_sin_datos_personales(sincronizado):
    from app.db import conexion
    entrada = str(propuesta.entrada_para_gemma(motor.demanda()))
    with conexion() as con:
        for fila in con.execute("SELECT nombre, matricula FROM identidad"):
            assert fila["nombre"] not in entrada and fila["matricula"] not in entrada


def test_plantilla_pasa_la_validacion(sincronizado):
    ranking = motor.demanda()
    p, checks = propuesta.validar(propuesta.plantilla(ranking), ranking)
    assert p is not None, checks


def test_validacion_rechaza_cifra_inventada(sincronizado):
    ranking = motor.demanda()
    datos = propuesta.plantilla(ranking)
    datos["materias"][0]["justificacion"] += " Además, 87 alumnos ya se preinscribieron."
    p, checks = propuesta.validar(datos, ranking)
    assert p is None
    assert any(c["nombre"] == "Cifras respaldadas" and not c["ok"] for c in checks)


def test_validacion_rechaza_clave_inexistente(sincronizado):
    ranking = motor.demanda()
    datos = propuesta.plantilla(ranking)
    datos["materias"][0]["clave"] = "XYZ999"
    p, checks = propuesta.validar(datos, ranking)
    assert p is None


def test_gemma_corrige_tras_rechazo(sincronizado, monkeypatch):
    """Simula a Gemma: primero inventa una cifra, luego corrige. SIRA debe aceptar el segundo intento."""
    ranking = motor.demanda()
    buena = propuesta.plantilla(ranking)
    mala = {**buena, "materias": [dict(m) for m in buena["materias"]]}
    mala["materias"][0]["justificacion"] += " Hay 999 alumnos interesados."
    respuestas = iter([mala, buena])
    monkeypatch.setattr(gemma, "disponible", lambda: True)
    monkeypatch.setattr(gemma, "proponer_oferta", lambda entrada, correcciones=None: next(respuestas))
    p = propuesta.generar()
    assert p["origen"] == "gemma"
    assert len(p["validacion"]["intentos_gemma"]) == 2


def test_extraccion_con_gemma_si_cambian_los_selectores(monkeypatch):
    html = "<div class='datosAlumno'>Alumno: Ana Pech Matrícula: 259000123</div><table><tr><td>MAT101</td></tr></table>"
    visto = {}

    def falso(tabla):
        visto["tabla"] = tabla
        return [{"clave": "MAT101", "asignatura": "Cálculo", "creditos": "8", "semestre": "1", "periodo": "2025-1",
                 "tipo": "Ordinario", "calificacion": "8", "estatus": "Aprobada"}]

    with pytest.raises(ValueError):
        conector.parsear_kardex(html)
    monkeypatch.setattr(gemma, "extraer_kardex", falso)
    filas = gemma.extraer_kardex(conector.quitar_identidad(html))
    assert "Ana Pech" not in visto["tabla"] and "259000123" not in visto["tabla"]
    assert conector.normalizar(filas)[0]["oportunidad"] == 1


def test_oficio_pdf_y_word(sincronizado):
    p = propuesta.generar() if not propuesta.ultima() else propuesta.ultima()
    assert oficio.pdf(p)[:4] == b"%PDF"
    assert oficio.docx(p)[:2] == b"PK"


def test_paginas_responden(cliente, sincronizado):
    assert cliente.get("/").status_code == 200
    cliente.post("/entrar", data={"perfil": "tutor", "tutor": "tutor01"})
    assert "intervención" in cliente.get("/tutor").text
    cliente.post("/entrar", data={"perfil": "gestor"})
    assert cliente.get("/gestor").status_code == 200
    assert cliente.get("/gestor/propuesta?prueba=1").status_code == 200
    assert cliente.get("/privacidad").status_code == 200
    assert cliente.get("/accesibilidad").status_code == 200


# ------------------------------------------------------------------ propuesta semestral
def test_carga_respeta_seriacion_y_limites():
    from app.config import materias_por_clave
    regs = [
        {"clave": "MAT101", "periodo": "2025-1", "oportunidad": 1, "tipo": "Ordinario", "calificacion": 4, "estatus": "Reprobada"},
        {"clave": "MAT101", "periodo": "2025-1", "oportunidad": 2, "tipo": "Extraordinario", "calificacion": 5, "estatus": "Reprobada"},
        {"clave": "PRG101", "periodo": "2025-1", "oportunidad": 1, "tipo": "Ordinario", "calificacion": 9, "estatus": "Aprobada"},
        {"clave": "PRG201", "periodo": "2026-2", "oportunidad": 1, "tipo": "Ordinario", "calificacion": None, "estatus": "En curso"},
    ]
    ev = motor.evaluar_alumno(regs)
    carga = motor.carga_sugerida(regs, ev["nivel"])
    claves = [m["clave"] for m in carga["materias"]]
    assert claves[0] == "MAT101"                      # el recursamiento en última oportunidad va primero
    assert "MAT201" not in claves                     # su prerrequisito MAT101 sigue adeudado
    assert len(claves) <= 4                           # rojo: carga reducida
    cat = materias_por_clave()
    previstas = {"PRG101", "PRG201"}
    for m in carga["materias"]:
        if m["tipo"] == "nueva":
            assert all(p in previstas for p in cat[m["clave"]]["prerrequisitos"])
    prg301 = next((m for m in carga["materias"] if m["clave"] == "PRG301"), None)
    if prg301:
        assert prg301["condicionada_a"] == ["PRG201"]


def test_oferta_semestral_coherente(sincronizado):
    o = motor.oferta_semestral()
    assert o["totales"]["grupos"] == sum(m["grupos"] for m in o["materias"])
    for m in o["materias"]:
        assert m["abre"] == (m["demanda_esperada"] >= o["parametros"]["cupo_minimo"])
        assert m["demanda_esperada"] <= m["seguros"] + m["condicionados"]


def test_semestral_valida_y_rechaza(sincronizado):
    from app import semestral
    o = motor.oferta_semestral()
    ok, _ = semestral.validar(semestral.plantilla(o), o)
    assert ok is not None
    mala = semestral.plantilla(o)
    mala["resumen"] += " Se esperan 4321 alumnos."
    rechazo, checks = semestral.validar(mala, o)
    assert rechazo is None and any(not c["ok"] for c in checks)
    from app.db import conexion
    entrada = str(semestral.entrada_para_gemma(o))
    with conexion() as con:
        for fila in con.execute("SELECT nombre, matricula FROM identidad"):
            assert fila["nombre"] not in entrada and fila["matricula"] not in entrada


def test_semestral_paginas_y_oficio(cliente, sincronizado):
    cliente.post("/entrar", data={"perfil": "gestor"})
    assert cliente.get("/gestor/semestral").status_code == 200
    assert cliente.post("/gestor/semestral").status_code == 200
    assert cliente.get("/gestor/semestral/oficio.pdf").content[:4] == b"%PDF"
    assert cliente.get("/gestor/semestral/oficio.docx").content[:2] == b"PK"
