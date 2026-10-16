"""
dashboard_sms.py — Dashboard de envíos de SMS (Temporal.SmsLog).

Una fila de SmsLog = un SMS. El estado lo actualiza el polling
(sms_polling_estados) consultando Infobip:
  - entregado:   ENTREGADO
  - fallido:     FALLIDO (o rechazado / vencido / no entregable)
  - pendiente:   PENDIENTE / enviado, de las últimas 48 h (todavía puede cambiar)
  - sin_reporte: lo demás, o pendiente con más de 48 h (Infobip ya no lo reporta)

Por defecto muestra las últimas 48 horas. Horas en hora de Colombia.
Sin Flask ni base local: el cliente de BigQuery llega como parámetro.
"""

import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytz
from google.cloud import bigquery

TABLA_SMS_LOG = "capable-arbor-209819.Temporal.SmsLog"
COLOMBIA_TZ = pytz.timezone("America/Bogota")
HORAS_POR_DEFECTO = 48
ESTADOS = ["entregado", "fallido", "pendiente", "sin_reporte"]
CACHE_SEGUNDOS = 30
_cache = {}

_ESTADO_SQL = """
  CASE
    WHEN UPPER(IFNULL(resultado, '')) = 'ENTREGADO' THEN 'entregado'
    WHEN UPPER(IFNULL(resultado, '')) IN ('FALLIDO', 'UNDELIVERABLE', 'REJECTED', 'EXPIRED') THEN 'fallido'
    WHEN UPPER(IFNULL(resultado, '')) IN ('PENDIENTE', 'PENDING', 'ENVIADO', 'ACCEPTED')
         AND fecha_envio >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 48 HOUR) THEN 'pendiente'
    ELSE 'sin_reporte'
  END"""

_FILTRO = """fecha_envio >= @desde AND fecha_envio < @hasta
  AND (@campana = '' OR LOWER(IFNULL(campana, '')) LIKE CONCAT('%', LOWER(@campana), '%'))"""

_CONTEOS = """
  COUNT(*) AS total,
  COUNTIF(estado = 'entregado') AS entregados,
  COUNTIF(estado = 'fallido') AS fallidos,
  COUNTIF(estado = 'pendiente') AS pendientes,
  COUNTIF(estado = 'sin_reporte') AS sin_reporte"""

_BASE = f"SELECT *, {_ESTADO_SQL} AS estado FROM `{TABLA_SMS_LOG}` WHERE {_FILTRO}"

CONSULTAS = {
    "envios": f"""
      SELECT IFNULL(NULLIF(bulk_id, ''), '(sin bulk_id)') AS bulk_id,
        ANY_VALUE(campana) AS campana, ANY_VALUE(usuario) AS usuario,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MIN(fecha_envio), 'America/Bogota') AS fecha_envio,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(fecha_actualizacion), 'America/Bogota') AS ultima_actualizacion,
        COUNTIF(IFNULL(message_id, '') = '') AS sin_message_id,
        UNIX_SECONDS(MIN(fecha_envio)) AS orden,
        {_CONTEOS}
      FROM ({_BASE})
      GROUP BY 1 ORDER BY orden DESC LIMIT 1000""",
    "horas": f"""
      SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:00', TIMESTAMP_TRUNC(fecha_envio, HOUR, 'America/Bogota'), 'America/Bogota') AS hora,
        {_CONTEOS}
      FROM ({_BASE})
      GROUP BY 1 ORDER BY 1""",
    "campanas": f"""
      SELECT IFNULL(NULLIF(campana, ''), '(sin campaña)') AS campana, COUNT(DISTINCT bulk_id) AS envios, {_CONTEOS}
      FROM ({_BASE})
      GROUP BY 1 ORDER BY total DESC LIMIT 50""",
    "errores": f"""
      SELECT IFNULL(NULLIF(error, ''), '(sin descripción)') AS error, COUNT(*) AS mensajes
      FROM ({_BASE})
      WHERE estado = 'fallido'
      GROUP BY 1 ORDER BY mensajes DESC LIMIT 10""",
    "ultimo": f"""
      SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(fecha_envio), 'America/Bogota') AS ultimo_envio
      FROM `{TABLA_SMS_LOG}`""",
}


