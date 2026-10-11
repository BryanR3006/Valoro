"""
Validación de consultas SQL recibidas del usuario antes de ejecutarlas en BigQuery.

Las pantallas de la app (vistas previas SMS/Email, envíos y programaciones)
reciben una consulta escrita por el usuario y la ejecutan con la cuenta de
servicio. Este módulo garantiza que esa consulta sea UNA sola sentencia
de lectura (SELECT / WITH ... SELECT):

- ``validar_consulta_lectura``: revisión léxica, sin red. Ignora comentarios,
  cadenas e identificadores entre comillas invertidas; rechaza ``;`` intermedios
  (varias sentencias) y cualquier sentencia que no empiece por SELECT o WITH.
- ``verificar_consulta_lectura``: lo anterior + un *dry run* en BigQuery, que
  confirma con el propio motor que el tipo de sentencia es ``SELECT`` (no cuesta
  ni lee datos).

Sin dependencias de Flask/DB: el cliente BigQuery se pasa como parámetro, así
que se puede probar con objetos falsos.
"""

from __future__ import annotations

import re


class ConsultaNoPermitida(ValueError):
    """La consulta no es una única sentencia de solo lectura."""


_INICIO_PERMITIDO = re.compile(r"^\(*\s*(SELECT|WITH)\b", re.IGNORECASE)


def _esqueleto(sql: str) -> str:
    """
    Devuelve la consulta con comentarios, cadenas e identificadores `...`
    reemplazados por espacios, para analizar solo el código SQL real.
    Lanza ConsultaNoPermitida si queda una cadena o comentario sin cerrar.
    """
    salida = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        dos = sql[i:i + 2]

        # Comentarios de línea: -- y #
        if dos == "--" or c == "#":
            fin = sql.find("\n", i)
            i = n if fin == -1 else fin
            salida.append(" ")
            continue

        # Comentario de bloque
        if dos == "/*":
            fin = sql.find("*/", i + 2)
            if fin == -1:
                raise ConsultaNoPermitida("La consulta tiene un comentario /* sin cerrar.")
            i = fin + 2
            salida.append(" ")
            continue

        # Cadenas ('...', "...", '''...''', """...""", con prefijos r/b) e identificadores `...`
        if c in ("'", '"', "`"):
            anterior = sql[i - 1].lower() if i > 0 else ""
            cruda = anterior == "r" or (anterior == "b" and i > 1 and sql[i - 2].lower() == "r")
            triple = c != "`" and sql[i:i + 3] == c * 3
            cierre = c * 3 if triple else c
            j = i + len(cierre)
            while True:
                if j >= n:
                    raise ConsultaNoPermitida("La consulta tiene una cadena o identificador sin cerrar.")
                if sql[j] == "\\" and not cruda and c != "`":
                    j += 2
                    continue
                if sql.startswith(cierre, j):
                    j += len(cierre)
                    break
                j += 1
            salida.append(" ")
            i = j
            continue

        salida.append(c)
        i += 1
    return "".join(salida)


def validar_consulta_lectura(sql) -> str:
    """
    Valida que ``sql`` sea una única sentencia SELECT/WITH.
    Devuelve la consulta lista para ejecutar (sin espacios ni ``;`` finales).
    Lanza ConsultaNoPermitida con un mensaje apto para el usuario.
    """
    if not sql or not isinstance(sql, str) or not sql.strip():
        raise ConsultaNoPermitida("La consulta es obligatoria.")

    texto = sql.strip()
    esqueleto = _esqueleto(texto).strip()

    # Se toleran ';' al final; cualquier otro indica varias sentencias.
    esqueleto = esqueleto.rstrip("; \t\r\n")
    if ";" in esqueleto:
        raise ConsultaNoPermitida(
            "Solo se permite una sentencia SQL (se encontró ';' en medio de la consulta)."
        )
    if not esqueleto:
        raise ConsultaNoPermitida("La consulta es obligatoria.")
    if not _INICIO_PERMITIDO.match(esqueleto):
        raise ConsultaNoPermitida("Solo se permiten consultas SELECT o WITH.")

    # Quitar los ';' finales del texto real (pueden venir tras un comentario final).
    while True:
        recortado = texto.rstrip().rstrip(";").rstrip()
        if recortado == texto:
            break
        texto = recortado
    return texto


def verificar_consulta_lectura(client, sql) -> str:
    """
    Validación léxica + dry run en BigQuery: el motor debe reportar
    ``statement_type == "SELECT"``. Devuelve la consulta lista para ejecutar.
    """
    texto = validar_consulta_lectura(sql)
    from google.cloud import bigquery  # import local: el módulo se puede probar sin la librería

    job = client.query(texto, job_config=bigquery.QueryJobConfig(dry_run=True, use_query_cache=False))
    tipo = (getattr(job, "statement_type", None) or "").upper()
    if tipo != "SELECT":
        raise ConsultaNoPermitida(
            f"Solo se permiten consultas de lectura (BigQuery la identificó como '{tipo or 'desconocida'}')."
        )
    return texto
