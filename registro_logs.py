"""
registro_logs.py — Acciones de cada persona en .txt (carpeta logs/, como en Omnic).

  logs/
    acciones/acciones_AAAA-MM-DD.txt  cada acción de una persona (todo lo que no es GET,
                                      descargas e ingresos): lo mismo que RegistroAccion
    usuarios/<usuario>/AAAA-MM.txt    las acciones de cada persona

Lo usa usuarios.py (after_request). Nunca se guardan teléfonos, correos de
clientes, claves ni cuerpos de peticiones. La carpeta logs/ está en .gitignore
y estos archivos se guardan siempre (no se rotan).

Sin Flask ni base de datos al importar: se puede usar desde cualquier script.
"""

import re
import threading
from datetime import datetime
from pathlib import Path

CARPETA = Path(__file__).resolve().parent / "logs"
FORMATO_FECHA = "%Y-%m-%d %H:%M:%S"

_candado = threading.RLock()


def _ahora():
    try:
        import pytz
        return datetime.now(pytz.timezone("America/Bogota")).replace(tzinfo=None)
    except Exception:
        return datetime.now()


def _ruta(subcarpeta, nombre):
    carpeta = CARPETA / subcarpeta
    carpeta.mkdir(parents=True, exist_ok=True)
    return carpeta / nombre


def _escribir(ruta, texto):
    """Agrega texto al archivo. Nunca rompe a quien llama."""
    try:
        with _candado, open(ruta, "a", encoding="utf-8") as f:
            f.write(texto)
    except Exception:
        pass


def _nombre_usuario(usuario):
    """Nombre de carpeta seguro en Windows para un usuario (su correo)."""
    limpio = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(usuario or "").strip().lower())
    return limpio.strip(". ") or "sin_usuario"


def _miles(n):
    try:
        return f"{int(n):,}".replace(",", ".")
    except (TypeError, ValueError):
        return str(n)


def usuario_sesion():
    """Usuario con sesión iniciada en la petición actual, o None (scheduler, scripts)."""
    try:
        from flask import g, has_request_context
        if has_request_context():
            u = g.get("usuario")
            return u.usuario if u is not None else None
    except Exception:
        pass
    return None


def registrar_accion(*, usuario, perfil="", metodo="", ruta="", canal="", accion="", campana="",
                     registros=None, estado_http=None, resultado=""):
    """Una acción de una persona (lo mismo que RegistroAccion) → logs/acciones y su carpeta."""
    try:
        ahora = _ahora()
        partes = [ahora.strftime(FORMATO_FECHA), usuario or "sin_usuario"]
        if perfil:
            partes.append(perfil)
        if canal:
            partes.append(canal)
        partes.append(accion or f"{metodo} {ruta}")
        if campana:
            partes.append(f"campaña: {campana}")
        if registros not in (None, ""):
            partes.append(f"registros: {_miles(registros)}")
        if estado_http is not None:
            partes.append(f"HTTP {estado_http}")
        if resultado:
            partes.append(str(resultado))
        linea = " | ".join(str(p) for p in partes) + "\n"
        _escribir(_ruta("acciones", f"acciones_{ahora:%Y-%m-%d}.txt"), linea)
        _escribir(_ruta(Path("usuarios") / _nombre_usuario(usuario), f"{ahora:%Y-%m}.txt"), "ACCIÓN  " + linea)
    except Exception:
        pass
