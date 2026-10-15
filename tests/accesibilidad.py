"""Auditoría automática de accesibilidad de SIRA.

Requiere el servidor corriendo (uvicorn app.main:app) y Playwright con Chromium:
    pip install playwright && python -m playwright install chromium
    python tests/accesibilidad.py [http://127.0.0.1:8000]

Para cada página: corre axe-core (WCAG 2.0/2.1/2.2, A y AA), revisa que no haya
desplazamiento horizontal a 320 px de ancho y que el primer Tab muestre un foco visible.
Escribe los resultados en docs/accesibilidad-resultados.json (la página /accesibilidad los muestra).
"""
import json
import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
RAIZ = Path(__file__).resolve().parent.parent
AXE = (Path(__file__).parent / "vendor" / "axe.min.js").read_text(encoding="utf-8")
ETIQUETAS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]

PAGINAS = [
    ("Inicio", None, "/"),
    ("Panel del tutor", "tutor", "/tutor"),
    ("Detalle de alumno", "tutor", "ALUMNO"),
    ("Demanda por materia", "gestor", "/gestor"),
    ("Propuesta intersemestral", "gestor", "/gestor/propuesta?prueba=1"),
    ("Propuesta semestral", "gestor", "/gestor/semestral"),
    ("Privacidad", None, "/privacidad"),
    ("Accesibilidad", None, "/accesibilidad"),
]


def entrar(page, perfil):
    page.goto(BASE + "/salir")
    if perfil:
        page.check(f"input[value={perfil}]")
        page.click("button:has-text('Entrar')")
        page.wait_for_load_state()


def preparar(page):
    """Garantiza datos: sincroniza a todos los tutores en la réplica y genera una propuesta."""
    entrar(page, "gestor")
    page.request.post(BASE + "/gestor/sincronizar-demo")
    for _ in range(120):
        e = page.request.get(BASE + "/api/sincronizacion-demo").json()
        if not e["activo"] and e["sincronizados"] == e["total"]:
            break
        time.sleep(0.5)
    page.request.post(BASE + "/gestor/propuesta")
    page.request.post(BASE + "/gestor/semestral")


def auditar(page):
    page.add_script_tag(content=AXE)
    r = page.evaluate("""async (tags) => {
        const res = await axe.run(document, { runOnly: { type: 'tag', values: tags } });
        return { version: axe.version, violaciones: res.violations.map(v => ({ id: v.id, impacto: v.impact, nodos: v.nodes.length, ayuda: v.help })) };
    }""", ETIQUETAS)
    return r


def main():
    salida = {"fecha": date.today().isoformat(), "paginas": []}
    with sync_playwright() as p:
        nav = p.chromium.launch()
        page = nav.new_page(viewport={"width": 1280, "height": 900})
        estrecha = nav.new_page(viewport={"width": 320, "height": 800})
        preparar(page)
        for nombre, perfil, ruta in PAGINAS:
            for pg in (page, estrecha):
                entrar(pg, perfil)
            if ruta == "ALUMNO":
                page.goto(BASE + "/tutor")
                ruta = page.locator("table tbody tr th a").first.get_attribute("href")
            page.goto(BASE + ruta)
            res = auditar(page)
            salida["axe_version"] = res["version"]
            page.keyboard.press("Tab")
            foco = page.evaluate("""() => { const e = document.activeElement; const s = getComputedStyle(e);
                return e !== document.body && (s.outlineStyle !== 'none' && parseFloat(s.outlineWidth) > 0 || e.classList.contains('saltar')); }""")
            estrecha.goto(BASE + ruta)
            desborde = estrecha.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")
            salida["paginas"].append({"nombre": nombre, "ruta": ruta.split("?")[0], "violaciones": len(res["violaciones"]),
                                      "detalle": res["violaciones"], "desborde_320": desborde, "foco_visible": foco})
            print(f"{nombre:28} axe: {len(res['violaciones'])} fallas  320px desborde: {desborde}  foco: {foco}")
            for v in res["violaciones"]:
                print("   -", v["id"], v["impacto"], v["nodos"], v["ayuda"])
        nav.close()
    destino = RAIZ / "docs" / "accesibilidad-resultados.json"
    destino.parent.mkdir(exist_ok=True)
    destino.write_text(json.dumps(salida, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Resultados en", destino)
    return 0 if all(pg["violaciones"] == 0 and not pg["desborde_320"] for pg in salida["paginas"]) else 1


if __name__ == "__main__":
    sys.exit(main())
