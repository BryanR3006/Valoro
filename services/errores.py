"""
Mensajes de error claros para la pantalla (SMS, email, BigQuery).

explicar_error(exc) devuelve un texto corto que dice la causa: sin conexión,
límite de la API, sin saldo, clave rechazada, error de la consulta… y en qué
servicio (Infobip, la plataforma de email, BigQuery). El detalle técnico
(traceback, URL, respuesta completa de la API) va solo al log: quien llama
sigue registrando la excepción con logger.exception.

tipo_error(exc) devuelve la causa como palabra (conexion, limite, saldo,
credenciales, consulta, datos, otro) para quien necesite decidir por ella.

Sin Flask, base de datos ni red: se prueba con excepciones de mentira.
"""
import re

# Servicio según lo que diga el error (URL o nombre), con su nombre para la pantalla.
SERVICIOS = (
    ("esmsv", "la plataforma de email"),
    ("infobip", "Infobip (SMS)"),
    ("googleapis", "BigQuery"),
    ("bigquery", "BigQuery"),
)

# Causa → patrones del texto del error (minúsculas). El orden importa: "too many 429
# error responses" es límite, no conexión; "403 quota exceeded" es límite, no clave.
PATRONES = (
    ("saldo", r"not_enough_credits|not enough credits|insufficient (?:balance|credit|funds)|sin saldo|"
              r"saldo insuficiente|no credit|credits? (?:exhausted|agotad)|errormsg_\w*credit"),
    ("limite", r"\b429\b|too many requests|too many 429|rate.?limit|throttl|too_many|limit exceeded|"
               r"quota exceeded|exceeded rate|errormsg_\w*limit|max(?:imum)? (?:sends|requests)|límite de envío"),
    ("credenciales", r"\b401\b|unauthori[sz]ed|invalid (?:api.?key|token|credentials)|authentication failed|"
                     r"errormsg_unauthorized|errormsg_\w*apikey|\b403\b.*(?:access denied|forbidden)|forbidden"),
    ("conexion", r"max retries exceeded|read timed out|timed out|timeout|newconnectionerror|failed to establish|"
                 r"getaddrinfo|name resolution|nameresolutionerror|connection (?:aborted|refused|reset|error)|"
                 r"remotedisconnected|winerror 100(?:60|61|54|65)|error de red|"
                 r"ssl: |sslerror|eof occurred"),
)

MENSAJES = {
    "conexion": "Sin conexión con {servicio}: no respondió a tiempo. Revisa el internet del equipo o intenta en unos minutos.",
    "limite": "{Servicio} no deja hacer tantas peticiones seguidas (límite de la API). Espera unos minutos e intenta de nuevo.",
    "saldo": "Sin saldo en {servicio}: recarga la cuenta para poder enviar.",
    "credenciales": "{Servicio} rechazó la clave de acceso (api_key en config.json): revísala.",
}

# Excepciones propias de la app cuyo texto ya es claro: se muestran tal cual
# (salvo que su texto sea de conexión, límite, saldo o clave).
AMIGABLES = {"EmailServiceError", "SmsServiceError", "SinDestinatariosEmail", "ErrorListaNegraEmail",
             "EmailClientError", "EmailTimeoutError", "ConsultaNoPermitida", "ValueError"}
# Errores de BigQuery (google.api_core): el mensaje de BigQuery dice qué falló en la consulta.
ERRORES_BIGQUERY = {"GoogleAPIError", "GoogleAPICallError"}

LARGO_MAXIMO = 400


def marcar_claro(exc):
    """
    Marca un error cuyo texto ya es claro y final (p. ej. "Infobip no respondió a
    tiempo: el lote pudo haber salido…"): explicar_error lo deja tal cual en vez
    de cambiarlo por el mensaje general de conexión. Devuelve la misma excepción.
    """
    exc.claro = True
    return exc


def _nombres_clase(exc):
    return {c.__name__ for c in type(exc).__mro__}


def _servicio(texto, servicio=None):
    for clave, nombre in SERVICIOS:
        if clave in texto:
            return nombre
    return servicio or "el servicio externo"


def tipo_error(exc) -> str:
    """conexion, limite, saldo, credenciales, consulta (BigQuery), datos (mensaje propio claro) u otro."""
    texto = str(exc).lower()
    nombres = _nombres_clase(exc)
    for tipo, patron in PATRONES:
        if re.search(patron, texto) or (tipo == "conexion" and nombres & {"Timeout", "ConnectionError",
                                                                          "ConnectTimeout", "ReadTimeout"}):
            return tipo
    if nombres & ERRORES_BIGQUERY:
        return "consulta"
    if nombres & AMIGABLES:
        return "datos"
    return "otro"


def _corto(texto, largo=LARGO_MAXIMO):
    texto = " ".join(str(texto).split())
    return texto if len(texto) <= largo else texto[:largo - 1] + "…"


def explicar_error(exc, servicio=None, bigquery=None) -> str:
    """
    Mensaje claro y corto para la pantalla. servicio: nombre por defecto si el
    error no lo dice ("Infobip (SMS)", "la plataforma de email"…). bigquery:
    función que da el mensaje de un error de BigQuery (bigquery.mensaje_error_bigquery).
    """
    texto = str(exc)
    if getattr(exc, "claro", False):
        return _corto(texto)
    tipo = tipo_error(exc)
    if tipo in MENSAJES:
        nombre = _servicio(texto.lower(), servicio)
        return MENSAJES[tipo].format(servicio=nombre, Servicio=nombre[:1].upper() + nombre[1:])
    if tipo == "consulta":
        return _corto(bigquery(exc) if bigquery else f"Error de BigQuery: {getattr(exc, 'message', texto)}")
    if tipo == "datos":
        return _corto(texto)
    return _corto(f"Error inesperado ({type(exc).__name__}): {texto}. El detalle quedó en el log.", 300)
