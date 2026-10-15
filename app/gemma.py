"""Cliente de Gemma 4 (Gemini API / Google AI Studio).

Regla de privacidad: ninguna función de este módulo recibe nombres ni matrículas.
- extraer_kardex: recibe solo la tabla del kárdex, ya sin el bloque de identidad.
- sugerir_intervencion: recibe el kárdex evaluado de un seudónimo.
- proponer_oferta: recibe únicamente cifras agregadas por materia.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .config import GEMINI_API_KEY, GEMMA_MODEL, GEMMA_MODEL_OFICIO, reglas


class GemmaNoDisponible(Exception):
    pass


_cliente = None


def disponible() -> bool:
    return bool(GEMINI_API_KEY)


def _client():
    global _cliente
    if not disponible():
        raise GemmaNoDisponible("falta GEMINI_API_KEY")
    if _cliente is None:
        from google import genai
        _cliente = genai.Client(api_key=GEMINI_API_KEY)
    return _cliente


def _json_de_texto(texto: str) -> Any:
    texto = texto.strip()
    bloque = re.search(r"```(?:json)?\s*(.*?)```", texto, re.S)
    if bloque:
        texto = bloque.group(1)
    inicio = min([i for i in (texto.find("{"), texto.find("[")) if i >= 0], default=0)
    return json.loads(texto[inicio:])


def llamar(sistema: str, contenido: str, esquema: dict | None = None,
           modelo: str = GEMMA_MODEL, temperatura: float = 0.3) -> str:
    """Una llamada a Gemma. Pide JSON con esquema; si el modelo no lo acepta, reintenta sin esquema."""
    from google.genai import types

    cliente = _client()
    intentos = []
    if esquema is not None:
        intentos.append(types.GenerateContentConfig(
            system_instruction=sistema, temperature=temperatura,
            response_mime_type="application/json", response_schema=esquema))
    intentos.append(types.GenerateContentConfig(system_instruction=sistema, temperature=temperatura))
    ultimo_error: Exception | None = None
    for cfg in intentos:
        try:
            resp = cliente.models.generate_content(model=modelo, contents=contenido, config=cfg)
            if resp.text:
                return resp.text
        except Exception as e:  # el SDK lanza distintos tipos según el error
            ultimo_error = e
    raise GemmaNoDisponible(f"la llamada a {modelo} falló: {ultimo_error}")


# ---------------------------------------------------------------- extracción
ESQUEMA_KARDEX = {
    "type": "ARRAY",
    "items": {
        "type": "OBJECT",
        "properties": {k: {"type": "STRING"} for k in
                       ["clave", "asignatura", "creditos", "semestre", "periodo", "tipo", "calificacion", "estatus"]},
        "required": ["clave", "periodo", "tipo", "calificacion", "estatus"],
    },
}


def extraer_kardex(tabla_html: str) -> list[dict]:
    """Respaldo cuando los selectores ya no coinciden: Gemma lee la tabla y devuelve filas."""
    sistema = (
        "Extraes datos de tablas de kárdex universitarios. Devuelve SOLO un arreglo JSON, una fila por renglón "
        "de la tabla, con las llaves: clave, asignatura, creditos, semestre, periodo, tipo, calificacion, estatus. "
        "Copia los valores tal como aparecen; si una celda está vacía usa cadena vacía. No inventes filas."
    )
    filas = _json_de_texto(llamar(sistema, tabla_html[:60000], ESQUEMA_KARDEX))
    if not isinstance(filas, list):
        raise GemmaNoDisponible("la respuesta no fue una lista de filas")
    return [{k: str(f.get(k, "")) for k in ESQUEMA_KARDEX["items"]["properties"]} for f in filas]


# ---------------------------------------------------------------- sugerencias al tutor
def _sugerencia_plantilla(ev: dict) -> str:
    if ev["nivel"] == "verde":
        return "Sin acciones urgentes. Reconoce su avance en la próxima sesión de tutoría."
    partes = []
    criticas = [a for a in ev["adeudadas"] if a["restantes"] <= 1]
    if criticas:
        a = criticas[0]
        if a["en_curso"]:
            partes.append(f"Prioriza {a['nombre']}: la cursa en su última oportunidad. Canalízalo a asesoría "
                          "académica esta semana y acuerda una revisión de avance a mitad del periodo.")
        else:
            partes.append(f"Prioriza {a['nombre']}: le queda una sola oportunidad. Recomiéndale el intersemestral "
                          "solo si llega con asesoría previa; si no, que la curse con apoyo en el siguiente periodo.")
    bloqueantes = sorted([a for a in ev["adeudadas"] if not a["en_curso"]], key=lambda a: -a["bloqueo"])
    if bloqueantes and (not criticas or bloqueantes[0]["clave"] != criticas[0]["clave"]):
        b = bloqueantes[0]
        partes.append(f"{b['nombre']} le bloquea {b['bloqueo']} materias posteriores; es buena candidata para el intersemestral.")
    if ev.get("caida_promedio") and ev["caida_promedio"] >= 1:
        partes.append("Su promedio bajó: en la entrevista pregunta por carga de trabajo, horario y situación personal.")
    if not partes:
        partes.append("Da seguimiento a sus materias adeudadas y confirma si piensa inscribirse al intersemestral.")
    return " ".join(partes)


def sugerir_intervencion(evaluacion: dict, semestre: int) -> tuple[str, str]:
    """Devuelve (texto, origen). El origen es 'gemma' o 'plantilla'."""
    if not disponible():
        return _sugerencia_plantilla(evaluacion), "plantilla"
    datos = {
        "semestre": semestre,
        "nivel": evaluacion["nivel"],
        "motivos": evaluacion["razones"],
        "promedio": evaluacion["promedio"],
        "caida_promedio": evaluacion["caida_promedio"],
        "materias_adeudadas": [
            {k: a[k] for k in ("clave", "nombre", "usadas", "restantes", "en_curso", "bloqueo")}
            for a in evaluacion["adeudadas"]
        ],
        "oportunidades_maximas": reglas()["oportunidades_max"],
        "intersemestral_disponible": reglas()["intersemestral"],
    }
    sistema = (
        "Eres asesor de tutorías en una universidad mexicana. Con el kárdex resumido de un alumno (sin datos "
        "personales) escribe para su tutor una sugerencia breve: qué materia atender primero y por qué, si le "
        "conviene el intersemestral y una o dos preguntas para la entrevista. Máximo 90 palabras, español claro, "
        "tono respetuoso, sin diagnósticos médicos ni psicológicos. Usa solo los datos dados; no inventes cifras."
    )
    try:
        texto = llamar(sistema, json.dumps(datos, ensure_ascii=False), modelo=GEMMA_MODEL, temperatura=0.4).strip()
        return texto, "gemma"
    except GemmaNoDisponible:
        return _sugerencia_plantilla(evaluacion), "plantilla"


# ---------------------------------------------------------------- propuesta de oferta
ESQUEMA_PROPUESTA = {
    "type": "OBJECT",
    "properties": {
        "resumen": {"type": "STRING"},
        "materias": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "clave": {"type": "STRING"},
            "grupos": {"type": "INTEGER"},
            "cupo_estimado": {"type": "INTEGER"},
            "prioridad": {"type": "STRING", "enum": ["alta", "media", "baja"]},
            "justificacion": {"type": "STRING"},
        }, "required": ["clave", "grupos", "cupo_estimado", "prioridad", "justificacion"]}},
        "descartadas": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "clave": {"type": "STRING"}, "motivo": {"type": "STRING"}}, "required": ["clave", "motivo"]}},
    },
    "required": ["resumen", "materias", "descartadas"],
}

SISTEMA_PROPUESTA = (
    "Eres asesor de planeación académica. Con la tabla agregada de demanda por materia (sin datos personales) "
    "propones qué materias abrir en el intersemestral. Criterio: maximizar alumnos destrabados y priorizar a los "
    "alumnos en riesgo alto, respetando el número máximo de grupos y el cupo mínimo. Reglas estrictas: "
    "(1) usa solo claves que aparecen en la tabla; (2) cupo_estimado debe ser exactamente demanda_total de esa materia; "
    "(3) cada cifra que escribas en una justificación debe aparecer en la fila de esa materia; "
    "(4) no propongas materias con suficiente=false; (5) la suma de grupos no puede exceder grupos_max. "
    "Cada justificación: 2 o 3 frases, español formal y claro. El resumen: 3 frases para la jefatura, sin cifras "
    "que no estén en los datos. En descartadas incluye las materias de la tabla que no propones, con un motivo breve."
)


def proponer_oferta(entrada: dict, correcciones: list[str] | None = None) -> dict:
    contenido = json.dumps(entrada, ensure_ascii=False)
    if correcciones:
        contenido += ("\n\nTu respuesta anterior no pasó la validación por estos motivos; corrígelos:\n- "
                      + "\n- ".join(correcciones))
    return _json_de_texto(llamar(SISTEMA_PROPUESTA, contenido, ESQUEMA_PROPUESTA, modelo=GEMMA_MODEL_OFICIO))


# ---------------------------------------------------------------- propuesta semestral
ESQUEMA_SEMESTRAL = {
    "type": "OBJECT",
    "properties": {
        "resumen": {"type": "STRING"},
        "observaciones": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "clave": {"type": "STRING"}, "texto": {"type": "STRING"}}, "required": ["clave", "texto"]}},
    },
    "required": ["resumen", "observaciones"],
}

SISTEMA_SEMESTRAL = (
    "Eres asesor de planeación académica. Recibes la oferta calculada para el próximo semestre (sin datos "
    "personales): por materia, alumnos que la necesitan con seguridad, alumnos condicionados a acreditar un "
    "prerrequisito que cursan hoy, demanda esperada, recursamientos, alumnos en riesgo alto y grupos sugeridos. "
    "Escribe: (1) un resumen de 3 o 4 frases para la jefatura; (2) entre 3 y 6 observaciones sobre las materias "
    "que requieren atención especial (muchos recursamientos, muchos alumnos en riesgo, demanda muy condicionada o "
    "materias sin grupo). Reglas estrictas: usa solo claves de la tabla; cada cifra que escribas debe aparecer "
    "en la fila de esa materia o en los totales; no cambies el número de grupos; español formal y claro."
)


def redactar_semestral(entrada: dict, correcciones: list[str] | None = None) -> dict:
    contenido = json.dumps(entrada, ensure_ascii=False)
    if correcciones:
        contenido += ("\n\nTu respuesta anterior no pasó la validación por estos motivos; corrígelos:\n- "
                      + "\n- ".join(correcciones))
    return _json_de_texto(llamar(SISTEMA_SEMESTRAL, contenido, ESQUEMA_SEMESTRAL, modelo=GEMMA_MODEL_OFICIO))