def _pct(parte, total):
    return round(100.0 * parte / total, 1) if total else 0.0


def _con_tasas(fila):
    total = fila.get("total") or 0
    fila["tasa_entrega"] = _pct(fila.get("entregados") or 0, total)
    fila["tasa_fallo"] = _pct(fila.get("fallidos") or 0, total)
    return fila


def rango(horas=None, desde=None, hasta=None):
    """(desde, hasta) como datetime con zona. Por defecto las últimas 48 horas."""
    ahora = datetime.now(COLOMBIA_TZ)
    if desde:
        inicio = COLOMBIA_TZ.localize(datetime.strptime(desde, "%Y-%m-%d"))
        fin = COLOMBIA_TZ.localize(datetime.strptime(hasta or desde, "%Y-%m-%d")) + timedelta(days=1)
        return inicio, min(fin, ahora + timedelta(minutes=1))
    return ahora - timedelta(hours=int(horas or HORAS_POR_DEFECTO)), ahora + timedelta(minutes=1)


def obtener_dashboard(client, horas=None, desde=None, hasta=None, campana="") -> dict:
    inicio, fin = rango(horas, desde, hasta)
    clave = (inicio.strftime("%Y%m%d%H%M"), fin.strftime("%Y%m%d%H"), campana or "")
    guardado = _cache.get(clave)
    if guardado and time.time() - guardado[0] < CACHE_SEGUNDOS:
        return guardado[1]

    params = [
        bigquery.ScalarQueryParameter("desde", "TIMESTAMP", inicio),
        bigquery.ScalarQueryParameter("hasta", "TIMESTAMP", fin),
        bigquery.ScalarQueryParameter("campana", "STRING", campana or ""),
    ]

    def ejecutar(sql):
        config = bigquery.QueryJobConfig(query_parameters=params)
        return [dict(f.items()) for f in client.query(sql, job_config=config).result()]

    with ThreadPoolExecutor(max_workers=len(CONSULTAS)) as pool:
        futuros = {n: pool.submit(ejecutar, sql) for n, sql in CONSULTAS.items()}
        datos = {n: f.result() for n, f in futuros.items()}

    envios = [_con_tasas(e) for e in datos["envios"]]
    total = _con_tasas({k: sum(e[k] for e in envios) for k in ("total", "entregados", "fallidos", "pendientes", "sin_reporte")})
    total["envios"] = len(envios)
    total["sin_message_id"] = sum(e["sin_message_id"] for e in envios)
    resultado = {
        "fuente": "smslog",
        "desde": inicio.strftime("%Y-%m-%d %H:%M"),
        "hasta": min(fin, datetime.now(COLOMBIA_TZ)).strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "envios": envios,
        "horas": [_con_tasas(h) for h in datos["horas"]],
        "campanas": [_con_tasas(c) for c in datos["campanas"]],
        "errores": datos["errores"],
        "ultimo_envio": (datos["ultimo"][0]["ultimo_envio"] if datos["ultimo"] else None),
        "generado": datetime.now(COLOMBIA_TZ).strftime("%Y-%m-%d %H:%M:%S"),
    }
    _cache[clave] = (time.time(), resultado)
    return resultado


# ==================== FUENTE: SMS DE INFOBIP EN BIGQUERY ====================
# Todos los SMS de la cuenta, copiados de Infobip a Temporal.SmsInfobip_Valoro por
# infobip_sync.py (job sms_infobip_sync). El dashboard lee la tabla: abre en
# segundos, sirve para cualquier rango y guarda historia (Infobip solo ~2 días).
# Se cruzan con SmsLog por bulk_id para campaña y usuario ("desde la app").

from infobip_sync import COSTO_SQL, TABLA as TABLA_SMS_INFOBIP
import infobip_logs

FILAS_DETALLE = 500
MAX_FILAS_CSV = 300000

