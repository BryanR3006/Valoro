"""
Variables {{...}} de las plantillas frente a las columnas de la consulta (SMS y email).

  - Se comparan sin mayúsculas, tildes, espacios, guiones ni guiones bajos:
    {{Nombre Cliente}} = columna nombre_cliente = NOMBRECLIENTE.
  - El nombre del cliente tiene varios alias (nombre, Nombre Cliente,
    customer_name...): si la plantilla usa uno y la consulta trae otro, sirve igual.
  - Los IDs que asigna el usuario (ID del mensaje y de la campaña) reemplazan
    los de la consulta en cada fila (poner_ids).

Sin Flask, base de datos ni red.
"""
import re
import unicodedata


def clave(nombre) -> str:
    """'Nombre Cliente', 'nombre_cliente' y 'NOMBRECLIENTE' dan 'nombrecliente'."""
    texto = unicodedata.normalize("NFKD", str(nombre or ""))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", texto.lower())


# Alias del nombre del cliente (ya pasados por clave()).
ALIAS_NOMBRE = {"nombre", "nombrecliente", "customername", "nombrecompleto", "nombres", "name"}


def es_nombre(nombre) -> bool:
    """True si la variable o columna es el nombre del cliente."""
    return clave(nombre) in ALIAS_NOMBRE


def buscar_columna(variable, columnas):
    """
    Columna (tal como viene en la fila) que corresponde a la variable, o None.
    Primero la del mismo nombre; si la variable es el nombre del cliente,
    cualquier columna de nombre.
    """
    columnas = list(columnas)
    buscada = clave(variable)
    for columna in columnas:
        if clave(columna) == buscada:
            return columna
    if buscada in ALIAS_NOMBRE:
        for alias in ("nombre", "nombrecliente", "customername", "nombrecompleto", "nombres", "name"):
            for columna in columnas:
                if clave(columna) == alias:
                    return columna
    return None


# Columnas donde van los IDs que asigna el usuario (plantilla o pantalla), en SMS y email.
COLUMNA_ID_MENSAJE = "Id Mensaje"
COLUMNA_ID_CAMPANA = "Id Campaña"


def poner_ids(fila, id_mensaje="", id_campana=""):
    """
    Pone los IDs asignados en las columnas 'Id Mensaje' / 'Id Campaña' de la
    fila, en lugar de los que trae la consulta (o el Excel), y quita las otras
    formas del mismo nombre (id_mensaje, ID MENSAJE…) para no repetir columnas.
    Van primero: en SMS, si el callbackData se recorta a 4000 caracteres no se
    pierden. Sin IDs la fila queda como viene.
    """
    ids = {}
    if str(id_campana or "").strip():
        ids[COLUMNA_ID_CAMPANA] = str(id_campana).strip()
    if str(id_mensaje or "").strip():
        ids[COLUMNA_ID_MENSAJE] = str(id_mensaje).strip()
    if not ids:
        return fila
    claves = {clave(c) for c in ids}
    return {**ids, **{k: v for k, v in fila.items() if clave(k) not in claves}}
