"""
Exclusiones de la base SMS (issue #38), acordadas con el equipo de SMS.

Cada regla aplica solo si la plantilla usa esa variable:
  1.1 Nombre Banco: se excluyen las entidades marcadas NO. Una entidad que
      no está en la lista sí se envía.
  1.2 Valor Oferta Esp / Valor Oferta Esp 2: sin valor → se excluye.
  1.3 Fecha de Pago / Fecha de Pago 2: sin valor → se excluye.
  1.4 Porcentaje / Porcentaje 2: sin valor → se excluye.
  Link: sin valor → se excluye (p. ej. WS sin el link de WhatsApp del día).

Sin Flask ni base de datos: funciones puras sobre las filas.
"""

import re

# Entidad_principal marcadas NO por el equipo de SMS (07/10/2026).
ENTIDADES_EXCLUIDAS = (
    "COOPCENTRAL", "GIROS Y FINANZAS", "GIROS Y FINANZAS2", "GRUPO SOL",
    "TUYA", "TUYA2", "FGA2", "BANCO UNION", "BANCO UNION2",
)

# Regla → variables de la plantilla que la activan (sin mayúsculas).
VARIABLES_OBLIGATORIAS = {
    "Sin valor oferta": ("valor oferta esp", "valor oferta esp 2"),
    "Sin fecha de pago": ("fecha de pago", "fecha de pago 2"),
    "Sin porcentaje": ("porcentaje", "porcentaje 2"),
    "Sin link": ("link",),
}
REGLA_BANCO = "Banco excluido"


def _sin_marcas(entidad) -> str:
    """'TUYA2' → 'TUYA'; 'BANCOLOMBIA3P1' → 'BANCOLOMBIA' (como nombre_banco de sms_excel)."""
    return re.split(r"\d", str(entidad or "").strip().upper(), maxsplit=1)[0].strip()


# Se compara sin marcas: así sirve con la Entidad_principal original o con
# el Nombre Banco ya limpio (ninguna entidad NO comparte nombre con una SÍ).
_BANCOS_EXCLUIDOS = {_sin_marcas(e) for e in ENTIDADES_EXCLUIDAS}


def banco_excluido(entidad) -> bool:
    """True si la entidad está marcada NO."""
    nombre = _sin_marcas(entidad)
    return bool(nombre) and nombre in _BANCOS_EXCLUIDOS


def _vacio(valor) -> bool:
    return valor is None or str(valor).strip().lower() in ("", "nan", "none", "null", "nat")


def motivo_exclusion(fila: dict, variables_usadas) -> str:
    """Primera regla que excluye la fila ('' si se envía)."""
    usadas = {str(v).strip().lower() for v in variables_usadas}
    por_nombre = {str(k).strip().lower(): v for k, v in fila.items()}

    if "nombre banco" in usadas:
        entidad = por_nombre.get("entidad_principal")
        if _vacio(entidad):
            entidad = por_nombre.get("nombre banco")
        if banco_excluido(entidad):
            return REGLA_BANCO

    for regla, variables in VARIABLES_OBLIGATORIAS.items():
        if any(v in usadas and _vacio(por_nombre.get(v)) for v in variables):
            return regla
    return ""


def aplicar_exclusiones(filas, variables_usadas):
    """Devuelve (filas que se envían, {regla: cantidad excluida})."""
    quedan, excluidos = [], {}
    for fila in filas:
        motivo = motivo_exclusion(fila, variables_usadas)
        if motivo:
            excluidos[motivo] = excluidos.get(motivo, 0) + 1
        else:
            quedan.append(fila)
    return quedan, excluidos