_CATEGORIA_INFOBIP = """
  CASE
    WHEN estado = 'DELIVERED' THEN 'entregado'
    WHEN estado IN ('REJECTED', 'UNDELIVERABLE', 'EXPIRED') THEN 'fallido'
    WHEN estado IN ('PENDING', 'ACCEPTED') THEN 'pendiente'
    ELSE 'sin_reporte'
  END"""

_BASE_INFOBIP = f"""
  SELECT s.*, {_CATEGORIA_INFOBIP} AS categoria, l.campana, l.usuario, l.bulk_id IS NOT NULL AS en_smslog,
         TIMESTAMP_DIFF(s.done_at, s.sent_at, MILLISECOND) / 1000 AS demora
  FROM `{TABLA_SMS_INFOBIP}` s
  LEFT JOIN (SELECT bulk_id, ANY_VALUE(campana) AS campana, ANY_VALUE(usuario) AS usuario
             FROM `{TABLA_SMS_LOG}` WHERE IFNULL(bulk_id, '') != '' GROUP BY bulk_id) l USING (bulk_id)
  WHERE s.sent_at >= @desde AND s.sent_at < @hasta
    AND (@campana = '' OR LOWER(IFNULL(l.campana, '')) LIKE CONCAT('%', LOWER(@campana), '%'))"""

_CONTEOS_INFOBIP = f"""
  COUNT(*) AS total,
  COUNTIF(categoria = 'entregado') AS entregados,
  COUNTIF(categoria = 'fallido') AS fallidos,
  COUNTIF(categoria = 'pendiente') AS pendientes,
  COUNTIF(categoria = 'sin_reporte') AS sin_reporte,
  COUNTIF(estado = 'REJECTED') AS rechazados,
  COUNTIF(estado IN ('UNDELIVERABLE', 'EXPIRED')) AS no_entregados,
  ROUND(SUM({COSTO_SQL})) AS costo"""

CONSULTAS_INFOBIP = {
    "total": f"""
      SELECT {_CONTEOS_INFOBIP}, COUNT(DISTINCT destino) AS destinos_unicos,
        COUNTIF(categoria = 'entregado' AND demora < 5) AS entregados_5s,
        COUNT(DISTINCT bulk_id) AS envios, COUNT(DISTINCT IF(en_smslog, bulk_id, NULL)) AS desde_app,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(sent_at), 'America/Bogota') AS ultimo_envio
      FROM ({_BASE_INFOBIP})""",
    "envios": f"""
      SELECT IFNULL(NULLIF(bulk_id, ''), '(sin bulk_id)') AS bulk_id, {_CONTEOS_INFOBIP},
        ANY_VALUE(campana) AS campana, ANY_VALUE(usuario) AS usuario, LOGICAL_OR(en_smslog) AS en_smslog,
        ANY_VALUE(sender) AS sender, ANY_VALUE(texto) AS texto,
        APPROX_TOP_COUNT(operador, 1)[OFFSET(0)].value AS operador_principal,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MIN(sent_at), 'America/Bogota') AS fecha_envio,
        FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(sent_at), 'America/Bogota') AS ultima_actualizacion,
        UNIX_SECONDS(MIN(sent_at)) AS orden
      FROM ({_BASE_INFOBIP})
      GROUP BY 1 ORDER BY orden DESC LIMIT 2000""",
    "horas": f"""
      SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:00', sent_at, 'America/Bogota') AS hora, {_CONTEOS_INFOBIP}
      FROM ({_BASE_INFOBIP}) GROUP BY 1 ORDER BY 1""",
    "dias": f"""
      SELECT FORMAT_TIMESTAMP('%Y-%m-%d', sent_at, 'America/Bogota') AS dia, {_CONTEOS_INFOBIP}
      FROM ({_BASE_INFOBIP}) GROUP BY 1 ORDER BY 1""",
    "operadores": f"""
      SELECT IFNULL(operador, 'Desconocido') AS operador, {_CONTEOS_INFOBIP},
        APPROX_QUANTILES(IF(categoria = 'entregado', demora, NULL), 100)[OFFSET(50)] AS p50_seg,
        APPROX_QUANTILES(IF(categoria = 'entregado', demora, NULL), 100)[OFFSET(95)] AS p95_seg,
        COUNTIF(categoria = 'entregado' AND demora > 300) AS mas_5min
      FROM ({_BASE_INFOBIP}) GROUP BY 1 ORDER BY total DESC""",
    "errores": f"""
      SELECT error, ANY_VALUE(descripcion_error) AS descripcion, COUNT(*) AS mensajes
      FROM ({_BASE_INFOBIP})
      WHERE IFNULL(error, '') NOT IN ('', 'NO_ERROR')
      GROUP BY 1 ORDER BY mensajes DESC LIMIT 15""",
    "sincronizado": f"""
      SELECT FORMAT_TIMESTAMP('%Y-%m-%d %H:%M', MAX(fecha_actualizacion), 'America/Bogota') AS ultima
      FROM `{TABLA_SMS_INFOBIP}` WHERE sent_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 3 DAY)""",
}
_agregado = {}


