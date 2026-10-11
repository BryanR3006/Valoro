"""
Carga de la base SMS desde Excel (issue #39).

Sin Flask ni base de datos: el cliente de BigQuery llega como parámetro,
para poder probarlo con objetos falsos.

Pasos:
  1. Lee el Excel. Las columnas se reconocen por el nombre de la variable
     de carga (mapa del issue #37) sin importar mayúsculas, tildes,
     espacios o guiones bajos. Las columnas que no son del mapa se
     conservan tal cual (se pueden usar en la plantilla).
  2. Si la fila trae Id Cliente, lo que falte sale de Campanas_intradia
     (lo que trae el Excel tiene prioridad).
  3. Calcula las columnas derivadas: Valor a pagar 2, Valor Oferta Esp 2,
     Porcentaje 2, Link (según Id Mensaje) y Nombre Banco sin las marcas
     internas de Entidad_principal.
  4. Devuelve las filas como texto, listas para preparar_sms.

Las validaciones (lista negra, duplicados) las hace el flujo normal de SMS.
"""

import logging
import math
import re
import unicodedata
from datetime import date, datetime
from numbers import Number

import pandas as pd

logger = logging.getLogger(__name__)

TABLA_INTRADIA = "capable-arbor-209819.Campanas.Campanas_intradia"

# Variables de carga (issue #37), en el orden de la plataforma de SMS.
VARIABLES_CARGA = (
    "Celular", "Nombre Cliente", "Id Cliente", "Fecha de Pago", "Fecha de Pago 2",
    "Valor a pagar", "Valor a pagar 2", "Valor Oferta Esp", "Valor Oferta Esp 2",
    "Cuotas", "Porcentaje", "Porcentaje 2", "Segmento", "Link", "Link WhatsApp", "Id Campaña",
    "Id Mensaje", "Descripcion Campaña", "Objetivo", "Nombre Banco",
    "Nombre Campaña", "Canal Ley", "Ley Contacto",
)

# Otros nombres aceptados en el Excel para cada variable.
ALIAS_EXCEL = {
    "Celular": ("telefono", "telefono_1", "tel1", "movil", "phone"),
    "Nombre Cliente": ("name", "nombre"),
    "Id Cliente": ("contacto__c", "contaco__c", "customer_id"),
    "Valor a pagar": ("saldo_total_cliente",),
    "Segmento": ("operado_por__c",),
    "Nombre Banco": ("entidad_principal",),
    "Canal Ley": ("canal_ley",),
    "Ley Contacto": ("ley_contacto",),
    "Link WhatsApp": ("link_ws", "whatsapp", "link_de_whatsapp"),
}

# Variable → columnas de Campanas_intradia que la completan (la primera con valor).
COMPLETAR_DESDE_INTRADIA = {
    "Celular": ("Telefono_1", "Telefono_2", "Telefono_3"),
    "Nombre Cliente": ("Name",),
    "Valor a pagar": ("Saldo_Total_cliente",),
    "Segmento": ("Operado_Por__c",),
    "Nombre Banco": ("Entidad_principal",),
    "Canal Ley": ("CANAL_LEY",),
    "Ley Contacto": ("LEY_CONTACTO",),
}

# Link según lo que contenga el Id Mensaje. WS (WhatsApp) cambia cada día:
# va en la columna Link WhatsApp del Excel; basta escribirlo en una fila y
# se usa para todos los WS de la base.
LINK_POR_ID_MENSAJE = (
    ("PBX", "https://qnt.com.co/wp-content/llamar.html"),
    ("PAG", "https://qnt.com.co/pagos-qnt/"),
    ("LAND", "https://portalclientes.qnt.com.co/inicio"),
)

_VACIOS = ("", "nan", "none", "null", "nat", "<na>")


# ══════════════════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════════════════

def _clave(nombre) -> str:
    """Nombre de columna comparable: sin tildes, minúsculas, '_' por espacios."""
    texto = unicodedata.normalize("NFKD", str(nombre))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[\s_]+", "_", texto.strip().lower())


