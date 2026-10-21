"""
Una sola app por carpeta.

Dos app.py corriendo desde la misma carpeta comparten app.db y cada una tiene
su scheduler: las dos ejecutan las mismas programaciones (mensajes
duplicados) y se pelean app_log.txt (en Windows no se puede rotar un archivo
que otro proceso tiene abierto: "Logging error ... WinError 32"). Pasó el
21/10/2026 con una app olvidada desde el 16/10 en otro puerto.

proteger_instancia_unica() toma un candado exclusivo sobre app.lock (en la
carpeta de app.py) y lo mantiene mientras el proceso viva; el sistema lo suelta
solo cuando el proceso termina, aunque se cierre a la fuerza. Si otra app ya lo
tiene, este arranque se cancela con un mensaje claro. Las copias de prueba
corren en otra carpeta, con su propio app.lock.
"""
import os
import sys

NOMBRE_CANDADO = "app.lock"
_archivo = None  # se mantiene abierto: cerrarlo soltaría el candado


def _tomar(archivo):
    """Candado exclusivo sin esperar sobre el primer byte. Lanza OSError si otro lo tiene."""
    archivo.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(archivo.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(archivo, fcntl.LOCK_EX | fcntl.LOCK_NB)


def proteger_instancia_unica(ruta_app):
    """Toma el candado de la carpeta de ruta_app o termina el proceso si ya hay otra app corriendo ahí."""
    global _archivo
    carpeta = os.path.dirname(os.path.realpath(str(ruta_app)))
    ruta = os.path.join(carpeta, NOMBRE_CANDADO)
    archivo = open(ruta, "a+")
    try:
        _tomar(archivo)
    except OSError:
        archivo.close()
        print("=" * 78)
        print("❌ ARRANQUE CANCELADO: ya hay una app corriendo desde esta carpeta:")
        print(f"   {carpeta}")
        print("   Dos apps a la vez duplican las programaciones (mensajes repetidos) y bloquean app_log.txt.")
        print("   Ciérrala primero (Ctrl+C en su consola, o en el Administrador de tareas: python.exe app.py)")
        print("   y vuelve a arrancar. Para probar cambios use una copia de prueba en otra carpeta.")
        print("=" * 78)
        sys.exit(1)
    _archivo = archivo
