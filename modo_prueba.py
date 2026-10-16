"""
Modo copia de prueba.

Con la variable de entorno APP_COPIA_PRUEBA=1 la aplicación arranca como
copia de prueba y omite tareas automáticas que escriben en BigQuery de
producción:
  - no programa el polling de estados SMS (backend.polling_estados_sms);
  - no sincroniza los SMS de Infobip a Temporal.SmsInfobip_Valoro
    (app._sincronizar_sms_infobip) ni deja actualizar estados a mano;
  - no recarga las programaciones de email pendientes al arrancar
    (la tabla es compartida y las enviaría la app de producción);
  - revisa carpeta y puerto al arrancar (proteger_arranque_copia_prueba).

OJO: no bloquea los envíos de SMS ni de email ni las programaciones SMS del
app.db de la copia. Desarrolle con un app.db sin programaciones activas.

Sin la variable (o con cualquier otro valor) todo funciona como siempre.
"""

import os
import sys

VARIABLE_COPIA_PRUEBA = "APP_COPIA_PRUEBA"
PUERTO_APP_REAL = 5000


def es_copia_prueba(environ=None) -> bool:
    """True solo si APP_COPIA_PRUEBA vale exactamente '1'."""
    entorno = os.environ if environ is None else environ
    return str(entorno.get(VARIABLE_COPIA_PRUEBA, "")).strip() == "1"


def puerto_configurado(environ=None) -> int:
    """Mismo cálculo que app.run: PORT o 5000. Lanza ValueError si PORT no es un número."""
    entorno = os.environ if environ is None else environ
    return int(str(entorno.get("PORT", str(PUERTO_APP_REAL))).strip())


def _normalizar(ruta) -> str:
    return os.path.normcase(os.path.realpath(str(ruta)))


def revisar_arranque_copia(ruta_app, carpeta_actual, environ):
    """
    Revisa el arranque de una copia de prueba.
    Devuelve (lineas_informativas, errores). Sin errores → se puede arrancar.
    """
    ruta_app = os.path.realpath(str(ruta_app))
    carpeta_copia = os.path.dirname(ruta_app)
    carpeta_actual = os.path.realpath(str(carpeta_actual))

    errores = []
    try:
        puerto = puerto_configurado(environ)
        texto_puerto = str(puerto)
    except ValueError:
        puerto = None
        texto_puerto = f"{environ.get('PORT')!r} (inválido)"
        errores.append(f"PORT={environ.get('PORT')!r} no es un número de puerto válido.")

    info = [
        "🧪 ================= COPIA DE PRUEBA (APP_COPIA_PRUEBA=1) =================",
        f"🧪 app.py en ejecución: {ruta_app}",
        f"🧪 Carpeta actual:      {carpeta_actual}",
        f"🧪 Puerto:              {texto_puerto}",
    ]

    if _normalizar(carpeta_actual) != _normalizar(carpeta_copia):
        errores.append(
            f"La carpeta actual ({carpeta_actual}) no es la carpeta de la copia "
            f"({carpeta_copia}). Entra a la carpeta de la copia (cd) antes de arrancar: "
            f"config.json se lee de la carpeta de la copia."
        )
    if puerto == PUERTO_APP_REAL:
        errores.append(
            f"El puerto {PUERTO_APP_REAL} es el de la aplicación real. "
            f"Define PORT con otro puerto (por ejemplo, $env:PORT = \"5001\")."
        )
    return info, errores


def _escribir(linea, salida):
    try:
        print(linea, file=salida, flush=True)
    except UnicodeEncodeError:
        # Consola sin soporte de emojis: se imprime igual, sin los caracteres raros.
        print(linea.encode("ascii", "replace").decode("ascii"), file=salida, flush=True)


def proteger_arranque_copia_prueba(ruta_app, environ=None, carpeta_actual=None,
                                   salida=None, terminar=None) -> None:
    """
    Si APP_COPIA_PRUEBA=1: imprime ruta del app.py, carpeta actual y puerto
    (líneas que empiezan por 🧪) y, si la carpeta actual no es la de la copia o
    el puerto es 5000, imprime el error y termina el proceso sin arrancar.
    Sin la variable no hace nada.
    """
    entorno = os.environ if environ is None else environ
    if not es_copia_prueba(entorno):
        return
    carpeta_actual = os.getcwd() if carpeta_actual is None else carpeta_actual
    salida = sys.stdout if salida is None else salida
    terminar = sys.exit if terminar is None else terminar

    info, errores = revisar_arranque_copia(ruta_app, carpeta_actual, entorno)
    for linea in info:
        _escribir(linea, salida)
    if errores:
        _escribir("❌ ARRANQUE CANCELADO: la copia de prueba no se inició.", salida)
        for error in errores:
            _escribir(f"❌ {error}", salida)
        terminar(2)
        return
    _escribir("🧪 Revisión de arranque OK: carpeta y puerto de la copia.", salida)
