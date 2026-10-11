"""
bigquery.py — Lectura de bases para SMS y email desde BigQuery.

Solo consultas de lectura (SELECT/WITH), verificadas con consulta_segura.
Si BigQuery rechaza la consulta, el mensaje real del error vuelve en "message".
"""
import logging

from consulta_segura import ConsultaNoPermitida, verificar_consulta_lectura

logger = logging.getLogger(__name__)


def fetch_sms_query_rows(client, query: str, *, max_rows: int = 50000) -> dict:
    """Ejecuta una consulta de lectura (SELECT/WITH) y devuelve las filas tal cual, para SMS y email."""
    try:
        if not query or not isinstance(query, str):
            return {"success": False, "message": "La consulta es obligatoria.", "rows": []}
        try:
            query_text = verificar_consulta_lectura(client, query)
        except ConsultaNoPermitida as exc:
            return {"success": False, "message": str(exc), "rows": []}
        result = client.query(query_text).result(max_results=max_rows)
        rows = []
        for row in result:
            rows.append(dict(row))
            if len(rows) >= max_rows:
                break
        return {"success": True, "rows": rows, "total": len(rows),
                "message": f"Se obtuvieron {len(rows)} fila(s) desde BigQuery."}
    except Exception as exc:
        logger.error(f"Error en la consulta a BigQuery: {exc}")
        return {"success": False, "message": mensaje_error_bigquery(exc), "rows": []}


def mensaje_error_bigquery(exc) -> str:
    """Mensaje real del error de BigQuery (sin la URL de la API), para mostrarlo en pantalla."""
    mensaje = getattr(exc, "message", None) or str(exc)
    return f"Error de BigQuery: {mensaje}"