def _vacio(valor) -> bool:
    if valor is None:
        return True
    if isinstance(valor, float) and math.isnan(valor):
        return True
    try:
        if pd.isna(valor):
            return True
    except (TypeError, ValueError):
        pass
    return str(valor).strip().lower() in _VACIOS


def _numero(valor):
    """Número de una celda: 1250000, '1.250.000', '$ 1,250,000.50', '0,4'. None si no es número."""
    if _vacio(valor):
        return None
    if isinstance(valor, Number) and not isinstance(valor, bool):
        return float(valor)
    texto = re.sub(r"[^\d,.\-]", "", str(valor))
    if not re.search(r"\d", texto):
        return None
    if "," in texto and "." in texto:
        # El último separador es el decimal.
        if texto.rfind(",") > texto.rfind("."):
            texto = texto.replace(".", "").replace(",", ".")
        else:
            texto = texto.replace(",", "")
    elif texto.count(".") > 1 or texto.count(",") > 1:
        texto = texto.replace(".", "").replace(",", "")
    elif "," in texto or "." in texto:
        sep = "," if "," in texto else "."
        decimales = texto.split(sep)[1]
        # 1.250 / 1,250 → miles; 0,4 / 12.5 → decimal.
        texto = texto.replace(sep, "") if len(decimales) == 3 else texto.replace(sep, ".")
    try:
        return float(texto)
    except ValueError:
        return None


def formato_miles(valor) -> str:
    """1250000 → '1.250.000' (pesos, sin decimales). Vacío si no es número."""
    numero = _numero(valor)
    if numero is None:
        return ""
    return f"{round(numero):,}".replace(",", ".")


def formato_porcentaje(valor) -> str:
    """0,4 → '40%'; 40 → '40%'; 0,125 → '12,5%'. Vacío si no es número."""
    numero = _numero(valor)
    if numero is None:
        return ""
    if abs(numero) <= 1:
        numero *= 100
    numero = round(numero, 1)
    texto = str(int(numero)) if numero == int(numero) else str(numero).replace(".", ",")
    return f"{texto}%"


def nombre_banco(entidad) -> str:
    """Entidad_principal sin las marcas internas: 'BANCOLOMBIA3P1' → 'BANCOLOMBIA'."""
    if _vacio(entidad):
        return ""
    return re.split(r"\d", str(entidad).strip(), maxsplit=1)[0].strip()


def link_por_id_mensaje(id_mensaje) -> str:
    """Link fijo según el Id Mensaje (PBX, PAG, LAND). Vacío si no aplica."""
    texto = "" if _vacio(id_mensaje) else str(id_mensaje).upper()
    for marca, link in LINK_POR_ID_MENSAJE:
        if marca in texto:
            return link
    return ""


def aplicar_link_whatsapp(filas, link, id_mensaje_pantalla="") -> int:
    """
    Link de WhatsApp (cambia cada día, se escribe con la campaña en pantalla)
    en las filas cuyo Id Mensaje contiene WS y no traen Link. Si la fila no
    trae Id Mensaje se usa el del mensaje elegido en pantalla. Modifica las
    filas y devuelve cuántas completó.
    """
    link = (link or "").strip()
    if not link:
        return 0
    completadas = 0
    for fila in filas:
        # Las consultas pueden traer 'link' o 'id_mensaje' con otro formato.
        columnas = {_clave(k): k for k in fila}
        col_link = columnas.get("link", "Link")
        id_mensaje = fila.get(columnas.get("id_mensaje", "Id Mensaje"))
        if _vacio(id_mensaje):
            id_mensaje = id_mensaje_pantalla
        if "WS" in str(id_mensaje or "").upper() and _vacio(fila.get(col_link)):
            fila[col_link] = link
            completadas += 1
    return completadas


