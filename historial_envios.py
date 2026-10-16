"""
historial_envios.py — Historial de todo lo enviado (SMS y email), para /envios.

Un envío = una ejecución (manual o de una programación):
  - SMS: filas de SmsLog con la misma campaña, usuario y mensaje (plantilla)
    guardadas seguidas (un envío grande queda en varios lotes con segundos de
    diferencia: se juntan si están a menos de JUNTAR_MINUTOS). El estado de
    cada SMS sale de SmsInfobip_Valoro (por message_id) o, si no está, del
    resultado de SmsLog (lo actualiza el polling).
  - Email: filas de EmailLog con el mismo bulk_id (ID de la campaña en la
    plataforma de email).

Sin Flask: el cliente de BigQuery llega como parámetro. juntar_lotes es puro.
"""

from datetime import timedelta

from google.cloud import bigquery

from infobip_sync import COSTO_SQL, TABLA as TABLA_SMS_INFOBIP
from services.email_service import EMAIL_LOG_TABLE
from services.sms_service import SMS_LOG_TABLE

JUNTAR_MINUTOS = 5
MAX_DETALLE = 5000

# Estado de cada SMS: el de Infobip si ya está en SmsInfobip_Valoro; si no, el de SmsLog.
_CATEGORIA_SMS = """
  CASE
    WHEN s.estado = 'DELIVERED' THEN 'entregado'
    WHEN s.estado IN ('REJECTED', 'UNDELIVERABLE', 'EXPIRED') THEN 'fallido'
    WHEN s.estado IN ('PENDING', 'ACCEPTED') THEN 'pendiente'
    WHEN UPPER(l.resultado) = 'ENTREGADO' THEN 'entregado'
    WHEN UPPER(l.resultado) IN ('FALLIDO', 'ERROR', 'RECHAZADO') THEN 'fallido'
    WHEN UPPER(l.resultado) = 'PENDIENTE' THEN 'pendiente'
    ELSE 'sin_reporte'
  END"""

_SMS_CON_ESTADO = f"""
  SELECT l.*, {_CATEGORIA_SMS} AS categoria, s.estado AS estado_infobip, s.descripcion_error,
         IF(s.message_id IS NULL, 0, {COSTO_SQL}) AS costo
  FROM `{SMS_LOG_TABLE}` l
  LEFT JOIN (SELECT * FROM `{TABLA_SMS_INFOBIP}` WHERE sent_at >= TIMESTAMP_SUB(@desde, INTERVAL 1 DAY)) s
    ON s.message_id = l.message_id AND IFNULL(l.message_id, '') != ''
  WHERE l.fecha_envio >= @desde AND l.fecha_envio < @hasta"""


def _params(desde, hasta, **extra):
    params = [bigquery.ScalarQueryParameter("desde", "TIMESTAMP", desde),
              bigquery.ScalarQueryParameter("hasta", "TIMESTAMP", hasta)]
    for nombre, valor in extra.items():
        params.append(bigquery.ScalarQueryParameter(nombre, "STRING", valor))
    return bigquery.QueryJobConfig(query_parameters=params)


def juntar_lotes(grupos, minutos=JUNTAR_MINUTOS):
    """
    Junta los grupos de SMS de un mismo envío: misma campaña, usuario y
    plantilla, y el siguiente empieza a menos de `minutos` del anterior.
    grupos: dicts con campana, usuario, plantilla, inicio, fin (datetime) y
    contadores (mensajes, entregados, fallidos, pendientes, sin_reporte, costo).
    Devuelve los envíos ordenados del más reciente al más viejo.
    """
    sumar = ("mensajes", "entregados", "fallidos", "pendientes", "sin_reporte", "costo")
    envios = []
    abiertos = {}
    for g in sorted(grupos, key=lambda g: g["inicio"]):
        clave = (g["campana"], g["usuario"], g["plantilla"])
        actual = abiertos.get(clave)
        if actual and g["inicio"] - actual["fin"] <= timedelta(minutes=minutos):
            for campo in sumar:
                actual[campo] = (actual[campo] or 0) + (g[campo] or 0)
            actual["fin"] = max(actual["fin"], g["fin"])
            actual["bulk_ids"] = sorted(set(actual["bulk_ids"]) | set(g["bulk_ids"]))
            continue
        nuevo = dict(g, bulk_ids=list(g["bulk_ids"]))
        abiertos[clave] = nuevo
        envios.append(nuevo)
    return sorted(envios, key=lambda e: e["inicio"], reverse=True)