def periodo_infobip(periodo, desde=None, hasta=None):
    """(desde, hasta) en hora Colombia: hoy, ayer, 48h, 7d o fechas (AAAA-MM-DD)."""
    ahora = datetime.now(COLOMBIA_TZ)
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    if desde:
        ini = COLOMBIA_TZ.localize(datetime.strptime(desde, "%Y-%m-%d"))
        fin = COLOMBIA_TZ.localize(datetime.strptime(hasta or desde, "%Y-%m-%d")) + timedelta(days=1)
        return ini, min(fin, ahora + timedelta(minutes=1))
    if periodo == "ayer":
        return hoy - timedelta(days=1), hoy
    if periodo == "48h":
        return ahora - timedelta(hours=48), ahora + timedelta(minutes=1)
    if periodo == "7d":
        return hoy - timedelta(days=6), ahora + timedelta(minutes=1)
    return hoy, ahora + timedelta(minutes=1)


def _params_infobip(ini, fin, campana):
    return [bigquery.ScalarQueryParameter("desde", "TIMESTAMP", ini),
            bigquery.ScalarQueryParameter("hasta", "TIMESTAMP", fin),
            bigquery.ScalarQueryParameter("campana", "STRING", campana or "")]


def _cerrar_infobip(fila):
    fila = _con_tasas(fila)
    fila["costo_por_entrega"] = round(fila["costo"] / fila["entregados"], 2) if fila.get("entregados") else 0
    for k in ("p50_seg", "p95_seg"):
        if fila.get(k) is not None:
            fila[k] = round(fila[k], 1)
    return fila


def obtener_dashboard_infobip(client, periodo="hoy", desde=None, hasta=None, campana=""):
    """Totales, lotes, horas, días, operadores y errores desde Temporal.SmsInfobip_Valoro."""
    ini, fin = periodo_infobip(periodo, desde, hasta)
    clave = (ini.strftime("%Y%m%d%H%M"), fin.strftime("%Y%m%d%H%M")[:-1], campana or "")
    guardado = _agregado.get(clave)
    if guardado and time.time() - guardado[0] < CACHE_SEGUNDOS:
        return guardado[1]

    params = _params_infobip(ini, fin, campana)

    def ejecutar(sql):
        return [dict(f.items()) for f in client.query(sql, job_config=bigquery.QueryJobConfig(query_parameters=params)).result()]

    with ThreadPoolExecutor(max_workers=len(CONSULTAS_INFOBIP)) as pool:
        futuros = {n: pool.submit(ejecutar, sql) for n, sql in CONSULTAS_INFOBIP.items()}
        datos = {n: f.result() for n, f in futuros.items()}

    total = _cerrar_infobip(datos["total"][0])
    total.update(tasa_5s=_pct(total["entregados_5s"], total["entregados"]), sin_message_id=0,
                 no_registrados=total["envios"] - total["desde_app"])
    for e in datos["errores"]:
        e["accion"] = infobip_logs.ACCIONES_ERROR.get(e["error"], "—")
    ahora = datetime.now(COLOMBIA_TZ)
    resultado = {
        "fuente": "infobip",
        "desde": ini.strftime("%Y-%m-%d %H:%M"),
        "hasta": min(fin, ahora).strftime("%Y-%m-%d %H:%M"),
        "total": total,
        "envios": [_cerrar_infobip(e) for e in datos["envios"]],
        "horas": [_cerrar_infobip(h) for h in datos["horas"]],
        "dias": [_cerrar_infobip(d) for d in datos["dias"]],
        "operadores": [_cerrar_infobip(o) for o in datos["operadores"]],
        "errores": datos["errores"],
        "campanas": [],
        "ventanas_con_error": [],
        "ultimo_envio": total.pop("ultimo_envio"),
        "ultima_sincronizacion": datos["sincronizado"][0]["ultima"] if datos["sincronizado"] else None,
        "generado": ahora.strftime("%Y-%m-%d %H:%M:%S"),
    }
    _agregado[clave] = (time.time(), resultado)
    return resultado