def _texto(valor) -> str:
    """Valor de celda como texto para el SMS."""
    if _vacio(valor):
        return ""
    if isinstance(valor, (datetime, pd.Timestamp)):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, date):
        return valor.strftime("%d/%m/%Y")
    if isinstance(valor, Number) and not isinstance(valor, bool) and float(valor) == int(valor):
        return str(int(valor))
    return str(valor).strip()


# ══════════════════════════════════════════════════════════════════════
# LECTURA DEL EXCEL
# ══════════════════════════════════════════════════════════════════════

def leer_excel_sms(file) -> tuple[list[dict], dict]:
    """
    Lee el Excel y renombra las columnas reconocidas al nombre de la
    variable de carga. Devuelve (filas, {variable: columna_del_excel}).
    """
    try:
        df = pd.read_excel(file, dtype=object)
    except Exception as exc:
        raise ValueError(f"No se pudo leer el Excel: {exc}")

    df = df.dropna(how="all")
    if df.empty:
        raise ValueError("El Excel no tiene filas.")

    aceptados = {}
    for variable in VARIABLES_CARGA:
        for nombre in (variable,) + ALIAS_EXCEL.get(variable, ()):
            aceptados.setdefault(_clave(nombre), variable)

    # Varias columnas de la misma variable (p. ej. 'Celular' y 'Telefono'):
    # se unen en orden; la primera con valor gana.
    por_variable, otras = {}, []
    for columna in df.columns:
        variable = aceptados.get(_clave(columna))
        if variable:
            por_variable.setdefault(variable, []).append(columna)
        else:
            otras.append(columna)

    datos = {str(c): df[c] for c in otras if str(c) not in por_variable}
    reconocidas = {}
    for variable, columnas in por_variable.items():
        serie = df[columnas[0]]
        for columna in columnas[1:]:
            serie = serie.where(~serie.map(_vacio), df[columna])
        datos[variable] = serie
        reconocidas[variable] = ", ".join(str(c) for c in columnas)
    df = pd.DataFrame(datos)

    if "Celular" not in reconocidas and "Id Cliente" not in reconocidas:
        raise ValueError(
            "El Excel debe tener la columna 'Celular' o 'Id Cliente' (Contacto__c). "
            f"Columnas encontradas: {[str(c) for c in df.columns]}"
        )

    filas = [{str(k): v for k, v in fila.items()} for fila in df.to_dict("records")]
    logger.info(f"[SMS-EXCEL] Leídas {len(filas)} filas | reconocidas: {reconocidas}")
    return filas, reconocidas


# ══════════════════════════════════════════════════════════════════════
# INTRADÍA
# ══════════════════════════════════════════════════════════════════════

def consultar_intradia_sms(client, contactos) -> dict:
    """{Contacto__c: fila} de Campanas_intradia con las columnas del mapa."""
    from google.cloud import bigquery

    columnas = sorted({c for cols in COMPLETAR_DESDE_INTRADIA.values() for c in cols})
    query = f"""
        SELECT Contacto__c, {", ".join(columnas)}
        FROM `{TABLA_INTRADIA}`
        WHERE Contacto__c IN UNNEST(@contactos)
    """
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("contactos", "STRING", sorted(contactos)),
    ])
    df = client.query(query, job_config=config).to_dataframe()
    return {str(f["Contacto__c"]).strip(): f for f in df.to_dict("records")}


# ══════════════════════════════════════════════════════════════════════
# ORQUESTADOR
# ══════════════════════════════════════════════════════════════════════

