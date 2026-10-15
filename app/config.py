"""Rutas, configuración y variables de entorno de SIRA."""
from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

# Carga un archivo .env sencillo si existe (sin dependencias extra).
_env = RAIZ / ".env"
if _env.exists():
    for _linea in _env.read_text(encoding="utf-8").splitlines():
        if "=" in _linea and not _linea.lstrip().startswith("#"):
            _k, _v = _linea.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())
DIR_CONFIG = RAIZ / "config"
DIR_DATOS = Path(os.environ.get("SIRA_DATA_DIR", RAIZ / "data"))
DIR_DATOS.mkdir(parents=True, exist_ok=True)

DB_PATH = DIR_DATOS / "sira.db"
MOCK_PATH = DIR_DATOS / "siiaa_mock.json"

# Portal a raspar. Por defecto, la réplica incluida en esta misma app.
SIIAA_BASE_URL = os.environ.get("SIIAA_BASE_URL", "http://127.0.0.1:8000/siiaa")
# Pausa entre páginas. En la réplica basta 0.15 s; con el portal real usar 2.5 s o más.
SCRAPER_PAUSA_S = float(os.environ.get("SCRAPER_PAUSA_S", "0.15"))
SCRAPER_UA = "SIRA-conector/0.1 (prototipo academico; contacto: ver README)"

# Sal para seudonimizar matrículas (HMAC). Cambiarla en cualquier entorno real.
SIRA_SECRET = os.environ.get("SIRA_SECRET", "cambia-esta-sal-en-produccion")

# Gemma 4 vía Gemini API (Google AI Studio)
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMMA_MODEL = os.environ.get("GEMMA_MODEL", "gemma-4-26b-a4b-it")
GEMMA_MODEL_OFICIO = os.environ.get("GEMMA_MODEL_OFICIO", "gemma-4-31b-it")


def _leer(nombre: str) -> dict:
    return json.loads((DIR_CONFIG / nombre).read_text(encoding="utf-8"))


@lru_cache
def reglas() -> dict:
    return _leer("reglas.json")


@lru_cache
def reticula() -> dict:
    return _leer("reticula.json")


@lru_cache
def selectores() -> dict:
    return _leer("siiaa_selectores.json")


def materias_por_clave() -> dict[str, dict]:
    return {m["clave"]: m for m in reticula()["materias"]}