def envios_sms(client, desde, hasta, destino=""):
    """Envíos de SMS entre desde y hasta (UTC). Con destino, solo los envíos que incluyen ese teléfono."""
    filtro = ""
    if destino:
        clave = "CONCAT(IFNULL(campana, ''), '|', CAST(fecha_envio AS STRING))"
        filtro = f"""WHERE {clave} IN (
            SELECT {clave} FROM `{SMS_LOG_TABLE}`
            WHERE fecha_envio >= @desde AND fecha_envio < @hasta AND telefono LIKE CONCAT('%', @destino, '%'))"""
    sql = f"""
      SELECT IFNULL(campana, '') AS campana, IFNULL(usuario, '') AS usuario, IFNULL(plantilla, '') AS plantilla,
        fecha_envio, COUNT(*) AS mensajes,
        ARRAY_AGG(DISTINCT IFNULL(bulk_id, '')) AS bulk_ids,
        ANY_VALUE(mensaje) AS ejemplo, ANY_VALUE(consulta_sql) AS consulta,
        COUNTIF(categoria = 'entregado') AS entregados, COUNTIF(categoria = 'fallido') AS fallidos,
        COUNTIF(categoria = 'pendiente') AS pendientes, COUNTIF(categoria = 'sin_reporte') AS sin_reporte,
        SUM(costo) AS costo
      FROM ({_SMS_CON_ESTADO}) {filtro}
      GROUP BY 1, 2, 3, 4"""
    grupos = []
    for f in client.query(sql, job_config=_params(desde, hasta, destino=destino)).result():
        grupos.append({
            "campana": f["campana"], "usuario": f["usuario"], "plantilla": f["plantilla"],
            "inicio": f["fecha_envio"], "fin": f["fecha_envio"], "mensajes": f["mensajes"],
            "bulk_ids": [b for b in f["bulk_ids"] if b], "ejemplo": f["ejemplo"] or "", "consulta": f["consulta"] or "",
            "entregados": f["entregados"], "fallidos": f["fallidos"], "pendientes": f["pendientes"],
            "sin_reporte": f["sin_reporte"], "costo": float(f["costo"] or 0),
        })
    return juntar_lotes(grupos)


def envios_email(client, desde, hasta, destino=""):
    """Envíos de email (uno por campaña de la plataforma) entre desde y hasta (UTC)."""
    filtro = ""
    if destino:
        filtro = f"""AND bulk_id IN (SELECT bulk_id FROM `{EMAIL_LOG_TABLE}` WHERE fecha_envio >= @desde
                     AND fecha_envio < @hasta AND LOWER(email) LIKE CONCAT('%', LOWER(@destino), '%'))"""
    sql = f"""
      SELECT IFNULL(bulk_id, '') AS bulk_id, ANY_VALUE(IFNULL(NULLIF(campana, ''), campana_nombre)) AS campana,
        ANY_VALUE(IFNULL(usuario, '')) AS usuario, ANY_VALUE(asunto) AS asunto, ANY_VALUE(contenido) AS contenido,
        MIN(fecha_envio) AS inicio, MAX(fecha_envio) AS fin, COUNT(*) AS mensajes,
        COUNTIF(LOWER(resultado) = 'enviado') AS enviados, COUNTIF(LOWER(resultado) != 'enviado') AS fallidos
      FROM `{EMAIL_LOG_TABLE}`
      WHERE fecha_envio >= @desde AND fecha_envio < @hasta {filtro}
      GROUP BY 1"""
    return [dict(f.items()) for f in client.query(sql, job_config=_params(desde, hasta, destino=destino)).result()]


def detalle_sms(client, campana, inicio, fin):
    """Cada SMS de un envío (campaña entre inicio y fin, UTC) con su estado."""
    sql = f"""
      SELECT telefono, mensaje, fecha_envio, categoria, estado_infobip, resultado,
             IFNULL(descripcion_error, error) AS error, bulk_id, message_id, costo
      FROM ({_SMS_CON_ESTADO}) WHERE IFNULL(campana, '') = @campana
      ORDER BY fecha_envio, telefono LIMIT {MAX_DETALLE}"""
    config = _params(inicio, fin + timedelta(seconds=1), campana=campana)
    return [dict(f.items()) for f in client.query(sql, job_config=config).result()]


def detalle_email(client, bulk_id):
    """Cada email de un envío (campaña de la plataforma de email)."""
    sql = f"""
      SELECT email, asunto, fecha_envio, resultado, error FROM `{EMAIL_LOG_TABLE}`
      WHERE bulk_id = @bulk_id ORDER BY email LIMIT {MAX_DETALLE}"""
    config = bigquery.QueryJobConfig(query_parameters=[bigquery.ScalarQueryParameter("bulk_id", "STRING", bulk_id)])
    return [dict(f.items()) for f in client.query(sql, job_config=config).result()]