def _consulta_mensajes(filtros):
    """(sql WHERE extra, parámetros) para los filtros del detalle de mensajes."""
    condiciones, params = [], []
    for campo, columna in (("bulk_id", "bulk_id"), ("estado", "estado"), ("operador", "operador"), ("error", "error")):
        if filtros.get(campo):
            condiciones.append(f"{columna} = @{campo}")
            params.append(bigquery.ScalarQueryParameter(campo, "STRING", filtros[campo]))
    if filtros.get("buscar"):
        condiciones.append("LOWER(CONCAT(IFNULL(destino, ''), ' ', IFNULL(texto, ''), ' ', IFNULL(bulk_id, ''))) LIKE @buscar")
        params.append(bigquery.ScalarQueryParameter("buscar", "STRING", f"%{filtros['buscar'].strip().lower()}%"))
    return (" AND " + " AND ".join(condiciones)) if condiciones else "", params


_COLUMNAS_MENSAJE = """
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', sent_at, 'America/Bogota') AS enviado,
  FORMAT_TIMESTAMP('%Y-%m-%d %H:%M:%S', done_at, 'America/Bogota') AS entregado,
  ROUND(demora, 1) AS demora_seg, bulk_id, destino, sender, operador, estado, nombre_estado,
  error, descripcion_error, precio, texto, message_id"""


def detalle_infobip(client, bulk_id="", periodo="hoy", desde=None, hasta=None,
                    estado="", operador="", error="", buscar="", pagina=1, todo=False):
    """SMS de la tabla filtrados, de a FILAS_DETALLE por página (todo=True para el CSV)."""
    ini, fin = periodo_infobip(periodo, desde, hasta)
    extra, params = _consulta_mensajes(dict(bulk_id=bulk_id, estado=estado, operador=operador, error=error, buscar=buscar))
    params += _params_infobip(ini, fin, "")
    pagina = max(1, int(pagina or 1))
    config = bigquery.QueryJobConfig(query_parameters=params)
    total = next(iter(client.query(f"SELECT COUNT(*) AS n FROM ({_BASE_INFOBIP}) WHERE TRUE{extra}",
                                   job_config=config).result()))["n"]
    limite = f"LIMIT {MAX_FILAS_CSV}" if todo else f"LIMIT {FILAS_DETALLE} OFFSET {(pagina - 1) * FILAS_DETALLE}"
    filas = [dict(f.items()) for f in client.query(
        f"SELECT {_COLUMNAS_MENSAJE} FROM ({_BASE_INFOBIP}) WHERE TRUE{extra} ORDER BY sent_at, message_id {limite}",
        job_config=config).result()]
    for f in filas:
        f["accion"] = infobip_logs.ACCIONES_ERROR.get(f["error"], "—") if f.get("error") not in (None, "", "NO_ERROR") else ""
    return {"total": total, "pagina": pagina, "por_pagina": FILAS_DETALLE, "filas": filas}