def completar_filas_sms(filas, client) -> tuple[list[dict], dict]:
    """
    Completa desde el intradía, calcula las columnas derivadas y deja todo
    como texto. Devuelve (filas, resumen).
    """
    contactos = set()
    for fila in filas:
        contacto = _texto(fila.get("Id Cliente"))
        if contacto and any(_vacio(fila.get(v)) for v in COMPLETAR_DESDE_INTRADIA):
            contactos.add(contacto)

    intradia = {}
    if contactos:
        if client is None:
            raise ValueError("No hay conexión a BigQuery para completar desde el intradía.")
        try:
            intradia = consultar_intradia_sms(client, contactos)
        except Exception as exc:
            raise ValueError(f"Error consultando Campanas_intradia: {exc}")

    # Link WhatsApp: el primero que aparezca sirve para toda la base.
    link_ws_base = next((fila.get("Link WhatsApp") for fila in filas
                         if not _vacio(fila.get("Link WhatsApp"))), "")
    con_link_ws = 0

    completados = {v: 0 for v in COMPLETAR_DESDE_INTRADIA}
    salida = []
    for fila in filas:
        nueva = dict(fila)
        contacto = _texto(fila.get("Id Cliente"))
        nueva["Id Cliente"] = contacto
        fila_intradia = intradia.get(contacto, {})

        for variable, columnas in COMPLETAR_DESDE_INTRADIA.items():
            if not _vacio(nueva.get(variable)):
                continue
            valor = next((fila_intradia.get(c) for c in columnas
                          if not _vacio(fila_intradia.get(c))), None)
            if not _vacio(valor):
                nueva[variable] = valor
                completados[variable] += 1

        # Entidad_principal se conserva para las exclusiones de bancos (#38).
        if "Entidad_principal" not in nueva:
            nueva["Entidad_principal"] = nueva.get("Nombre Banco")
        nueva["Nombre Banco"] = nombre_banco(nueva.get("Nombre Banco"))

        calculados = {
            "Valor a pagar 2": lambda: formato_miles(nueva.get("Valor a pagar")),
            "Valor Oferta Esp 2": lambda: formato_miles(nueva.get("Valor Oferta Esp")),
            "Porcentaje 2": lambda: formato_porcentaje(nueva.get("Porcentaje")),
            "Link": lambda: link_por_id_mensaje(nueva.get("Id Mensaje")),
        }
        for variable, calcular in calculados.items():
            if _vacio(nueva.get(variable)):
                nueva[variable] = calcular()

        if _vacio(nueva.get("Link")) and "WS" in _texto(nueva.get("Id Mensaje")).upper():
            link_ws = nueva.get("Link WhatsApp")
            nueva["Link"] = link_ws_base if _vacio(link_ws) else link_ws
            con_link_ws += 0 if _vacio(nueva["Link"]) else 1

        for variable in VARIABLES_CARGA:
            nueva.setdefault(variable, "")
        salida.append({k: _texto(v) for k, v in nueva.items()})

    resumen = {
        "total_filas": len(salida),
        "contactos_buscados": len(contactos),
        "no_encontrados_intradia": len(contactos) - len(intradia),
        "completados": {v: n for v, n in completados.items() if n},
        "sin_celular": sum(1 for f in salida if not f["Celular"]),
        "con_link_whatsapp": con_link_ws,
        "ws_sin_link": sum(1 for f in salida if "WS" in f["Id Mensaje"].upper() and not f["Link"]),
    }
    logger.info(f"[SMS-EXCEL] {resumen}")
    return salida, resumen


def excel_a_filas_sms(file, client) -> tuple[list[dict], dict]:
    """Lee el Excel y lo deja listo para preparar_sms. Devuelve (filas, resumen)."""
    filas, reconocidas = leer_excel_sms(file)
    filas, resumen = completar_filas_sms(filas, client)
    resumen["columnas_reconocidas"] = reconocidas
    if not any(f["Celular"] for f in filas):
        raise ValueError(
            f"Ninguna de las {len(filas)} filas tiene celular "
            "(ni en el Excel ni en Campanas_intradia)."
        )
    return filas, resumen


def plantilla_excel_sms() -> bytes:
    """Excel vacío con las columnas que se llenan a mano o desde el intradía."""
    import io

    columnas = [v for v in VARIABLES_CARGA if not v.endswith(" 2") or v == "Fecha de Pago 2"]
    salida = io.BytesIO()
    pd.DataFrame(columns=columnas).to_excel(salida, index=False, sheet_name="Base SMS")
    return salida.getvalue()
