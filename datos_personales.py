"""
Enmascarado de datos personales antes de escribirlos en los registros (logs).

Los logs (execution_log.txt y demás) quedan en texto plano en disco; no deben
llevar nombres, teléfonos, identificaciones, correos ni montos de clientes.
Sin dependencias de Flask/DB/red.
"""

from __future__ import annotations

import re

def enmascarar_telefono(valor) -> str:
    """'3001234567' → '300****567'. Deja visibles 3 dígitos al inicio y 3 al final."""
    digitos = re.sub(r"\D", "", str(valor or ""))
    if not digitos:
        return ""
    if len(digitos) <= 6:
        return "*" * len(digitos)
    return f"{digitos[:3]}{'*' * (len(digitos) - 6)}{digitos[-3:]}"


def enmascarar_nombre(valor) -> str:
    """'Juan Pérez' → 'J. P.'"""
    partes = str(valor or "").split()
    return " ".join(f"{p[0]}." for p in partes)


def enmascarar_email(valor) -> str:
    """'juan.perez@dominio.com' → 'j***@dominio.com'"""
    texto = str(valor or "").strip()
    if not texto:
        return ""
    usuario, arroba, dominio = texto.partition("@")
    return f"{usuario[:1]}***{arroba}{dominio}"
