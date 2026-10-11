"""
infobip_sync.py — Copia los SMS de Infobip a BigQuery (Temporal.SmsInfobip).

Infobip guarda los logs de SMS solo unos 2 días y un día completo tarda
minutos en bajar, así que el dashboard no lee la API: lee esta tabla.
  - Una fila por SMS (message_id), partida por día de envío (sent_at) y con
    vencimiento de particiones (DIAS_HISTORIA): BigQuery borra solo lo viejo.
  - Cada sincronización baja un rango de Infobip (infobip_logs.descargar, con
    reintentos), lo carga en una tabla temporal y hace MERGE por message_id:
    inserta los nuevos y actualiza el estado de los que cambiaron. Repetir un
    rango nunca duplica.

Sin Flask: el cliente de BigQuery llega como parámetro.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from google.cloud import bigquery

import infobip_logs

logger = logging.getLogger(__name__)

PROJECT_ID = "capable-arbor-209819"
TABLA = f"{PROJECT_ID}.Temporal.SmsInfobip"
DIAS_HISTORIA = 90

ESQUEMA = [
    bigquery.SchemaField("message_id", "STRING", mode="REQUIRED"),
    bigquery.SchemaField("bulk_id", "STRING"),
    bigquery.SchemaField("sender", "STRING"),
    bigquery.SchemaField("destino", "STRING"),
    bigquery.SchemaField("sent_at", "TIMESTAMP"),
    bigquery.SchemaField("done_at", "TIMESTAMP"),
    bigquery.SchemaField("mcc_mnc", "STRING"),
    bigquery.SchemaField("operador", "STRING"),
    bigquery.SchemaField("estado", "STRING", description="groupName de Infobip: DELIVERED, REJECTED, UNDELIVERABLE, EXPIRED, PENDING"),
    bigquery.SchemaField("nombre_estado", "STRING"),
    bigquery.SchemaField("error", "STRING"),
    bigquery.SchemaField("descripcion_error", "STRING"),
    bigquery.SchemaField("precio", "FLOAT", description="Precio por parte (pricePerMessage de Infobip)"),
    bigquery.SchemaField("partes", "INTEGER", description="messageCount: partes del SMS (más de 160 caracteres = 2 o más)"),
    bigquery.SchemaField("moneda", "STRING"),
    bigquery.SchemaField("texto", "STRING"),
    bigquery.SchemaField("fecha_actualizacion", "TIMESTAMP", description="Última vez que la sincronización escribió la fila"),
]
COLUMNAS = [c.name for c in ESQUEMA]

# Costo de cada SMS (issue #41): precio × partes, en todos los estados (Infobip
# también cobra rechazados y no entregados). Validado contra el portal de
# Infobip el 06/10/2026: 679.943,7 vs 679.944 COP. Las filas copiadas antes de
# guardar partes (hasta el 07/10) se estiman por el largo del texto.
COSTO_SQL = "IFNULL(precio, 0) * IFNULL(partes, IF(LENGTH(texto) > 160, 2, 1))"


def asegurar_tabla(client):
    """Crea la tabla (partida por día de sent_at, agrupada por bulk_id y estado) si no existe."""
    tabla = bigquery.Table(TABLA, schema=ESQUEMA)
    tabla.description = "SMS de la cuenta de Infobip (/sms/3/logs), sincronizados por la app (infobip_sync.py)."
    tabla.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="sent_at",
        expiration_ms=DIAS_HISTORIA * 24 * 3600 * 1000)
    tabla.clustering_fields = ["bulk_id", "estado"]
    tabla = client.create_table(tabla, exists_ok=True)
    # Columnas nuevas (partes, moneda) en la tabla que ya existía.
    existentes = {c.name for c in tabla.schema}
    nuevas = [c for c in ESQUEMA if c.name not in existentes]
    if nuevas:
        tabla.schema = list(tabla.schema) + nuevas
        client.update_table(tabla, ["schema"])
        logger.info(f"📨 SmsInfobip: columnas agregadas {[c.name for c in nuevas]}")


def _fila(sms, ahora_iso):
    fecha = lambda d: d.isoformat() if d else None
    return {
        "message_id": sms.message_id, "bulk_id": sms.bulk_id, "sender": sms.sender, "destino": sms.destino,
        "sent_at": fecha(sms.sent_at), "done_at": fecha(sms.done_at), "mcc_mnc": sms.mcc_mnc,
        "operador": infobip_logs.operador(sms.mcc_mnc), "estado": sms.estado, "nombre_estado": sms.nombre_estado,
        "error": sms.error, "descripcion_error": sms.descripcion_error, "precio": sms.precio,
        "partes": sms.partes, "moneda": sms.moneda,
        "texto": sms.texto, "fecha_actualizacion": ahora_iso,
    }


def sincronizar(client, api_key, base_url, desde, hasta):
    """
    Baja de Infobip los SMS enviados entre desde y hasta y los guarda en la
    tabla. Devuelve {"sms", "insertados", "actualizados", "ventanas_con_error", "segundos"}.
    """
    t0 = time.time()
    asegurar_tabla(client)
    mensajes, fallidas = infobip_logs.descargar(api_key, base_url, desde, hasta, usar_cache=False)
    mensajes = [m for m in mensajes if m.message_id]
    resumen = {"sms": len(mensajes), "insertados": 0, "actualizados": 0, "ventanas_con_error": fallidas}
    if not mensajes:
        resumen["segundos"] = round(time.time() - t0, 1)
        return resumen

    ahora_iso = datetime.now(timezone.utc).isoformat()
    temporal = f"{PROJECT_ID}.Temporal.SmsInfobip_carga_{uuid4().hex[:12]}"
    try:
        client.load_table_from_json(
            [_fila(m, ahora_iso) for m in mensajes], temporal,
            job_config=bigquery.LoadJobConfig(schema=ESQUEMA, write_disposition="WRITE_TRUNCATE"),
        ).result()
        primero = min(m.sent_at for m in mensajes if m.sent_at) if any(m.sent_at for m in mensajes) else desde
        actualizar = ", ".join(f"{c} = S.{c}" for c in COLUMNAS if c != "message_id")
        job = client.query(f"""
            MERGE `{TABLA}` T
            USING (
              SELECT * FROM `{temporal}`
              QUALIFY ROW_NUMBER() OVER (PARTITION BY message_id ORDER BY done_at DESC) = 1
            ) S
            ON T.message_id = S.message_id
               AND T.sent_at >= TIMESTAMP_SUB(@primero, INTERVAL 1 DAY)
            WHEN MATCHED AND (IFNULL(T.estado, '') != IFNULL(S.estado, '')
                              OR IFNULL(T.done_at, TIMESTAMP '1970-01-01') != IFNULL(S.done_at, TIMESTAMP '1970-01-01')
                              OR IFNULL(T.error, '') != IFNULL(S.error, '')
                              OR IFNULL(T.partes, 0) != IFNULL(S.partes, 0))
              THEN UPDATE SET {actualizar}
            WHEN NOT MATCHED THEN INSERT ({", ".join(COLUMNAS)}) VALUES ({", ".join("S." + c for c in COLUMNAS)})
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("primero", "TIMESTAMP", primero)]))
        job.result()
        stats = job.dml_stats
        resumen["insertados"] = getattr(stats, "inserted_row_count", 0) or 0
        resumen["actualizados"] = getattr(stats, "updated_row_count", 0) or 0
    finally:
        client.delete_table(temporal, not_found_ok=True)
    resumen["segundos"] = round(time.time() - t0, 1)
    logger.info(f"📨 SmsInfobip: {resumen}")
    return resumen
