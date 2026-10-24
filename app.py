from datetime import datetime, timezone, timedelta
from flask import jsonify, render_template, request, send_from_directory, send_file
from uuid import uuid4
import io

# Copia de prueba (APP_COPIA_PRUEBA=1): revisa carpeta y puerto ANTES de
# importar backend, que arranca el scheduler, la base de datos y BigQuery. Si
# la carpeta no es la de esta copia o el puerto es 5000, termina aquí.
from modo_prueba import proteger_arranque_copia_prueba
proteger_arranque_copia_prueba(__file__)
# Una sola app por carpeta (instancia_unica.py): dos a la vez duplican las
# programaciones y bloquean app_log.txt. También antes de importar backend.
from instancia_unica import proteger_instancia_unica
proteger_instancia_unica(__file__)

import json
import os
import re
from apscheduler.triggers.cron import CronTrigger
import pytz
COLOMBIA_TZ = pytz.timezone("America/Bogota")

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from backend import (
    app, db, scheduler, load_config, logger,
    log_task, log_gui_action, read_recent_log_lines, execution_logger, registrar_resumen_consola,
    cleanup_old_log_files, reschedule_console_message_job, DOWNLOAD_FOLDER,
    log_activity, logger_sms, logger_email,
)
from general_params import load_general_parameters, save_general_parameters
from conexion_bigquery import get_bigquery_client
from bigquery import fetch_sms_query_rows
from services.sms_service import (
    SMS_LOG_TABLE, SmsServiceError, enviar_sms_desde_filas, preview_sms, resumen_filtros_sms,
)
from services.email_client import EmailClient, EmailClientError
from services.email_service import (
    EMAIL_LOG_TABLE, EmailServiceError, preview_email,
    filtrar_destinatarios, enviar_campana_email,
)

# ==================== CONFIGURACIÓN GLOBAL ====================
PROJECT_ID = "capable-arbor-209819"
bq_client = None


# ==================== USUARIOS (login, perfiles y registro de acciones) ====================
# La guardia (before_request) exige sesión y revisa el perfil en todas las rutas
# desde un solo lugar (ver usuarios.py). Los usuarios se comparten entre PC por
# BigQuery (usuarios_bq.py): al arrancar y cada minuto.
import usuarios
import usuarios_bq
from registro_logs import usuario_sesion
usuarios.instalar(app)
scheduler.add_job(usuarios_bq.sincronizar, trigger=DateTrigger(run_date=datetime.now(COLOMBIA_TZ) + timedelta(seconds=3)),
                  args=[app, "arranque"], id="usuarios_sync_arranque", replace_existing=True)
scheduler.add_job(usuarios_bq.sincronizar, trigger=IntervalTrigger(seconds=60), args=[app], id="usuarios_sync",
                  replace_existing=True, max_instances=1, coalesce=True)


#==================== CONFIGURACIÓN DE LOGS ====================
# logger_sms escribe en logs/sms.log y logger_email en logs/email.log (ver backend.py).


#==================== PÁGINAS PRINCIPALES ====================

@app.context_processor
def _endpoint_actual():
    """Página actual para marcar el enlace activo de la barra lateral."""
    from flask import has_request_context
    # Los jobs del scheduler también renderizan plantillas, sin petición web.
    return {"current_endpoint": (request.endpoint or "") if has_request_context() else ""}



@app.route("/")
def index():
    """Inicio: lo enviado hoy (lo carga /api/inicio/resumen) y la actividad reciente."""
    return render_template("index.html")


def _hora_colombia(fecha):
    """'HH:MM' en hora de Colombia de una fecha UTC (con o sin zona). Vacío si no hay fecha."""
    if not fecha:
        return ""
    if fecha.tzinfo is None:
        fecha = pytz.utc.localize(fecha)
    return fecha.astimezone(COLOMBIA_TZ).strftime("%H:%M")


def _texto_intervalo(minutos):
    """120 → '2 h', 90 → '1 h 30', 30 → '30 min'."""
    horas, resto = divmod(int(minutos), 60)
    if not horas:
        return f"{resto} min"
    return f"{horas} h" + (f" {resto:02d}" if resto else "")


def _ejecuciones_de_hoy(hoy):
    """
    ({(canal, id de la programación): [ejecuciones de hoy como dict]}, desde):
    desde es cuándo empezó el registro de ejecuciones (antes no hay datos por turno).
    """
    from database import EjecucionProgramacion
    inicio = datetime.combine(hoy, datetime.min.time())
    por_prog = {}
    for e in (EjecucionProgramacion.query.filter(EjecucionProgramacion.fecha >= inicio)
              .order_by(EjecucionProgramacion.fecha).all()):
        por_prog.setdefault((e.canal, e.programacion_id), []).append(e.a_dict())
    primera = db.session.query(db.func.min(EjecucionProgramacion.fecha)).scalar()
    return por_prog, primera or _ahora_colombia()


def _con_turnos(fila, horas, ejecuciones, ahora, desde):
    """Agrega a la fila de Inicio sus turnos de hoy y los mensajes enviados en todos ellos."""
    from services.sms_horarios import turnos_del_dia
    fila["turnos"] = turnos_del_dia(horas, ejecuciones, ahora, desde)
    if ejecuciones:
        fila["mensajes"] = sum(e.get("enviados") or 0 for e in ejecuciones)
    return fila


def _programaciones_sms_de_hoy(hoy, ejecuciones_hoy, desde_registro):
    """
    Programaciones SMS (SQLite) que ya corrieron hoy, que tocan hoy o que
    quedaron con error. estado_hoy: corrio / pendiente / error. turnos: cada
    turno del día (recurrente cada 2 h → uno por turno) con lo que pasó.
    """
    from database import ProgramacionSms
    from services.sms_horarios import horas_del_dia
    ahora = _ahora_colombia()
    filas = []
    for prog in ProgramacionSms.query.filter(ProgramacionSms.estado != "cancelado").all():
        tipo = (prog.tipo_programacion or "simple").lower()
        ejecuciones = ejecuciones_hoy.get(("sms", str(prog.id)), [])
        # fecha_ejecucion se guarda en UTC; fecha_programada en hora de Colombia.
        ejecucion = prog.fecha_ejecucion and pytz.utc.localize(prog.fecha_ejecucion).astimezone(COLOMBIA_TZ)
        corrio = bool(ejecucion) and ejecucion.date() == hoy
        horas = []
        if tipo == "recurrente":
            hora = prog.hora_inicio or ""
            if prog.intervalo_minutos and prog.hora_fin:
                hora = f"{hora} a {prog.hora_fin}, cada {_texto_intervalo(prog.intervalo_minutos)}"
            toca = prog.estado == "pendiente" and not (prog.fecha_fin and prog.fecha_fin < hoy.isoformat())
            try:
                horas = horas_del_dia(prog.hora_inicio or "08:00", prog.intervalo_minutos, prog.hora_fin)
            except ValueError:
                pass
        else:
            hora = prog.fecha_programada.strftime("%H:%M") if prog.fecha_programada else ""
            toca = prog.estado == "pendiente" and bool(prog.fecha_programada) and prog.fecha_programada.date() == hoy
            if prog.fecha_programada and prog.fecha_programada.date() == hoy:
                horas = [hora]
        if prog.estado in ("error", "fallido"):
            estado_hoy = "error"
        elif ejecuciones and ejecuciones[-1].get("resultado") == "error":
            estado_hoy = "error"  # el último turno de hoy falló (la recurrente sigue con el próximo)
        elif corrio or ejecuciones:
            estado_hoy = "corrio"
        elif toca:
            estado_hoy = "pendiente"
        else:
            continue
        # Turnos de antes de crearla o de cuando no había registro no cuentan como perdidos.
        creada = prog.fecha_creacion and pytz.utc.localize(prog.fecha_creacion).astimezone(COLOMBIA_TZ).replace(tzinfo=None)
        desde = max(d for d in (creada, desde_registro) if d)
        if prog.estado not in ("pendiente", "error", "fallido") and tipo == "recurrente":
            horas = []  # completada: ya no tiene turnos
        filas.append(_con_turnos({
            "canal": "sms", "id": prog.id, "campana": prog.campana, "tipo": tipo, "hora": hora,
            "estado_hoy": estado_hoy, "corrio_a_las": ejecucion.strftime("%H:%M") if corrio else "",
            "mensajes": prog.ultimo_enviados if corrio else None, "error": prog.ultimo_error or "",
        }, horas, ejecuciones, ahora, desde))
    return filas


def _programaciones_email_de_hoy(hoy, ejecuciones_hoy, desde_registro):
    """
    Programaciones de email (SQLite) que corrieron hoy, tocan hoy o tienen
    error. Fechas en hora de Colombia. estado_hoy: corrio / pendiente / error.
    """
    from database import ProgramacionEmail
    from services.sms_horarios import horas_del_dia
    ahora = _ahora_colombia()
    filas = []
    for prog in ProgramacionEmail.query.filter(ProgramacionEmail.estado != "cancelado").all():
        ejecuciones = ejecuciones_hoy.get(("email", prog.id), [])
        corrio = bool(prog.fecha_ejecucion) and prog.fecha_ejecucion.date() == hoy
        recurrente = prog.tipo == "recurrente"
        if recurrente:
            toca = prog.estado in ("pendiente", "enviando") and not (prog.fecha_fin and prog.fecha_fin < hoy.isoformat())
        else:
            toca = prog.estado in ("pendiente", "enviando") and prog.fecha_programada.date() == hoy
        if prog.estado == "error":
            estado_hoy = "error"
        elif ejecuciones and ejecuciones[-1].get("resultado") == "error":
            estado_hoy = "error"  # el último turno de hoy falló (la recurrente sigue con el próximo)
        elif corrio or ejecuciones:
            estado_hoy = "corrio"
        elif toca:
            estado_hoy = "pendiente"
        else:
            continue
        # Recurrente como SMS: una vez al día o cada intervalo hasta hora_fin.
        horas = []
        if recurrente:
            hora = prog.hora_recurrente() or ""
            if prog.intervalo_minutos and prog.hora_fin:
                hora = f"{hora} a {prog.hora_fin}, cada {_texto_intervalo(prog.intervalo_minutos)}"
            if prog.estado in ("pendiente", "enviando", "error"):
                try:
                    horas = horas_del_dia(prog.hora_recurrente() or "08:00", prog.intervalo_minutos, prog.hora_fin)
                except ValueError:
                    pass
        else:
            hora = prog.fecha_programada.strftime("%H:%M")
            horas = [hora] if prog.fecha_programada.date() == hoy else []
        # En la recurrente, fecha_programada es desde cuándo corre (creada o editada).
        desde = max(d for d in (prog.fecha_creacion, desde_registro, prog.fecha_programada if recurrente else None) if d)
        filas.append(_con_turnos({
            "canal": "email", "id": prog.id, "campana": prog.campana or "", "tipo": prog.tipo or "simple",
            "hora": hora, "estado_hoy": estado_hoy,
            "corrio_a_las": prog.fecha_ejecucion.strftime("%H:%M") if corrio else "",
            "mensajes": prog.ultimo_enviados if corrio else None, "error": prog.ultimo_error or "",
        }, horas, ejecuciones, ahora, desde))
    return filas


@app.route("/api/inicio/resumen")
def inicio_resumen():
    """
    Lo enviado hoy (hora de Colombia): total de SMS y emails (SmsLog / EmailLog),
    mensajes por campaña y las programaciones de hoy con su estado. Si BigQuery
    falla responde igual con lo de SQLite y el error en 'avisos'.
    """
    from bigquery import mensaje_error_bigquery
    hoy = datetime.now(COLOMBIA_TZ).date()
    avisos = []
    totales = {"sms": None, "email": None}
    campanas = []
    ejecuciones_hoy, desde_registro = _ejecuciones_de_hoy(hoy)
    programaciones = (_programaciones_sms_de_hoy(hoy, ejecuciones_hoy, desde_registro)
                      + _programaciones_email_de_hoy(hoy, ejecuciones_hoy, desde_registro))

    if _asegurar_bq():
        for canal, tabla in (("sms", SMS_LOG_TABLE), ("email", EMAIL_LOG_TABLE)):
            try:
                filas = list(bq_client.query(f"""
                    SELECT campana, COUNT(*) AS mensajes, MAX(fecha_envio) AS ultimo
                    FROM `{tabla}`
                    WHERE DATE(fecha_envio, 'America/Bogota') = CURRENT_DATE('America/Bogota')
                    GROUP BY campana ORDER BY ultimo DESC
                """).result())
            except Exception as exc:
                avisos.append(f"{'SMS' if canal == 'sms' else 'Emails'} enviados hoy: {mensaje_error_bigquery(exc)}")
                continue
            totales[canal] = sum(f["mensajes"] for f in filas)
            for f in filas:
                campanas.append({"canal": canal, "campana": f["campana"] or "", "mensajes": f["mensajes"],
                                 "ultimo": _hora_colombia(f["ultimo"])})
    else:
        avisos.append("No se pudo conectar a BigQuery: faltan los totales de hoy.")

    return jsonify({"success": True, "fecha": hoy.isoformat(), "totales": totales,
                    "campanas": campanas, "programaciones": programaciones, "avisos": avisos})



def _saldo_infobip():
    """(saldo, moneda) de la cuenta de Infobip (GET /account/1/balance)."""
    import requests
    infobip = load_config().get("infobip", {})
    if not infobip.get("api_key") or not infobip.get("base_url"):
        raise ValueError("faltan infobip.api_key o infobip.base_url en config.json")
    resp = requests.get(f"{infobip['base_url'].rstrip('/')}/account/1/balance",
                        headers={"Authorization": f"App {infobip['api_key']}", "Accept": "application/json"},
                        timeout=20)
    resp.raise_for_status()
    datos = resp.json()
    return float(datos.get("balance") or 0), datos.get("currency") or "COP"


@app.route("/api/inicio/saldo")
def inicio_saldo():
    """
    Saldo de Infobip y lo consumido hoy y en el mes: costo de SMS (SmsInfobip_Valoro,
    precio × partes, como el dashboard) y emails enviados (EmailLog). La
    plataforma de email no da el saldo por API: de email solo va lo enviado.
    Cada parte que falle va en 'avisos' y el resto responde igual.
    """
    from bigquery import mensaje_error_bigquery
    from infobip_sync import COSTO_SQL, TABLA as TABLA_SMS_INFOBIP
    avisos = []
    sms = {"saldo": None, "moneda": "COP", "costo_hoy": None, "costo_mes": None,
           "sms_hoy": None, "sms_mes": None, "alcanza_dias": None}
    email = {"enviados_hoy": None, "enviados_mes": None}
    try:
        sms["saldo"], sms["moneda"] = _saldo_infobip()
    except Exception as exc:
        logger_sms.warning(f"⚠️ No se pudo leer el saldo de Infobip: {exc}")
        avisos.append(f"Saldo de Infobip: {mensaje_error(exc, 'Infobip (SMS)')}")

    if _asegurar_bq():
        try:
            fila = list(bq_client.query(f"""
                SELECT
                  COUNTIF(DATE(sent_at, 'America/Bogota') = CURRENT_DATE('America/Bogota')) AS sms_hoy,
                  COUNT(*) AS sms_mes,
                  SUM(IF(DATE(sent_at, 'America/Bogota') = CURRENT_DATE('America/Bogota'), {COSTO_SQL}, 0)) AS costo_hoy,
                  SUM({COSTO_SQL}) AS costo_mes,
                  COUNT(DISTINCT DATE(sent_at, 'America/Bogota')) AS dias
                FROM `{TABLA_SMS_INFOBIP}`
                WHERE sent_at >= TIMESTAMP(DATE_TRUNC(CURRENT_DATE('America/Bogota'), MONTH), 'America/Bogota')
            """).result())[0]
            sms.update({"sms_hoy": fila["sms_hoy"], "sms_mes": fila["sms_mes"],
                        "costo_hoy": round(fila["costo_hoy"] or 0), "costo_mes": round(fila["costo_mes"] or 0)})
            # Días que alcanza el saldo al consumo promedio por día del mes.
            if sms["saldo"] is not None and fila["dias"] and fila["costo_mes"]:
                sms["alcanza_dias"] = int(sms["saldo"] / (fila["costo_mes"] / fila["dias"]))
        except Exception as exc:
            avisos.append(f"Consumo de SMS: {mensaje_error_bigquery(exc)}")
        try:
            fila = list(bq_client.query(f"""
                SELECT
                  COUNTIF(DATE(fecha_envio, 'America/Bogota') = CURRENT_DATE('America/Bogota')) AS hoy,
                  COUNT(*) AS mes
                FROM `{EMAIL_LOG_TABLE}`
                WHERE fecha_envio >= TIMESTAMP(DATE_TRUNC(CURRENT_DATE('America/Bogota'), MONTH), 'America/Bogota')
            """).result())[0]
            email.update({"enviados_hoy": fila["hoy"], "enviados_mes": fila["mes"]})
        except Exception as exc:
            avisos.append(f"Emails enviados: {mensaje_error_bigquery(exc)}")
    else:
        avisos.append("No se pudo conectar a BigQuery: falta lo consumido.")
    return jsonify({"success": True, "sms": sms, "email": email, "avisos": avisos})


# ==================== CONFIGURACIÓN GENERAL ====================

@app.route('/config-general')
def config_general():
    result = load_general_parameters()
    parameters = result.get("parameters", {})
    limits = result.get("limits", {})
    error = None if result.get("success") else result.get("message")
    return render_template("config_general.html", parameters=parameters, limits=limits, error=error)


@app.route('/config-general/save', methods=['POST'])
def config_general_save():
    data = request.get_json() or {}
    result = save_general_parameters(data)
    if not result.get("success"):
        return jsonify(result), 400
    load_config()
    console_interval = reschedule_console_message_job(result["parameters"]["console_message_interval_seconds"])
    result["console_message_interval_seconds"] = console_interval
    result["logs_deleted"] = cleanup_old_log_files()
    return jsonify(result)


# ==================== FUNCIONES AUXILIARES GENERALES ====================


def _get_sms_rows(query):
    """Ejecuta consulta BigQuery y devuelve filas."""
    global bq_client
    if bq_client is None:
        init_bigquery()
    if bq_client is None:
        return None, (jsonify({"success": False, "message": "No se pudo inicializar BigQuery."}), 500)
    result = fetch_sms_query_rows(bq_client, query)
    if not result.get("success"):
        return None, (jsonify(result), 400)
    return result["rows"], None


# Bases SMS subidas desde Excel (issue #39): excel_id → filas, 2 horas.
_SMS_EXCEL_CACHE = {}


def _sms_excel_del_cache(excel_id):
    """Entrada del cache del Excel SMS o None si no existe o expiró."""
    ahora = datetime.now(COLOMBIA_TZ)
    for clave in [k for k, v in _SMS_EXCEL_CACHE.items() if v["expires"] < ahora]:
        _SMS_EXCEL_CACHE.pop(clave, None)
    return _SMS_EXCEL_CACHE.get(excel_id)


def _sms_filas_de_peticion(data):
    """
    Filas de la base SMS según el origen elegido en pantalla: el Excel
    subido (excel_id) o la consulta. Devuelve (filas, origen, error_response);
    origen es la consulta o "Excel: <archivo>" (queda en SmsLog y en la
    programación). El link de WhatsApp de pantalla (cambia cada día) va en
    las filas con Id Mensaje WS que no traen Link.
    """
    from services.sms_excel import aplicar_link_whatsapp, aplicar_link_mensaje

    link_whatsapp = (data.get("link_whatsapp") or "").strip()
    id_mensaje = (data.get("id_mensaje") or "").strip()
    excel_id = (data.get("excel_id") or "").strip()
    if excel_id:
        cache = _sms_excel_del_cache(excel_id)
        if not cache:
            return None, "", (jsonify({"success": False,
                                       "message": "El Excel no está cargado o expiró. Vuelve a subirlo."}), 400)
        campana = (data.get("campana") or "").strip()
        filas = []
        for fila in cache["filas"]:
            fila = dict(fila)
            # Nombre Campaña es obligatorio: si el Excel no lo trae, el de pantalla.
            if campana and not fila.get("Nombre Campaña"):
                fila["Nombre Campaña"] = campana
            filas.append(fila)
        aplicar_link_whatsapp(filas, link_whatsapp, id_mensaje)
        aplicar_link_mensaje(filas, data.get("link"))
        return filas, f"Excel: {cache['archivo']}", None

    query = (data.get("query") or "").strip()
    rows, error_response = _get_sms_rows(query)
    if rows:
        aplicar_link_whatsapp(rows, link_whatsapp, id_mensaje)
        aplicar_link_mensaje(rows, data.get("link"))
    return rows, query, error_response


@app.route("/api/sms/excel", methods=["POST"])
def sms_subir_excel():
    """Sube el Excel de la base SMS, completa desde el intradía y lo deja en cache."""
    from services.sms_excel import excel_a_filas_sms
    import uuid

    archivo = request.files.get("file")
    if not archivo or not archivo.filename:
        return jsonify({"success": False, "message": "No se envió archivo."}), 400
    if not archivo.filename.lower().endswith((".xlsx", ".xls")):
        return jsonify({"success": False, "message": "Debe ser .xlsx o .xls"}), 400

    if bq_client is None:
        init_bigquery()

    try:
        filas, resumen = excel_a_filas_sms(archivo, bq_client)
    except ValueError as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        logger_sms.exception("Error procesando Excel SMS")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500

    excel_id = str(uuid.uuid4())
    _SMS_EXCEL_CACHE[excel_id] = {
        "filas": filas,
        "archivo": archivo.filename,
        "expires": datetime.now(COLOMBIA_TZ) + timedelta(hours=2),
    }
    logger_sms.info(f"📄 Excel SMS '{archivo.filename}': {resumen}")
    return jsonify({
        "success": True,
        "excel_id": excel_id,
        "archivo": archivo.filename,
        "columnas": list(filas[0].keys()) if filas else [],
        **resumen,
    })


@app.route("/api/sms/excel/plantilla", methods=["GET"])
def sms_plantilla_excel():
    """Excel vacío con las columnas de la base SMS."""
    from services.sms_excel import plantilla_excel_sms

    return send_file(io.BytesIO(plantilla_excel_sms()), as_attachment=True,
                     download_name="plantilla_base_sms.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")



def init_bigquery():
    """Inicializar conexión BigQuery."""
    global bq_client
    try:
        bq_client = get_bigquery_client()
        logger.info("Conexion a BigQuery establecida exitosamente")
    except Exception as e:
        logger.error(f"Error al conectar con BigQuery: {e}")
        bq_client = None


def _asegurar_bq():
    """Inicializa BigQuery si hace falta; True si hay cliente."""
    global bq_client
    if bq_client is None:
        init_bigquery()
    return bq_client is not None


# ==================== SMS - CAMPOS OBLIGATORIOS ====================

def _faltantes_sms(data, *, con_nombre=True, hora=None):
    """
    Nombres de los campos obligatorios que faltan en la petición SMS.
    hora: "fecha_programada" (programación simple) o "hora_inicio" (recurrente).
    """
    faltan = []
    if con_nombre and not (data.get("campana") or "").strip():
        faltan.append("nombre de la campaña")
    if not ((data.get("query") or "").strip() or data.get("excel_id")):
        faltan.append("consulta SQL (o Excel)")
    if not (data.get("plantilla") or "").strip():
        faltan.append("mensaje")
    if hora == "fecha_programada" and not (data.get("fecha_programada") or "").strip():
        faltan.append("fecha y hora de envío")
    if hora == "hora_inicio" and not (data.get("hora_inicio") or "").strip():
        faltan.append("hora de envío")
    return faltan


def _respuesta_faltantes(faltan):
    """Respuesta 400 que nombra los campos que faltan."""
    return jsonify({"success": False, "faltantes": faltan,
                    "message": f"Faltan datos obligatorios: {', '.join(faltan)}."}), 400


#================= SMS - PÁGINA ====================


@app.route("/config-sms")
def config_sms():
    """Enviar SMS ya. Las programaciones usan la misma pantalla (sms_programaciones_pagina)."""
    return render_template("config_sms.html", modo="enviar")



@app.route("/api/sms/preview", methods=["POST"])
def sms_preview():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()

    faltan = _faltantes_sms(data, con_nombre=False)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        rows, _, error_response = _sms_filas_de_peticion(data)
        if error_response:
            return error_response
        result = preview_sms(rows, plantilla, link=(data.get("link") or "").strip())
        if not result.get("success"):
            return jsonify(result), 400
        return jsonify({"success": True, **result})
    except Exception as exc:
        logger_sms.exception("❌ Error en la vista previa de SMS")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


# ==================== SMS - ENVIAR ====================

@app.route("/api/sms/enviar", methods=["POST"])
def sms_send():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    campaign = (data.get("campana") or "").strip()
    # Quien envía/programa = el usuario con sesión (registro de acciones y logs); el campo del formulario queda de respaldo.
    usuario = usuario_sesion() or (data.get("usuario") or "").strip()
    allow_resend = bool(data.get("confirmar_reenvio", False))

    faltan = _faltantes_sms(data)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        rows, origen, error_response = _sms_filas_de_peticion(data)
        if error_response:
            return error_response
        infobip_config = load_config().get("infobip", {})
        nombre = f"SMS '{campaign}' (envío de {usuario or 'sin usuario'})"
        result = enviar_sms_desde_filas(
            rows, plantilla, infobip_config,
            client=bq_client, campaign=campaign, usuario=usuario,
            query_sql=origen, allow_resend=allow_resend,
            id_mensaje=(data.get("id_mensaje") or "").strip(),
            id_campana=(data.get("id_campana") or "").strip(),
            link=(data.get("link") or "").strip(),
            progreso=lambda texto: log_activity(f"📤 {nombre}: {texto}"),
        )
        registrar_resumen_sms(campaign, usuario, origen, plantilla,
                             result["total_preparados"], result["enviados"],
                             result["fallidos"], "exitoso" if result["fallidos"] == 0 else "parcial")
        if result.get("sin_destinatarios"):
            log_activity(f"⏭️ {nombre}: {resumen_filtros_sms(len(rows), result.get('details') or {})}. "
                         f"{result.get('mensaje')}")
        elif result["fallidos"]:
            log_activity(f"⚠️ {nombre}: {result.get('message')}", "WARNING")
        else:
            log_activity(f"✅ {nombre}: {result.get('message')}")
        return jsonify({"success": result["fallidos"] == 0, **result})
    except SmsServiceError as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        log_activity(f"❌ SMS '{campaign}': NO SE ENVIÓ. {mensaje_error(exc)}", "ERROR")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        logger_sms.exception(f"❌ Error enviando SMS de la campaña '{campaign}'")
        log_activity(f"❌ SMS '{campaign}': NO SE ENVIÓ. {mensaje_error(exc, 'Infobip (SMS)')}", "ERROR")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


# ==================== SMS - VALIDAR ====================

@app.route("/api/sms/validar", methods=["POST"])
def sms_validar():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    allow_resend = bool(data.get("confirmar_reenvio", False))

    faltan = _faltantes_sms(data, con_nombre=False)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        rows, _, error_response = _sms_filas_de_peticion(data)
        if error_response:
            return error_response
        link = (data.get("link") or "").strip()
        preview = preview_sms(rows, plantilla, link=link)
        if not preview.get("success"):
            return jsonify(preview), 400

        from services.sms_service import preparar_sms, verificar_lista_negra, verificar_duplicados, filtrar_permitidos
        prepared, details = preparar_sms(rows, plantilla, link)
        if not _asegurar_bq():
            return jsonify({"success": False, "message": "No se pudo conectar a BigQuery para revisar la lista negra."}), 500
        phones = [item["phone"] for item in prepared]
        blocked = verificar_lista_negra(bq_client, phones)
        duplicates = verificar_duplicados(bq_client, phones)

        # Los señuelos nunca se excluyen (ni por lista negra ni por duplicados).
        allowed, n_bloqueados, n_duplicados = filtrar_permitidos(prepared, blocked, duplicates, allow_resend)

        from services.sms_service import items_vista_previa
        preview_mensajes = items_vista_previa(allowed, 3)

        return jsonify({
            "success": True,
            "total_consulta": len(rows),
            "total_validos": details.get("total_validos", 0),
            "invalidos": details.get("invalid_numbers", 0),
            "duplicados": n_duplicados,
            "blacklist": n_bloqueados,
            "excluidos_reglas": details.get("excluidos_reglas", {}),
            # Sin clientes no sale nada (ni a los señuelos): 0 a enviar.
            "a_enviar": len(allowed) if any(not i.get("senuelo") for i in allowed) else 0,
            "preview": preview_mensajes
        })
    except Exception as exc:
        logger_sms.exception("❌ Error validando la base de SMS")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500
    

def mensaje_error(exc, servicio=None):
    """
    Mensaje claro para la pantalla (services/errores.py): sin conexión, límite de
    la API, sin saldo, clave rechazada, error de la consulta… El detalle completo
    (traceback, URL, respuesta de la API) va al log en cada except.
    """
    from bigquery import mensaje_error_bigquery
    from services.errores import explicar_error
    return explicar_error(exc, servicio=servicio, bigquery=mensaje_error_bigquery)


def registrar_ejecucion(canal, prog_id, campana, turno, resultado, *, filas=None, enviados=None,
                        excluidos=None, mensaje=None):
    """
    Guarda una ejecución (turno) de una programación en ejecuciones_programacion:
    resultado enviado / sin_datos / error. Inicio la muestra por turno. Un error
    aquí no interrumpe el envío.
    """
    from database import EjecucionProgramacion
    try:
        if turno is not None and turno.tzinfo is not None:
            turno = turno.astimezone(COLOMBIA_TZ).replace(tzinfo=None)
        db.session.add(EjecucionProgramacion(
            canal=canal, programacion_id=str(prog_id), campana=(campana or "")[:255], turno=turno,
            fecha=_ahora_colombia(), resultado=resultado, filas_consulta=filas, enviados=enviados,
            excluidos=excluidos, mensaje=(str(mensaje)[:1000] if mensaje else None),
        ))
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(f"No se pudo registrar la ejecución de la programación {canal} {prog_id}")


def registrar_resumen_sms(campaign, usuario, query, plantilla, total, enviados, fallidos, estado):
    """Registra un resumen del envío en execution_log.txt (no interrumpe el flujo; la consola lleva su línea clara)."""
    try:
        execution_logger.info(
            f"[SMS] Campaña='{campaign}' Usuario='{usuario}' "
            f"Total={total} Enviados={enviados} Fallidos={fallidos} Estado={estado}"
        )
    except Exception:
        pass
# ==================== SCHEDULER SMS ====================

def _sms_job_id(prog):
    """ID único del job para una programación."""
    return f"sms_{prog.tipo_programacion}_{prog.id}"


def _sms_now():
    return datetime.now(COLOMBIA_TZ)


def execute_sms_schedule(schedule_id: str):
    """
    Ejecuta UNA programación SMS específica.
    - Simple: se ejecuta una vez y queda como 'enviado'.
    - Recurrente: valida hora/fecha_fin/ya-ejecutado-hoy antes de enviar.
    """
    with app.app_context():
        from database import db, ProgramacionSms

        prog = db.session.get(ProgramacionSms, int(schedule_id))
        if not prog:
            logger_sms.warning(f"⚠️ Programación SMS {schedule_id} no existe")
            return

        if prog.estado != 'pendiente':
            logger_sms.info(f"⏭️ SMS #{prog.id}: estado={prog.estado}, se omite")
            return

        tipo = (prog.tipo_programacion or 'simple').lower()
        ahora = _sms_now()
        hoy = ahora.date()
        # Hora que le toca (para el registro de ejecuciones): la programada, o el turno de la recurrente.
        turno = prog.fecha_programada

        # ---------- VALIDACIONES SOLO PARA RECURRENTE ----------
        if tipo == 'recurrente':
            # 1) ¿Ya pasó fecha_fin?
            if prog.fecha_fin:
                try:
                    ffin = datetime.strptime(prog.fecha_fin, "%Y-%m-%d").date()
                    if hoy > ffin:
                        logger_sms.info(f"🛑 Recurrente SMS #{prog.id}: pasó fecha_fin, completando")
                        prog.estado = 'completado'
                        prog.fecha_actualizacion = datetime.utcnow()
                        db.session.commit()
                        _sms_desagendar(prog)
                        return
                except Exception:
                    pass

            # 2) ¿Toca un turno? Una vez al día a hora_inicio, o cada
            #    intervalo_minutos hasta hora_fin (services/sms_horarios.py).
            from services.sms_horarios import turno_a_enviar
            ultima = prog.fecha_ejecucion
            if ultima is not None and ultima.tzinfo is None:
                ultima = pytz.utc.localize(ultima).astimezone(COLOMBIA_TZ)
            # Un turno que ya se intentó (aunque haya fallado) no se repite: sigue el próximo.
            if prog.ultimo_intento is not None:
                intento = COLOMBIA_TZ.localize(prog.ultimo_intento)
                if ultima is None or intento > ultima:
                    ultima = intento
            try:
                turno = turno_a_enviar(ahora, ultima, prog.hora_inicio or '08:00',
                                       prog.intervalo_minutos, prog.hora_fin)
            except ValueError:
                logger_sms.warning(f"⚠️ Recurrente SMS #{prog.id}: horario inválido ({prog.hora_inicio})")
                return
            if turno is None:
                return
            logger_sms.info(f"⏰ Recurrente SMS #{prog.id}: turno de las {turno:%H:%M}")

        # ---------- EJECUTAR ----------
        # Consola y Actividad en vivo: una línea clara por paso (el detalle va a logs/sms.log).
        nombre = f"SMS '{prog.campana or prog.id}'"
        if not turno:
            etiqueta = nombre
        else:
            etiqueta = f"{nombre} · {'turno' if tipo == 'recurrente' else 'programado'} {turno:%H:%M}"
        filas = None
        if tipo == 'recurrente':
            # Se marca antes de enviar: si falla a mitad, este turno no se repite (duplicaría lo que salió).
            prog.ultimo_intento = turno.astimezone(COLOMBIA_TZ).replace(tzinfo=None)
            db.session.commit()
        try:
            logger_sms.info(f"📤 Ejecutando SMS {tipo.upper()} #{prog.id} - {prog.campana}")

            config = load_config()
            infobip_config = config.get("infobip", {})

            if prog.filas_excel:
                log_activity(f"⏰ {etiqueta}: empieza (base de Excel)…")
                filas = json.loads(prog.filas_excel)
                if prog.link:
                    from services.sms_excel import aplicar_link_mensaje
                    aplicar_link_mensaje(filas, prog.link)
            else:
                log_activity(f"⏰ {etiqueta}: consultando BigQuery…")
                if not _asegurar_bq():
                    raise SmsServiceError("No se pudo conectar a BigQuery.")
                fetched = fetch_sms_query_rows(bq_client, prog.consulta_sql)
                if not fetched.get("success"):
                    raise SmsServiceError(fetched.get("message", "Error en consulta"))
                filas = fetched["rows"]
                if prog.link_whatsapp:
                    from services.sms_excel import aplicar_link_whatsapp
                    aplicar_link_whatsapp(filas, prog.link_whatsapp)
                if prog.link:
                    from services.sms_excel import aplicar_link_mensaje
                    aplicar_link_mensaje(filas, prog.link)

            resultado = enviar_sms_desde_filas(
                filas,
                prog.plantilla,
                infobip_config,
                client=bq_client,
                campaign=prog.campana or "",
                usuario=prog.usuario or "sistema",
                query_sql=prog.consulta_sql,
                allow_resend=bool(prog.confirmar_reenvio),
                id_mensaje=prog.id_mensaje or "",
                id_campana=prog.id_campana or "",
                link=prog.link or "",
                progreso=lambda texto: log_activity(f"📤 {nombre}: {texto}"),
            )
            if resultado.get("enviados", 0) == 0 and resultado.get("fallidos", 0) > 0:
                # Ningún SMS salió (sin saldo, sin conexión, clave…): es un error, no un envío.
                from services.errores import marcar_claro
                raise marcar_claro(SmsServiceError(resultado.get("message") or "Ningún SMS salió."))

            if tipo == 'simple':
                prog.estado = 'enviado'

            prog.fecha_ejecucion = datetime.utcnow()
            prog.total_destinatarios = resultado.get("total_preparados", 0)
            prog.ultimo_enviados = resultado.get("enviados", 0)
            prog.ultimo_error = None
            prog.fecha_actualizacion = datetime.utcnow()
            db.session.commit()

            logger_sms.info(f"✅ SMS {tipo.capitalize()} enviado: #{prog.id}")
            detalles = resultado.get("details") or {}
            excluidos = (detalles.get("excluidos_lista_negra") or 0) + (detalles.get("duplicados_hoy") or 0)
            if resultado.get("sin_destinatarios"):
                registrar_ejecucion("sms", prog.id, prog.campana, turno, "sin_datos", filas=len(filas),
                                    enviados=0, excluidos=excluidos, mensaje=resultado.get("mensaje"))
                log_activity(f"⏭️ {etiqueta}: {resumen_filtros_sms(len(filas), detalles)}. {resultado.get('mensaje')}")
            else:
                registrar_ejecucion("sms", prog.id, prog.campana, turno, "enviado", filas=len(filas),
                                    enviados=resultado.get("enviados", 0), excluidos=excluidos,
                                    mensaje=resultado.get("message") if resultado.get("fallidos") else None)
                if resultado.get("fallidos"):
                    log_activity(f"⚠️ {etiqueta}: {resultado.get('message')}", "WARNING")
                else:
                    log_activity(f"✅ {etiqueta}: {resultado.get('message')}")
            registro = resultado.get("registro_smslog") or {}
            if registro.get("errores"):
                log_activity(f"⚠️ SMS programado #{prog.id}: solo {registro.get('guardados', 0)} de "
                             f"{registro.get('total', 0)} SMS quedaron en SmsLog ({registro['errores'][0]})", "WARNING")
            log_gui_action("SMS programado ejecutado", programacion=str(prog.id))

        except Exception as exc:
            # Pantalla: mensaje claro (ultimo_error, Inicio, actividad). Log: el detalle completo.
            mensaje = mensaje_error(exc, "Infobip (SMS)")
            logger_sms.exception(f"❌ Error ejecutando la programación SMS #{prog.id} '{prog.campana}': {exc}")
            try:
                db.session.rollback()
                prog.ultimo_error = mensaje[:1000]
                prog.fecha_actualizacion = datetime.utcnow()
                if tipo != 'recurrente':
                    prog.estado = 'error'
                db.session.commit()
                if tipo != 'recurrente':
                    _sms_desagendar(prog)
            except Exception:
                logger_sms.exception(f"No se pudo guardar el error de la programación SMS #{prog.id}")
            if tipo == 'recurrente':
                # La recurrente sigue: este turno queda con error y el próximo sale normal.
                log_activity(f"❌ {etiqueta}: NO SE ENVIÓ. {mensaje} Sigue en el próximo turno.", "ERROR")
            else:
                log_activity(f"❌ {etiqueta}: NO SE ENVIÓ. {mensaje}", "ERROR")
            registrar_ejecucion("sms", prog.id, prog.campana, turno, "error",
                                filas=len(filas) if filas is not None else None, mensaje=mensaje)




def agendar_programacion_sms(prog):
    """Registra el job correcto en el scheduler según el tipo."""
    job_id = _sms_job_id(prog)

    # Quita el job anterior (también el del otro tipo, si cambió simple ↔ recurrente).
    _sms_desagendar(prog)

    if prog.estado != 'pendiente':
        return

    tipo = (prog.tipo_programacion or 'simple').lower()

    # ---------- SIMPLE: DateTrigger a la hora exacta ----------
    if tipo == 'simple':
        if not prog.fecha_programada:
            logger_sms.warning(f"⚠️ SMS simple #{prog.id}: sin fecha_programada")
            return

        fecha = prog.fecha_programada
        if fecha.tzinfo is None:
            fecha = COLOMBIA_TZ.localize(fecha)
        else:
            fecha = fecha.astimezone(COLOMBIA_TZ)

        ahora = _sms_now()
        run_date = fecha if fecha > ahora else ahora + timedelta(seconds=5)

        scheduler.add_job(
            execute_sms_schedule,
            trigger=DateTrigger(run_date=run_date),
            args=[str(prog.id)],
            id=job_id,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        logger_sms.info(f"🗓️ SMS simple #{prog.id} agendado para {run_date}")

    # ---------- RECURRENTE: revisa cada 10 min (cada minuto si se repite en el día) ----------
    elif tipo == 'recurrente':
        revisar_cada = 1 if prog.intervalo_minutos else 10
        scheduler.add_job(
            execute_sms_schedule,
            trigger=IntervalTrigger(minutes=revisar_cada),
            args=[str(prog.id)],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger_sms.info(f"🔄 SMS recurrente #{prog.id} agendado (revisa cada {revisar_cada} min)")


def _sms_desagendar(prog):
    """Quita del scheduler los jobs de la programación (simple y recurrente)."""
    for job_id in (f"sms_simple_{prog.id}", f"sms_recurrente_{prog.id}"):
        try:
            scheduler.remove_job(job_id)
            logger_sms.info(f"🗑️ Job SMS removido: {job_id}")
        except Exception:
            pass


def cargar_programaciones_sms_al_iniciar():
    """
    Al arrancar, re-agenda todos los jobs SMS pendientes (los jobs se pierden al
    reiniciar). En la copia de prueba no se recargan.
    """
    from database import ProgramacionSms
    from modo_prueba import es_copia_prueba
    if es_copia_prueba():
        logger_sms.warning("🧪 Copia de prueba: no se recargan las programaciones SMS.")
        return
    try:
        pendientes = ProgramacionSms.query.filter_by(estado='pendiente').all()
        logger_sms.info(f"🔁 Re-agendando {len(pendientes)} programaciones SMS pendientes")
        for prog in pendientes:
            try:
                agendar_programacion_sms(prog)
            except Exception as e:
                logger_sms.exception(f"Error agendando SMS #{prog.id}: {e}")
    except Exception as e:
        logger_sms.exception(f"Error cargando programaciones SMS: {e}")


# ==================== API SMS - PROGRAMAR ====================

def _leer_repeticion(data, hora_inicio):
    """
    (intervalo_minutos, hora_fin, error) de una recurrente. Sin intervalo sale
    una vez al día; con intervalo se repite hasta hora_fin.
    """
    from services.sms_horarios import validar_horario
    try:
        intervalo = int(data.get("intervalo_minutos") or 0) or None
    except (TypeError, ValueError):
        return None, None, "Intervalo inválido."
    hora_fin = ((data.get("hora_fin") or "").strip() or None) if intervalo else None
    return intervalo, hora_fin, validar_horario(hora_inicio, intervalo, hora_fin)


def _texto_horario(hora_inicio, intervalo, hora_fin):
    """'todos los días a las 08:00' o 'todos los días a las 08:00, 10:00, ...'."""
    from services.sms_horarios import horas_del_dia
    if not intervalo:
        return f"todos los días a las {hora_inicio}"
    return f"todos los días a las {', '.join(horas_del_dia(hora_inicio, intervalo, hora_fin))}"


@app.route("/api/sms/programar", methods=["POST"])
def sms_programar_simple():
    """Programa un envío SMS UNA sola vez a una fecha/hora específica."""
    from services.sms_service import (
        guardar_programacion, aplicar_validaciones, SmsServiceError
    )

    global bq_client
    if bq_client is None:
        init_bigquery()

    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    campana = (data.get("campana") or "").strip()
    # Quien envía/programa = el usuario con sesión (registro de acciones y logs); el campo del formulario queda de respaldo.
    usuario = usuario_sesion() or (data.get("usuario") or "").strip()
    allow_resend = bool(data.get("confirmar_reenvio", False))
    fecha_programada = (data.get("fecha_programada") or "").strip()

    faltan = _faltantes_sms(data, hora="fecha_programada")
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        run_date = datetime.fromisoformat(fecha_programada)
        if run_date.tzinfo is None:
            run_date = COLOMBIA_TZ.localize(run_date)
        else:
            run_date = run_date.astimezone(COLOMBIA_TZ)

        if run_date <= datetime.now(COLOMBIA_TZ):
            raise SmsServiceError("La fecha programada debe ser futura.")

        rows, origen, error_response = _sms_filas_de_peticion(data)
        if error_response:
            return error_response
        # Con Excel la base se guarda en la programación (la consulta queda
        # como "Excel: <archivo>" para verla en el listado).
        filas_excel = json.dumps(rows, ensure_ascii=False) if data.get("excel_id") else None

        # Sin destinatarios hoy no es error: al ejecutar se vuelve a leer la base.
        mensajes_validos, _ = aplicar_validaciones(
            rows, plantilla, bq_client, allow_resend=allow_resend, link=(data.get("link") or "").strip()
        )

        schedule_id = guardar_programacion(
            query=origen,
            plantilla=plantilla,
            campaign=campana,
            usuario=usuario,
            allow_resend=allow_resend,
            total_dest=len(mensajes_validos),
            tipo_programacion="simple",
            fecha_programada=run_date.isoformat(),
            hora_inicio=run_date.strftime("%H:%M"),
            filas_excel=filas_excel,
            # Con Excel el link ya va en las filas; con consulta se aplica al ejecutar.
            link_whatsapp=None if filas_excel else ((data.get("link_whatsapp") or "").strip() or None),
            id_mensaje=(data.get("id_mensaje") or "").strip(),
            id_campana=(data.get("id_campana") or "").strip(),
            link=(data.get("link") or "").strip(),
        )

        from database import ProgramacionSms
        prog = db.session.get(ProgramacionSms, schedule_id)
        agendar_programacion_sms(prog)

        log_activity(f"🗓️ SMS '{campana}' programado para el {run_date:%Y-%m-%d %H:%M} (id {schedule_id}).")

        return jsonify({
            "success": True,
            "id": schedule_id,
            "tipo_programacion": "simple",
            "fecha_programada": run_date.isoformat(),
            "total_destinatarios": len(mensajes_validos),
            "message": "SMS programado correctamente.",
        })

    except (ValueError, SmsServiceError) as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        logger_sms.exception("Error programando SMS simple")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/sms/programar-recurrente", methods=["POST"])
def sms_programar_recurrente():
    """Programa un envío SMS recurrente diario a una hora hasta fecha_fin."""
    from services.sms_service import (
        guardar_programacion, aplicar_validaciones, SmsServiceError
    )

    global bq_client
    if bq_client is None:
        init_bigquery()

    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    campana = (data.get("campana") or "").strip()
    # Quien envía/programa = el usuario con sesión (registro de acciones y logs); el campo del formulario queda de respaldo.
    usuario = usuario_sesion() or (data.get("usuario") or "").strip()
    allow_resend = bool(data.get("confirmar_reenvio", False))
    hora_inicio = (data.get("hora_inicio") or "").strip()
    fecha_fin = (data.get("fecha_fin") or "").strip() or None
    intervalo_minutos, hora_fin, error_horario = _leer_repeticion(data, hora_inicio)
    if error_horario:
        return jsonify({"success": False, "message": error_horario}), 400

    if data.get("excel_id"):
        # La base de un Excel no cambia: repetirla cada día enviaría lo mismo.
        return jsonify({
            "success": False,
            "message": "Con Excel solo se puede programar un envío (no recurrente). "
                       "Para recurrentes use una consulta."
        }), 400

    faltan = _faltantes_sms(data, hora="hora_inicio")
    if faltan:
        return _respuesta_faltantes(faltan)

    if not re.match(r'^\d{2}:\d{2}$', hora_inicio):
        return jsonify({
            "success": False,
            "message": "Formato de hora inválido. Use HH:MM."
        }), 400

    try:
        rows, error_response = _get_sms_rows(query)
        if error_response:
            return error_response
        link = (data.get("link") or "").strip()
        from services.sms_excel import aplicar_link_mensaje
        aplicar_link_mensaje(rows, link)

        # Sin destinatarios hoy no es error: cada turno vuelve a leer la base.
        mensajes_validos, _ = aplicar_validaciones(
            rows, plantilla, bq_client, allow_resend=allow_resend, link=link
        )

        schedule_id = guardar_programacion(
            query=query,
            plantilla=plantilla,
            campaign=campana,
            usuario=usuario,
            allow_resend=allow_resend,
            total_dest=len(mensajes_validos),
            tipo_programacion="recurrente",
            hora_inicio=hora_inicio,
            fecha_fin=fecha_fin,
            id_mensaje=(data.get("id_mensaje") or "").strip(),
            id_campana=(data.get("id_campana") or "").strip(),
            intervalo_minutos=intervalo_minutos,
            hora_fin=hora_fin,
            link=link,
        )

        from database import ProgramacionSms
        prog = db.session.get(ProgramacionSms, schedule_id)
        agendar_programacion_sms(prog)

        logger_sms.info(f"✅ SMS recurrente creado: #{schedule_id} (hora={hora_inicio}, fecha_fin={fecha_fin})")
        cada = (f" cada {_texto_intervalo(intervalo_minutos)} hasta las {hora_fin}"
                if intervalo_minutos and hora_fin else "")
        log_activity(f"🔄 SMS recurrente '{campana}' creado: desde las {hora_inicio}{cada}"
                     + (f", hasta el {fecha_fin}" if fecha_fin else "") + f" (id {schedule_id}).")

        return jsonify({
            "success": True,
            "id": schedule_id,
            "tipo_programacion": "recurrente",
            "hora_inicio": hora_inicio,
            "fecha_fin": fecha_fin,
            "total_destinatarios": len(mensajes_validos),
            "intervalo_minutos": intervalo_minutos,
            "hora_fin": hora_fin,
            "message": f"SMS recurrente programado {_texto_horario(hora_inicio, intervalo_minutos, hora_fin)}.",
        })

    except (ValueError, SmsServiceError) as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        logger_sms.exception("Error programando SMS recurrente")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


# ==================== ARRANQUE SCHEDULER SMS ====================

def _recargar_programaciones_sms():
    with app.app_context():
        cargar_programaciones_sms_al_iniciar()


# Al arrancar (unos segundos después, para no demorar el inicio de la app).
scheduler.add_job(_recargar_programaciones_sms, trigger=DateTrigger(
    run_date=datetime.now(COLOMBIA_TZ) + timedelta(seconds=15)), id="sms_recargar_programaciones",
    replace_existing=True)



# ==================== SMS - CRUD MENSAJES (SQLite) ====================

# Sugerencias para el "momento" de las plantillas SMS y email; se suman los ya guardados.
MOMENTOS_SUGERIDOS = ["Mora corta", "Mora larga", "Cliente al día", "Acuerdo de pago",
                      "Recordatorio de pago", "Paz y salvo"]


def _momentos():
    """Momentos sugeridos más los guardados en plantillas SMS y email, sin repetir."""
    from database import MensajeOperacion, PlantillaEmail
    guardados = [m for (m,) in db.session.query(MensajeOperacion.Momento).distinct()]
    guardados += [m for (m,) in db.session.query(PlantillaEmail.momento).distinct()]
    vistos, lista = set(), []
    for momento in MOMENTOS_SUGERIDOS + sorted(m.strip() for m in guardados if m and m.strip()):
        if momento.lower() not in vistos:
            vistos.add(momento.lower())
            lista.append(momento)
    return lista


@app.route("/config-mensajes")
def config_mensajes():
    return render_template("config_mensajes.html", momentos=_momentos())


ETAPAS_SMS = (1, 2, 3, 4)


def _leer_etapa(valor):
    """Etapa de la plantilla SMS: 1 a 4, o None si viene vacía. ValueError si no es válida."""
    if valor in (None, ""):
        return None
    etapa = int(valor)
    if etapa not in ETAPAS_SMS:
        raise ValueError("La etapa debe ser 1, 2, 3 o 4")
    return etapa


@app.route("/api/sms/mensajes", methods=["GET"])
def api_mensajes_list():
    """Plantillas SMS. ?momento=X y ?etapa=N filtran; ?activos=1 solo las activas (constructor)."""
    try:
        from database import MensajeOperacion
        query = MensajeOperacion.query
        momento = (request.args.get("momento") or "").strip()
        if momento:
            query = query.filter(MensajeOperacion.Momento == momento)
        etapa = (request.args.get("etapa") or "").strip()
        if etapa:
            query = query.filter(MensajeOperacion.Etapa == int(etapa))
        if request.args.get("activos") == "1":
            query = query.filter(MensajeOperacion.Estado == 1)
        mensajes = query.order_by(MensajeOperacion.Momento, MensajeOperacion.Etapa, MensajeOperacion.id.desc()).all()
        return jsonify({"success": True, "items": [m.to_dict() for m in mensajes]})
    except Exception as exc:
        logger.exception(f"❌ Error en {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/sms/mensajes", methods=["POST"])
def api_mensajes_create():
    try:
        from database import MensajeOperacion, db
        data = request.get_json() or {}
        momento = (data.get("momento") or "").strip()
        mensaje_texto = (data.get("mensaje") or "").strip()
        estado = int(data.get("estado", 1))
        if not momento or not mensaje_texto:
            return jsonify({"success": False, "message": "Momento y mensaje son obligatorios"}), 400
        try:
            etapa = _leer_etapa(data.get("etapa"))
        except ValueError:
            return jsonify({"success": False, "message": "La etapa debe ser 1, 2, 3 o 4"}), 400
        nuevo = MensajeOperacion(Momento=momento, Etapa=etapa, Mensaje=mensaje_texto, Estado=estado,
                                 IdMensaje=(data.get("id_mensaje") or "").strip() or None,
                                 IdCampana=(data.get("id_campana") or "").strip() or None,
                                 Link=(data.get("link") or "").strip() or None)
        db.session.add(nuevo)
        db.session.commit()
        return jsonify({"success": True, "message": "Mensaje creado", "item": nuevo.to_dict()})
    except Exception as exc:
        logger.exception(f"❌ Error en {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/sms/mensajes/<int:mensaje_id>", methods=["PUT"])
def api_mensajes_update(mensaje_id):
    try:
        from database import MensajeOperacion, db
        mensaje = db.session.get(MensajeOperacion, mensaje_id)
        if not mensaje:
            return jsonify({"success": False, "message": "Mensaje no encontrado"}), 404
        data = request.get_json() or {}
        if "momento" in data: mensaje.Momento = data["momento"].strip()
        if "etapa" in data:
            try:
                mensaje.Etapa = _leer_etapa(data["etapa"])
            except ValueError:
                db.session.rollback()
                return jsonify({"success": False, "message": "La etapa debe ser 1, 2, 3 o 4"}), 400
        if "mensaje" in data: mensaje.Mensaje = data["mensaje"].strip()
        if "id_mensaje" in data: mensaje.IdMensaje = (data["id_mensaje"] or "").strip() or None
        if "id_campana" in data: mensaje.IdCampana = (data["id_campana"] or "").strip() or None
        if "link" in data: mensaje.Link = (data["link"] or "").strip() or None
        if "estado" in data: mensaje.Estado = int(data["estado"])
        if not mensaje.Momento or not mensaje.Mensaje:
            db.session.rollback()
            return jsonify({"success": False, "message": "Momento y mensaje son obligatorios"}), 400
        db.session.commit()
        return jsonify({"success": True, "message": "Mensaje actualizado", "item": mensaje.to_dict()})
    except Exception as exc:
        logger.exception(f"❌ Error en {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/sms/mensajes/<int:mensaje_id>", methods=["DELETE"])
def api_mensajes_delete(mensaje_id):
    try:
        from database import MensajeOperacion, db
        mensaje = db.session.get(MensajeOperacion, mensaje_id)
        if not mensaje:
            return jsonify({"success": False, "message": "Mensaje no encontrado"}), 404
        db.session.delete(mensaje)
        db.session.commit()
        return jsonify({"success": True, "message": "Mensaje eliminado"})
    except Exception as exc:
        logger.exception(f"❌ Error en {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/sms/mensajes/<int:mensaje_id>/toggle", methods=["POST"])
def api_mensajes_toggle(mensaje_id):
    try:
        from database import MensajeOperacion, db
        mensaje = db.session.get(MensajeOperacion, mensaje_id)
        if not mensaje:
            return jsonify({"success": False, "message": "Mensaje no encontrado"}), 404
        mensaje.Estado = 0 if mensaje.Estado == 1 else 1
        db.session.commit()
        return jsonify({"success": True, "message": f"Mensaje {'activado' if mensaje.Estado == 1 else 'desactivado'}", "estado": mensaje.Estado})
    except Exception as exc:
        logger.exception(f"❌ Error en {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500





# ==================== EMAIL - PÁGINA ====================

@app.route("/config-email")
def config_email():
    """Enviar email ya (con historial). Las programaciones usan la misma pantalla."""
    return render_template("config_email.html", modo="enviar")


@app.route("/email/programaciones")
def email_programaciones_pagina():
    """Crear programaciones de email (simples o recurrentes) y ver/editar/cancelar las existentes."""
    return render_template("config_email.html", modo="programar")


# ==================== EMAIL - CAMPOS OBLIGATORIOS ====================

# Las programaciones de email están en SQLite (database.ProgramacionEmail), como las de SMS.

def _faltantes_email(data, *, con_nombre=True, con_asunto=True, con_fecha=False):
    """Nombres de los campos obligatorios que faltan en la petición de email."""
    faltan = []
    if con_nombre and not (data.get("campana") or "").strip():
        faltan.append("nombre de la campaña")
    if not (data.get("query") or "").strip():
        faltan.append("consulta SQL")
    if con_asunto and not (data.get("asunto") or "").strip():
        faltan.append("asunto")
    if not (data.get("plantilla") or "").strip():
        faltan.append("mensaje (contenido HTML)")
    tipo = (data.get("tipo") or "simple").strip()
    if con_fecha and tipo not in ("simple", "recurrente"):
        faltan.append("tipo (simple o recurrente)")
    elif con_fecha and tipo == "recurrente":
        # Igual que SMS: hora de envío y, si se repite en el día, hasta qué hora.
        if not (data.get("hora_inicio") or "").strip():
            faltan.append("hora de envío")
        if data.get("intervalo_minutos") and not (data.get("hora_fin") or "").strip():
            faltan.append("hasta qué hora (Hasta las)")
    elif con_fecha and not (data.get("fecha_programada") or "").strip():
        faltan.append("fecha y hora de envío")
    return faltan


# ==================== EMAIL - PREVIEW ====================

@app.route("/api/email/preview", methods=["POST"])
def email_preview():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    asunto = (data.get("asunto") or "").strip()

    faltan = _faltantes_email(data, con_nombre=False, con_asunto=False)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        rows, error_response = _get_sms_rows(query)
        if error_response:
            return error_response
        from services.email_service import validar_variables_plantilla
        validacion = validar_variables_plantilla(rows, plantilla, asunto)
        if not validacion["valido"]:
            return jsonify({"success": False, "message": validacion["error"], "validacion": validacion}), 400
        result = preview_email(rows, plantilla, asunto)
        if not result.get("success"):
            return jsonify(result), 400
        return jsonify({"success": True, **result})
    except Exception as exc:
        logger_email.exception("❌ Error en la vista previa de email")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


# ==================== EMAIL - VALIDAR ====================

def _variables_sin_campo_email(plantilla, asunto):
    """
    (variables sin campo personalizado en la cuenta de email, aviso). Esas
    variables saldrían como texto en el correo. Si no se pudo revisar la cuenta,
    la lista va vacía y el aviso dice por qué.
    """
    from services.email_service import construir_mapeo_campos, variables_sin_campo
    if not plantilla:
        return [], None
    try:
        email_config = load_config().get("email", {})
        if not email_config.get("api_key"):
            return [], "No se revisaron los campos de la cuenta de email: falta email.api_key en config.json."
        campos_api = EmailClient(email_config["api_key"]).obtener_campos_personalizados()
        if not campos_api.get("success"):
            return [], "No se pudieron leer los campos personalizados de la cuenta de email."
        mapeo = construir_mapeo_campos(email_config, campos_api.get("data", []))
        return variables_sin_campo(plantilla, asunto, mapeo), None
    except Exception as exc:
        logger.exception(f"❌ Error en {request.method} {request.path}: {exc}")
        return [], f"No se pudieron revisar los campos de la cuenta de email. {mensaje_error(exc, 'la plataforma de email')}"


@app.route("/api/email/validar", methods=["POST"])
def email_validar():
    try:
        data = request.get_json(silent=True) or {}
        query = (data.get("query") or "").strip()
        plantilla = (data.get("plantilla") or "").strip()
        asunto = (data.get("asunto") or "").strip()
        confirmar_reenvio = data.get("confirmar_reenvio", False)

        if not query:
            return _respuesta_faltantes(["consulta SQL"])

        rows, error_response = _get_sms_rows(query)
        if error_response:
            return error_response

        try:
            from services.email_service import senuelos_email
            filtro = filtrar_destinatarios(bq_client, rows, confirmar_reenvio,
                                           senuelos_email(load_config().get("email", {})))
        except EmailServiceError as exc:
            logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
            return jsonify({"success": False, "message": mensaje_error(exc)}), 400
        validacion = {k: v for k, v in filtro.items() if k != "rows"}

        from services.email_service import validar_variables_plantilla, variables_de_plantilla, vista_previa_email
        vars_consulta = validar_variables_plantilla(rows, plantilla, asunto)
        encontradas = {v.strip().lower() for v in vars_consulta["variables_encontradas"]}
        sin_campo, aviso_campos = _variables_sin_campo_email(plantilla, asunto)
        sin_campo = {v.lower() for v in sin_campo}
        variables = [{"nombre": v, "en_consulta": v.lower() in encontradas, "sin_campo": v.lower() in sin_campo}
                     for v in variables_de_plantilla(plantilla, asunto)]

        # Vista previa con los datos de los clientes que sí recibirían el correo
        # (si los filtros los quitan a todos, con las primeras filas de la consulta).
        preview = vista_previa_email(filtro["rows"] or rows, plantilla, asunto, validacion["email_column"])
        validacion["aviso_campos"] = aviso_campos

        return jsonify({"success": True, **validacion, "preview": preview, "variables": variables})
    except Exception as exc:
        logger_email.exception("❌ Error validando la base de email")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


# ==================== EMAIL - ENVIAR ====================

def _progreso_email(campana):
    """Avance de la carga de contactos en la actividad en vivo del inicio (cada 100)."""
    def progreso(hechos, total, con_error):
        log_activity(f"📧 Email '{campana}': {hechos}/{total} contactos cargados en la plataforma de email"
                     + (f", {con_error} con error" if con_error else ""))
    return progreso


def _resumen_envio_email(filas, resultado):
    """'22 enviados de 147 registros · 129 excluidos (120 ya recibieron hoy, 9 en lista negra) · campaña 49 (Sending)'."""
    partes = [(resultado.get("duplicados_excluidos") or 0, "ya recibieron hoy"),
              (resultado.get("blacklist_excluidos") or 0, "en lista negra"),
              (resultado.get("invalidos") or 0, "sin email válido")]
    partes = [(n, texto) for n, texto in partes if n]
    texto = f"{resultado['enviados']} enviados" + (f" de {filas} registros" if filas is not None else "")
    if partes:
        texto += f" · {sum(n for n, _ in partes)} excluidos (" + ", ".join(f"{n} {t}" for n, t in partes) + ")"
    return texto + f" · campaña {resultado['campana_id']} ({resultado['estado_campana']})"


def _aviso_fallidos_email(resultado):
    """Texto para el resultado cuando algunos contactos no se pudieron cargar (no les llegó)."""
    if not resultado.get("fallidos"):
        return ""
    return (f". {resultado['fallidos']} no se pudieron cargar en la plataforma de email y no les llegó: "
            "vuelve a enviar y saldrá solo a ellos (los que ya recibieron hoy se excluyen solos).")


@app.route("/api/email/enviar", methods=["POST"])
def email_send():
    """Envía emails ya (la lógica vive en services.email_service.enviar_campana_email)."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    asunto = (data.get("asunto") or "").strip()
    campana_nombre = (data.get("campana") or "").strip()
    # Quien envía/programa = el usuario con sesión (registro de acciones y logs); el campo del formulario queda de respaldo.
    usuario = usuario_sesion() or (data.get("usuario") or "").strip()
    reply_email = (data.get("reply_email") or "").strip()
    confirmar_reenvio = data.get("confirmar_reenvio", False)

    faltan = _faltantes_email(data)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        rows, error_response = _get_sms_rows(query)
        if error_response:
            return error_response
        plantilla_guardada = _plantilla_email_del_envio(data)
        nombre = f"Email '{campana_nombre}' (envío de {usuario or 'sin usuario'})"
        log_activity(f"📋 {nombre}: BigQuery trajo {len(rows)} registros · revisando lista negra y "
                     f"enviados de hoy, cargando contactos…")
        resultado = enviar_campana_email(
            bq_client, rows, plantilla, asunto, _campana_con_plantilla(campana_nombre, plantilla_guardada),
            usuario, reply_email, confirmar_reenvio, load_config().get("email", {}),
            progreso=_progreso_email(campana_nombre),
        )
        _registrar_uso_plantilla_email(plantilla_guardada)
        log_gui_action("Envio Email", campana_id=resultado["campana_id"], enviados=resultado["enviados"])
        mensaje = (f"{resultado['enviados']} emails enviados (campaña {resultado['campana_id']}: "
                   f"{resultado['estado_campana']} en la plataforma de email)") + _aviso_fallidos_email(resultado)
        log_activity(f"{'⚠️' if resultado.get('fallidos') else '✅'} {nombre}: "
                     f"{_resumen_envio_email(len(rows), resultado)}" + (_aviso_fallidos_email(resultado) or "."),
                     "WARNING" if resultado.get("fallidos") else "INFO")
        return jsonify({"success": True, **resultado, "message": mensaje})
    except (EmailServiceError, EmailClientError) as exc:
        logger_email.error(f"❌ Envío de email '{campana_nombre}' rechazado: {exc}")
        log_activity(f"❌ Email '{campana_nombre}': NO SE ENVIÓ. {mensaje_error(exc, 'la plataforma de email')}",
                     "ERROR")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        logger_email.exception(f"❌ Error enviando los emails de la campaña '{campana_nombre}'")
        log_activity(f"❌ Email '{campana_nombre}': NO SE ENVIÓ. {mensaje_error(exc, 'la plataforma de email')}",
                     "ERROR")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500

# ==================== EMAIL - HISTORIAL ====================

@app.route("/api/email/historial")
def email_history():
    try:
        from google.cloud import bigquery
        page = max(1, int(request.args.get("page", 1)))
        per_page = min(100, max(1, int(request.args.get("per_page", 25))))
        offset = (page - 1) * per_page

        clauses = []
        parameters = []
        fecha = (request.args.get("fecha") or "").strip()
        if fecha:
            clauses.append("DATE(fecha_envio) = @fecha")
            parameters.append(bigquery.ScalarQueryParameter("fecha", "DATE", fecha))
        campana = (request.args.get("campana") or "").strip()
        if campana:
            clauses.append("LOWER(campana) LIKE LOWER(@campana)")
            parameters.append(bigquery.ScalarQueryParameter("campana", "STRING", f"%{campana}%"))
        email = (request.args.get("email") or "").strip()
        if email:
            clauses.append("email LIKE @email")
            parameters.append(bigquery.ScalarQueryParameter("email", "STRING", f"%{email}%"))

        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        parameters.extend([
            bigquery.ScalarQueryParameter("limit", "INT64", per_page),
            bigquery.ScalarQueryParameter("offset", "INT64", offset)
        ])

        query_sql = f"""
            SELECT * FROM `{EMAIL_LOG_TABLE}` {where}
            ORDER BY fecha_envio DESC LIMIT @limit OFFSET @offset
        """
        job_config = bigquery.QueryJobConfig(query_parameters=parameters)
        if not _asegurar_bq():
            return jsonify({"success": False, "message": "No se pudo conectar a BigQuery."}), 500
        df = bq_client.query(query_sql, job_config=job_config).to_dataframe()
        items = df.to_dict('records') if not df.empty else []
        return jsonify({"success": True, "page": page, "items": items})
    except Exception as exc:
        logger_email.exception("❌ Error leyendo el historial de email")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


# ==================== EMAIL - PROGRAMAR ====================

@app.route("/api/email/programar", methods=["POST"])
def email_schedule():
    """
    Programa un email, igual que SMS: simple (fecha y hora) o recurrente (todos
    los días a hora_inicio, o cada intervalo_minutos hasta hora_fin, hasta fecha_fin).
    """
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    asunto = (data.get("asunto") or "").strip()
    campana_nombre = (data.get("campana") or "").strip()
    # Quien envía/programa = el usuario con sesión (registro de acciones y logs); el campo del formulario queda de respaldo.
    usuario = usuario_sesion() or (data.get("usuario") or "").strip()
    reply_email = (data.get("reply_email") or "").strip()

    faltan = _faltantes_email(data, con_fecha=True)
    if faltan:
        return _respuesta_faltantes(faltan)
    horario, error_horario = _leer_horario_email(data)
    if error_horario:
        return jsonify({"success": False, "message": error_horario}), 400

    try:
        rows, error_response = _get_sms_rows(query)
        if error_response:
            return error_response

        from services.email_service import validar_variables_plantilla
        # Sin filas hoy no es error en una recurrente: cada turno vuelve a leer la base.
        if rows or horario["tipo"] == "simple":
            validacion = validar_variables_plantilla(rows, plantilla, asunto)
            if not validacion["valido"]:
                return jsonify({"success": False, "message": validacion["error"], "validacion": validacion}), 400
        sin_campo, _ = _variables_sin_campo_email(plantilla, asunto)
        if sin_campo:
            return jsonify({"success": False, "message":
                f"Estas variables no tienen campo personalizado en la cuenta de email y el envío fallaría: "
                f"{', '.join(sin_campo)}. Créalas en la cuenta o quítalas de la plantilla."}), 400

        plantilla_guardada = _plantilla_email_del_envio(data)
        from database import ProgramacionEmail
        prog = ProgramacionEmail(
            id=str(uuid4()), consulta_sql=query, plantilla=plantilla, asunto=asunto,
            campana=_campana_con_plantilla(campana_nombre, plantilla_guardada) or "",
            usuario=usuario, reply_email=reply_email, estado="pendiente", **horario,
        )
        db.session.add(prog)
        db.session.commit()
        _email_agendar(prog)

        _registrar_uso_plantilla_email(plantilla_guardada)
        cuando = _texto_horario_email(prog)
        log_activity(f"📧 Email programado {prog.id} ({prog.tipo}): {campana_nombre}, {cuando}")
        return jsonify({"success": True, "id": prog.id, "tipo": prog.tipo, "item": prog.a_dict(),
                        "message": f"Email programado {cuando}."})
    except (ValueError, EmailServiceError) as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        db.session.rollback()
        logger_email.exception("❌ Error programando el email")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


def _ahora_colombia():
    """Hora de Colombia sin zona (así se guardan las fechas de las programaciones)."""
    return datetime.now(COLOMBIA_TZ).replace(tzinfo=None)


def _leer_horario_email(data):
    """
    (campos de horario, error) de una programación de email, igual que SMS.
    Simple: fecha_programada futura. Recurrente: hora_inicio, repetición en el
    día (_leer_repeticion) y fecha_fin opcional; fecha_programada = ahora, desde
    cuándo corre (los turnos de hoy que ya pasaron no salen).
    """
    tipo = (data.get("tipo") or "simple").strip()
    if tipo == "recurrente":
        hora_inicio = (data.get("hora_inicio") or "").strip()[:5]
        if not re.match(r"^\d{2}:\d{2}$", hora_inicio):
            return None, "Hora de envío inválida. Use HH:MM."
        intervalo, hora_fin, error = _leer_repeticion(data, hora_inicio)
        if error:
            return None, error
        fecha_fin = (data.get("fecha_fin") or "").strip() or None
        if fecha_fin:
            try:
                datetime.strptime(fecha_fin, "%Y-%m-%d")
            except ValueError:
                return None, "Fecha fin inválida (AAAA-MM-DD)."
            if fecha_fin < _ahora_colombia().date().isoformat():
                return None, "La fecha fin ya pasó."
        return {"tipo": "recurrente", "fecha_programada": _ahora_colombia().replace(microsecond=0),
                "hora_inicio": hora_inicio, "intervalo_minutos": intervalo, "hora_fin": hora_fin,
                "fecha_fin": fecha_fin}, None
    try:
        run_date = datetime.fromisoformat((data.get("fecha_programada") or "").strip())
    except ValueError:
        return None, f"Fecha y hora de envío inválida: {data.get('fecha_programada')}"
    if run_date.tzinfo is not None:
        run_date = run_date.astimezone(COLOMBIA_TZ).replace(tzinfo=None)
    if run_date <= _ahora_colombia():
        return None, "La fecha y hora de envío debe ser futura."
    return {"tipo": "simple", "fecha_programada": run_date, "hora_inicio": None,
            "intervalo_minutos": None, "hora_fin": None, "fecha_fin": None}, None


def _texto_horario_email(prog):
    """'para 2026-10-20 08:00' o 'todos los días a las 08:00, 10:00 (hasta el ...)'."""
    if prog.tipo != "recurrente":
        return f"para {prog.fecha_programada:%Y-%m-%d %H:%M}"
    texto = _texto_horario(prog.hora_recurrente(), prog.intervalo_minutos, prog.hora_fin)
    return texto + (f" hasta el {prog.fecha_fin}" if prog.fecha_fin else "")


def _email_agendar(prog):
    """
    Crea o reemplaza el job de la programación, como SMS: simple = email_programado_<id>
    a su fecha (DateTrigger); recurrente = email_recurrente_<id>, revisa cada 10 min
    (cada minuto si se repite en el día) y envía cuando toca un turno.
    """
    _email_desagendar(prog.id)
    if prog.estado != "pendiente":
        return
    if prog.tipo == "recurrente":
        revisar_cada = 1 if prog.intervalo_minutos else 10
        # Primera revisión en unos segundos (antes a los 10 min): al reintentar o al
        # arrancar la app, si el turno de hoy no ha salido, sale ya.
        scheduler.add_job(
            execute_email_schedule, trigger=IntervalTrigger(minutes=revisar_cada), args=[prog.id],
            id=f"email_recurrente_{prog.id}", replace_existing=True, max_instances=1, coalesce=True,
            next_run_time=datetime.now(COLOMBIA_TZ) + timedelta(seconds=5),
        )
        return
    run_date = max(COLOMBIA_TZ.localize(prog.fecha_programada), datetime.now(COLOMBIA_TZ) + timedelta(seconds=5))
    scheduler.add_job(
        execute_email_schedule, trigger=DateTrigger(run_date=run_date), args=[prog.id],
        id=f"email_programado_{prog.id}", replace_existing=True, misfire_grace_time=3600,
    )


def _email_desagendar(schedule_id):
    """Quita los jobs de la programación, simple y recurrente (si no existen, no pasa nada)."""
    for job_id in (f"email_programado_{schedule_id}", f"email_recurrente_{schedule_id}"):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass


def _email_programacion_estado(schedule_id, nuevo, solo_si=None):
    """
    Cambia el estado de una programación de email. Con solo_si, solo si está en
    ese estado; devuelve cuántas filas cambió (para no tomarla dos veces).
    """
    from database import ProgramacionEmail
    consulta = ProgramacionEmail.query.filter_by(id=schedule_id)
    if solo_si:
        consulta = consulta.filter_by(estado=solo_si)
    cambiadas = consulta.update({"estado": nuevo, "fecha_actualizacion": _ahora_colombia()},
                                synchronize_session=False)
    db.session.commit()
    return cambiadas


def _email_programacion_error(schedule_id, mensaje):
    """Deja la programación en estado 'error' con el mensaje en ultimo_error y quita su job, como SMS."""
    from database import ProgramacionEmail
    ProgramacionEmail.query.filter_by(id=schedule_id).update(
        {"estado": "error", "ultimo_error": str(mensaje)[:1000], "fecha_actualizacion": _ahora_colombia()},
        synchronize_session=False)
    db.session.commit()
    _email_desagendar(schedule_id)


def _email_turno(prog, ahora):
    """
    Recurrente: el turno (datetime) que toca enviar ahora, o None (misma regla
    que SMS, services/sms_horarios.turno_a_enviar). Los turnos de antes de
    fecha_programada (cuando se creó o editó) no salen.
    """
    from services.sms_horarios import turno_a_enviar
    desde = prog.fecha_programada - timedelta(seconds=1)
    # Un turno ya intentado (aunque haya fallado) no se repite: sigue el próximo.
    ultima = max(d for d in (prog.fecha_ejecucion, desde, prog.ultimo_intento) if d)
    return turno_a_enviar(ahora, ultima, prog.hora_recurrente() or "08:00", prog.intervalo_minutos, prog.hora_fin)


def execute_email_schedule(schedule_id: str):
    """
    Jobs email_programado_<id> / email_recurrente_<id>: envía una programación de
    email con los mismos filtros que el envío inmediato. La recurrente solo envía
    cuando toca un turno y vuelve a quedar pendiente; al pasar fecha_fin queda completada.
    """
    from database import ProgramacionEmail
    from services.email_service import SinDestinatariosEmail
    with app.app_context():
        prog = db.session.get(ProgramacionEmail, schedule_id)
        if not prog or prog.estado != "pendiente":
            return
        campana, turno, filas = prog.campana or "", prog.fecha_programada, None
        recurrente = prog.tipo == "recurrente"
        if recurrente:
            ahora = _ahora_colombia()
            if prog.fecha_fin and ahora.date().isoformat() > prog.fecha_fin:
                prog.estado = "completado"
                db.session.commit()
                _email_desagendar(schedule_id)
                log_activity(f"🛑 Email recurrente '{campana or schedule_id}': pasó la fecha fin, queda completado.")
                return
            try:
                turno = _email_turno(prog, ahora)
            except ValueError:
                logger_email.warning(f"⚠️ Email recurrente {schedule_id}: horario inválido ({prog.hora_inicio})")
                return
            if turno is None:
                return
            logger_email.info(f"⏰ Email recurrente '{campana}': turno de las {turno:%H:%M}")
        else:
            logger_email.info(f"⏰ Email programado '{campana}': hora de envío {turno:%Y-%m-%d %H:%M}")
        logger_email.info(f"   ▶️ Empieza '{campana}' (programación {schedule_id}, creada por {prog.usuario or 'sin usuario'})")
        # Consola y Actividad en vivo: una línea clara por paso (el detalle va a logs/email.log).
        nombre = f"Email '{campana or schedule_id}'"
        etiqueta = f"{nombre} · {'turno' if recurrente else 'programado'} {turno:%H:%M}"
        try:
            # Reservarla: si ya se tomó (o se canceló/eliminó), no se envía dos veces.
            if _email_programacion_estado(schedule_id, "enviando", solo_si="pendiente") != 1:
                logger_email.warning(f"   ⏭️ '{campana}': no se envía, ya no está pendiente (otra ejecución la tomó, "
                                     f"o se canceló o eliminó)")
                return
            prog = db.session.get(ProgramacionEmail, schedule_id)
            if recurrente:
                # Se marca antes de enviar: si falla a mitad, este turno no se repite (duplicaría lo que salió).
                prog.ultimo_intento = turno
                db.session.commit()
            if not _asegurar_bq():
                raise EmailServiceError("No se pudo conectar a BigQuery para leer la base de la consulta.")
            logger_email.info(f"   🔎 '{campana}': corriendo la consulta en BigQuery…")
            log_activity(f"⏰ {etiqueta}: consultando BigQuery…")
            fetched = fetch_sms_query_rows(bq_client, prog.consulta_sql)
            if not fetched.get("success"):
                raise EmailServiceError(f"La consulta falló: {fetched.get('message', 'error en la consulta')}")
            filas = len(fetched["rows"])
            logger_email.info(f"   📋 '{campana}': la consulta trajo {filas} registro(s)")
            if filas:
                log_activity(f"📋 {nombre}: BigQuery trajo {filas} registros · revisando lista negra y "
                             f"enviados de hoy, cargando contactos…")
            try:
                resultado = enviar_campana_email(
                    bq_client, fetched["rows"], prog.plantilla, prog.asunto or "",
                    prog.campana or "", prog.usuario or "sistema", prog.reply_email or "",
                    False, load_config().get("email", {}), progreso=_progreso_email(campana),
                )
            except SinDestinatariosEmail as exc:
                # Sin datos no es una falla: queda con 0 enviados y la recurrente sigue.
                resultado = {"enviados": 0, "sin_datos": str(exc)}
            prog.ultimo_enviados = resultado["enviados"]
            prog.fecha_ejecucion = _ahora_colombia()
            prog.estado = "pendiente" if recurrente else "enviado"
            prog.ultimo_error = None
            db.session.commit()
            if resultado.get("sin_datos"):
                registrar_ejecucion("email", schedule_id, campana, turno, "sin_datos", filas=filas, enviados=0,
                                    mensaje=resultado["sin_datos"])
                logger_email.warning(f"   ⚠️ Email programado '{campana or schedule_id}': NO SE ENVIÓ. "
                                     f"{resultado['sin_datos']}")
                log_activity(f"⏭️ {etiqueta}: {filas} registros. {resultado['sin_datos']}")
            else:
                logger_email.info(
                    f"   ✅ '{campana}': ENVIADO. Consulta {filas} · enviados {resultado['enviados']} · "
                    f"fallidos {resultado.get('fallidos', 0)} · lista negra {resultado['blacklist_excluidos']} · "
                    f"duplicados de hoy {resultado['duplicados_excluidos']} · inválidos {resultado.get('invalidos', 0)} · "
                    f"campaña {resultado['campana_id']} ({resultado['estado_campana']})")
                registrar_ejecucion("email", schedule_id, campana, turno, "enviado", filas=filas,
                                    enviados=resultado["enviados"],
                                    excluidos=resultado["blacklist_excluidos"] + resultado["duplicados_excluidos"],
                                    mensaje=f"Campaña {resultado['campana_id']}: {resultado['estado_campana']}")
                log_activity(f"{'⚠️' if resultado.get('fallidos') else '✅'} {etiqueta}: "
                             f"{_resumen_envio_email(filas, resultado)}" + (_aviso_fallidos_email(resultado) or "."),
                             "WARNING" if resultado.get("fallidos") else "INFO")
        except Exception as exc:
            db.session.rollback()
            logger_email.exception(f"   ❌ '{campana}': NO SE ENVIÓ por un error ({type(exc).__name__}): {exc} "
                                   f"[programación {schedule_id}, consulta: {filas if filas is not None else 'sin leer'} registro(s)]")
            mensaje = mensaje_error(exc, "la plataforma de email")
            try:
                if recurrente:
                    # La recurrente sigue: este turno queda con error y el próximo sale normal.
                    ProgramacionEmail.query.filter_by(id=schedule_id).update(
                        {"estado": "pendiente", "ultimo_error": mensaje[:1000], "fecha_actualizacion": _ahora_colombia()},
                        synchronize_session=False)
                    db.session.commit()
                else:
                    _email_programacion_error(schedule_id, mensaje)
            except Exception:
                db.session.rollback()
                logger_email.exception(f"No se pudo guardar el error de la programación de email {schedule_id}")
            if recurrente:
                log_activity(f"❌ {etiqueta}: NO SE ENVIÓ. {mensaje} Sigue en el próximo turno.", "ERROR")
            else:
                log_activity(f"❌ {etiqueta}: NO SE ENVIÓ. {mensaje}", "ERROR")
            registrar_ejecucion("email", schedule_id, campana, turno, "error", filas=filas, mensaje=mensaje)


def cargar_programaciones_email_al_iniciar():
    """
    Vuelve a agendar las programaciones de email pendientes (los jobs se pierden
    al reiniciar). Simple con la hora ya pasada: menos de 1 hora, sale en 5
    segundos; más de 1 hora, queda con error. Las recurrentes siguen la regla de
    turnos (como SMS). En la copia de prueba no se recargan.
    """
    from modo_prueba import es_copia_prueba
    from database import ProgramacionEmail
    if es_copia_prueba():
        logger_email.warning("🧪 Copia de prueba: no se recargan las programaciones de email.")
        return
    with app.app_context():
        try:
            pendientes = ProgramacionEmail.query.filter_by(estado="pendiente").all()
            ahora = _ahora_colombia()
            for prog in pendientes:
                if prog.tipo != "recurrente" and ahora - prog.fecha_programada > timedelta(hours=1):
                    _email_programacion_error(prog.id, "No se envió: pasó más de 1 hora con la app apagada.")
                    log_activity(f"⚠️ Email programado '{prog.campana or prog.id}' para "
                                 f"{prog.fecha_programada:%Y-%m-%d %H:%M} no se envió (pasó más de 1 hora "
                                 f"con la app apagada): queda con error.", "WARNING")
                    continue
                _email_agendar(prog)
            logger.info(f"🔁 Programaciones de email pendientes revisadas: {len(pendientes)}")
        except Exception:
            db.session.rollback()
            logger_email.exception("❌ Error recargando las programaciones de email")


# Al arrancar (unos segundos después, para no demorar el inicio de la app).
scheduler.add_job(cargar_programaciones_email_al_iniciar, trigger=DateTrigger(
    run_date=datetime.now(COLOMBIA_TZ) + timedelta(seconds=15)), id="email_recargar_programaciones",
    replace_existing=True)


# ==================== EMAIL - PROGRAMACIONES (CRUD en SQLite) ====================

@app.route("/api/email/programaciones", methods=["GET"])
def email_programaciones():
    from database import ProgramacionEmail
    try:
        progs = ProgramacionEmail.query.order_by(ProgramacionEmail.fecha_programada.desc()).limit(200).all()
        return jsonify({"success": True, "items": [p.a_dict() for p in progs]})
    except Exception as exc:
        logger_email.exception("❌ Error listando las programaciones de email")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/email/programaciones/<schedule_id>", methods=["GET"])
def email_ver_programacion(schedule_id):
    from database import ProgramacionEmail
    prog = db.session.get(ProgramacionEmail, schedule_id)
    if not prog:
        return jsonify({"success": False, "message": "La programación no existe."}), 404
    return jsonify({"success": True, "item": prog.a_dict()})


@app.route("/api/email/programaciones/<schedule_id>", methods=["PATCH"])
def email_editar_programacion(schedule_id):
    """
    Edita una programación de email (nombre, consulta, asunto, mensaje, reply-to,
    responsable y horario, igual que SMS). Solo si no se está enviando ni se
    envió; si estaba cancelada, completada o con error, vuelve a quedar
    pendiente. Revisa la consulta en BigQuery (dry run) y reemplaza su job.
    """
    from database import ProgramacionEmail
    from consulta_segura import ConsultaNoPermitida, verificar_consulta_lectura
    from bigquery import mensaje_error_bigquery

    data = request.get_json(silent=True) or {}
    faltan = _faltantes_email(data, con_fecha=True)
    if faltan:
        return _respuesta_faltantes(faltan)
    horario, error_horario = _leer_horario_email(data)
    if error_horario:
        return jsonify({"success": False, "message": error_horario}), 400

    prog = db.session.get(ProgramacionEmail, schedule_id)
    if not prog:
        return jsonify({"success": False, "message": "La programación no existe."}), 404
    if prog.estado in ("enviando", "enviado"):
        return jsonify({"success": False, "message": "Ya se envió (o se está enviando): no se puede editar."}), 409

    # La consulta se sigue leyendo de BigQuery al enviar: se revisa que sea válida.
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo conectar a BigQuery para revisar la consulta."}), 500
    try:
        verificar_consulta_lectura(bq_client, data["query"])
    except ConsultaNoPermitida as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "message": mensaje_error_bigquery(exc)}), 400

    try:
        prog.campana = data["campana"].strip()
        prog.consulta_sql = data["query"].strip()
        prog.asunto = data["asunto"].strip()
        prog.plantilla = data["plantilla"].strip()
        prog.reply_email = (data.get("reply_email") or "").strip()
        prog.usuario = (data.get("usuario") or "").strip()
        for campo, valor in horario.items():
            setattr(prog, campo, valor)
        prog.estado = "pendiente"
        prog.ultimo_error = None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger_email.exception(f"❌ Error editando la programación de email {schedule_id}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500

    _email_agendar(prog)
    cuando = _texto_horario_email(prog)
    log_activity(f"✏️ Email programado {schedule_id} editado: {prog.campana}, {cuando}")
    return jsonify({"success": True, "message": f"Programación actualizada: {cuando}.", "item": prog.a_dict()})


@app.route("/api/email/cancelar/<schedule_id>", methods=["POST"])
@app.route("/api/email/programaciones/<schedule_id>/cancelar", methods=["POST"])
def email_cancelar_programacion(schedule_id):
    try:
        # Solo si sigue pendiente: si ya se está enviando o ya salió, no se puede cancelar.
        if _email_programacion_estado(schedule_id, "cancelado", solo_si="pendiente") != 1:
            return jsonify({"success": False, "message": "Esta programación ya no está pendiente: no se puede cancelar."}), 409
        _email_desagendar(schedule_id)
        log_activity(f"🚫 Email programado {schedule_id} cancelado")
        return jsonify({"success": True, "message": "Programación cancelada"})
    except Exception as exc:
        db.session.rollback()
        logger_email.exception(f"❌ Error cancelando la programación de email {schedule_id}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/email/programaciones/<schedule_id>", methods=["DELETE"])
def email_eliminar_programacion(schedule_id):
    """Borra la programación y su job. No se puede mientras se está enviando."""
    from database import ProgramacionEmail
    try:
        prog = db.session.get(ProgramacionEmail, schedule_id)
        if not prog:
            return jsonify({"success": False, "message": "La programación no existe."}), 404
        if prog.estado == "enviando":
            return jsonify({"success": False, "message": "Se está enviando ahora: espera a que termine para eliminarla."}), 409
        campana = prog.campana
        db.session.delete(prog)
        db.session.commit()
        _email_desagendar(schedule_id)
        log_activity(f"🗑️ Email programado {schedule_id} eliminado ({campana})")
        return jsonify({"success": True, "message": "Programación eliminada"})
    except Exception as exc:
        db.session.rollback()
        logger_email.exception(f"❌ Error eliminando la programación de email {schedule_id}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


@app.route("/api/email/programaciones/<schedule_id>/reejecutar", methods=["POST"])
def email_reejecutar_programacion(schedule_id):
    """
    Vuelve a dejar pendiente una programación de email con error o cancelada y
    la agenda otra vez. Simple: a su fecha si aún no pasó, si no en unos
    segundos. Recurrente: sigue con sus turnos (si el de ahora no ha salido, sale).
    """
    from database import ProgramacionEmail
    prog = db.session.get(ProgramacionEmail, schedule_id)
    if not prog:
        return jsonify({"success": False, "message": "La programación no existe."}), 404
    if prog.estado not in ("error", "cancelado"):
        return jsonify({"success": False, "message": "Solo se reintenta una programación con error o cancelada."}), 409
    try:
        prog.estado = "pendiente"
        prog.ultimo_error = None
        prog.ultimo_intento = None  # quien reintenta quiere que el turno de ahora salga
        if prog.tipo != "recurrente":
            prog.fecha_programada = max(prog.fecha_programada, _ahora_colombia() + timedelta(seconds=5))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger_email.exception(f"❌ Error reintentando la programación de email {schedule_id}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500
    _email_agendar(prog)
    cuando = _texto_horario_email(prog)
    if prog.tipo == "recurrente":
        try:
            turno = _email_turno(prog, _ahora_colombia())
        except ValueError:
            turno = None
        que_pasa = (f"el turno de hoy de las {turno:%H:%M} no ha salido: se envía en unos segundos" if turno else
                    f"hoy no hay turno pendiente (ya salió o aún no llega): sigue {cuando}")
    else:
        que_pasa = f"se envía el {prog.fecha_programada:%Y-%m-%d %H:%M}"
    from flask import g
    quien = getattr(g.get("usuario"), "usuario", None) or "sin usuario"
    aviso = f"🔁 Email '{prog.campana}' reintentado por {quien}: {que_pasa}."
    logger_email.info(aviso)
    log_activity(aviso)
    return jsonify({"success": True, "message": f"Reintento: {que_pasa}.", "fecha_programada": prog.fecha_programada.isoformat()})


# ==================== EMAIL - PLANTILLAS ====================
# Plantillas guardadas (PlantillaEmail): asunto + HTML con {{variables}}. Una
# plantilla ya usada no cambia su contenido: al editarla se guarda como "<nombre> v2".

@app.route("/email/plantillas", methods=["GET"])
def email_plantillas_pagina():
    return render_template("email_plantillas.html", momentos=_momentos())


@app.route("/api/email/plantillas", methods=["GET"])
def email_plantillas_listar():
    from database import PlantillaEmail
    consulta = PlantillaEmail.query
    if request.args.get("todas") != "1":
        consulta = consulta.filter_by(activa=True)
    plantillas = consulta.order_by(PlantillaEmail.activa.desc(), PlantillaEmail.nombre).all()
    return jsonify({"success": True, "plantillas": [p.a_dict() for p in plantillas]})


def _leer_plantilla_formulario():
    """(nombre, asunto, html, momento, usuario, errores) del JSON recibido."""
    from services.email_service import errores_plantilla
    data = request.get_json(silent=True) or {}
    nombre = re.sub(r"\s+", " ", str(data.get("nombre") or "")).strip()
    asunto = str(data.get("asunto") or "").strip()
    html = str(data.get("html") or "").strip()
    momento = str(data.get("momento") or "").strip() or None
    usuario = usuario_sesion() or str(data.get("usuario") or "").strip() or None
    return nombre, asunto, html, momento, usuario, errores_plantilla(nombre, asunto, html)


def _nombre_plantilla_repetido(nombre, excepto_id=None):
    from database import PlantillaEmail
    otra = PlantillaEmail.query.filter(db.func.lower(PlantillaEmail.nombre) == nombre.lower()).first()
    return otra is not None and otra.id != excepto_id


def _nombres_plantillas():
    from database import PlantillaEmail
    return [n for (n,) in db.session.query(PlantillaEmail.nombre).all()]


@app.route("/api/email/plantillas", methods=["POST"])
def email_plantillas_crear():
    from database import PlantillaEmail
    nombre, asunto, html, momento, usuario, errores = _leer_plantilla_formulario()
    if not errores and _nombre_plantilla_repetido(nombre):
        errores.append(f"Ya existe una plantilla llamada '{nombre}'.")
    if errores:
        return jsonify({"success": False, "message": " ".join(errores)}), 400
    plantilla = PlantillaEmail(nombre=nombre, momento=momento, asunto=asunto, html=html, usuario=usuario, activa=True)
    db.session.add(plantilla)
    db.session.commit()
    log_activity(f"📧 Plantilla de email creada (id {plantilla.id}): {nombre}")
    return jsonify({"success": True, "plantilla": plantilla.a_dict()})


@app.route("/api/email/plantillas/<int:plantilla_id>", methods=["PUT"])
def email_plantillas_editar(plantilla_id):
    """
    Edita una plantilla. Si ya se usó y cambia el asunto o el HTML, responde 409
    (requiere_nueva); con {"como_nueva": true} la guarda como "<nombre> v2".
    """
    from database import PlantillaEmail
    from services.email_service import nombre_libre
    plantilla = db.session.get(PlantillaEmail, plantilla_id)
    if not plantilla:
        return jsonify({"success": False, "message": "Plantilla no encontrada."}), 404
    nombre, asunto, html, momento, usuario, errores = _leer_plantilla_formulario()
    if errores:
        return jsonify({"success": False, "message": " ".join(errores)}), 400
    cambia_contenido = asunto != (plantilla.asunto or "") or html != plantilla.html
    if plantilla.usos and cambia_contenido:
        if not (request.get_json(silent=True) or {}).get("como_nueva"):
            return jsonify({
                "success": False, "requiere_nueva": True,
                "nombre_nueva": nombre_libre(nombre, _nombres_plantillas(), "v"),
                "message": f"Esta plantilla ya se usó en {plantilla.usos} envío(s). "
                           "¿Guardar los cambios como una plantilla nueva?",
            }), 409
        nueva = PlantillaEmail(nombre=nombre_libre(nombre, _nombres_plantillas(), "v"), momento=momento,
                               asunto=asunto, html=html, usuario=usuario, activa=True)
        db.session.add(nueva)
        db.session.commit()
        log_activity(f"📧 Plantilla de email '{plantilla.nombre}' guardada como nueva: {nueva.nombre}")
        return jsonify({"success": True, "plantilla": nueva.a_dict(), "nueva": True})
    if _nombre_plantilla_repetido(nombre, excepto_id=plantilla.id):
        return jsonify({"success": False, "message": f"Ya existe una plantilla llamada '{nombre}'."}), 400
    plantilla.nombre, plantilla.asunto, plantilla.html, plantilla.momento = nombre, asunto, html, momento
    if usuario:
        plantilla.usuario = usuario
    db.session.commit()
    log_activity(f"📧 Plantilla de email editada (id {plantilla.id}): {nombre}")
    return jsonify({"success": True, "plantilla": plantilla.a_dict()})


@app.route("/api/email/plantillas/<int:plantilla_id>/duplicar", methods=["POST"])
def email_plantillas_duplicar(plantilla_id):
    from database import PlantillaEmail
    from services.email_service import nombre_libre
    original = db.session.get(PlantillaEmail, plantilla_id)
    if not original:
        return jsonify({"success": False, "message": "Plantilla no encontrada."}), 404
    usuario = usuario_sesion() or str((request.get_json(silent=True) or {}).get("usuario") or "").strip() or original.usuario
    copia = PlantillaEmail(nombre=nombre_libre(original.nombre, _nombres_plantillas(), "copia "),
                           momento=original.momento, asunto=original.asunto, html=original.html, usuario=usuario, activa=True)
    db.session.add(copia)
    db.session.commit()
    log_activity(f"📧 Plantilla de email duplicada: {original.nombre} -> {copia.nombre}")
    return jsonify({"success": True, "plantilla": copia.a_dict()})


@app.route("/api/email/plantillas/<int:plantilla_id>/activa", methods=["POST"])
def email_plantillas_activa(plantilla_id):
    """Activa o desactiva. No se borran: su nombre queda en los envíos."""
    from database import PlantillaEmail
    plantilla = db.session.get(PlantillaEmail, plantilla_id)
    if not plantilla:
        return jsonify({"success": False, "message": "Plantilla no encontrada."}), 404
    plantilla.activa = bool((request.get_json(silent=True) or {}).get("activa"))
    db.session.commit()
    log_activity(f"📧 Plantilla de email {'activada' if plantilla.activa else 'desactivada'}: {plantilla.nombre}")
    return jsonify({"success": True, "plantilla": plantilla.a_dict()})


@app.route("/api/email/plantillas/<int:plantilla_id>", methods=["DELETE"])
def email_plantillas_eliminar(plantilla_id):
    """
    Borra una plantilla que nunca se usó. Las usadas no se borran (su nombre
    queda en los envíos y programaciones): se desactivan.
    """
    from database import PlantillaEmail
    plantilla = db.session.get(PlantillaEmail, plantilla_id)
    if not plantilla:
        return jsonify({"success": False, "message": "Plantilla no encontrada."}), 404
    if plantilla.usos:
        return jsonify({"success": False, "message": f"La plantilla ya se usó en {plantilla.usos} envío(s): "
                        "no se puede eliminar, desactívela."}), 409
    nombre = plantilla.nombre
    db.session.delete(plantilla)
    db.session.commit()
    log_activity(f"🗑️ Plantilla de email eliminada (id {plantilla_id}): {nombre}")
    return jsonify({"success": True, "message": "Plantilla eliminada."})


def _plantilla_email_del_envio(data):
    """PlantillaEmail elegida en /config-email (o None)."""
    from database import PlantillaEmail
    try:
        plantilla_id = int(data.get("plantilla_id") or 0)
    except (TypeError, ValueError):
        return None
    return db.session.get(PlantillaEmail, plantilla_id) if plantilla_id else None


def _registrar_uso_plantilla_email(plantilla):
    """Suma un uso (envío o programación) a la plantilla."""
    if not plantilla:
        return
    try:
        plantilla.usos = (plantilla.usos or 0) + 1
        plantilla.fecha_ultimo_uso = datetime.now(COLOMBIA_TZ).replace(tzinfo=None)
        db.session.commit()
    except Exception:
        db.session.rollback()
        logger.exception(f"No se pudo registrar el uso de la plantilla de email {plantilla.id}")


def _campana_con_plantilla(campana, plantilla):
    """
    Nombre de campaña con la plantilla: EmailLog y ProgramacionEmail no tienen
    columna plantilla_id, así que va en el nombre.
    """
    if not plantilla:
        return campana
    base = campana or f"Email {datetime.now(COLOMBIA_TZ):%Y-%m-%d %H:%M}"
    return f"{base} · Plantilla #{plantilla.id} {plantilla.nombre}"



# ==================== LOGS ====================

@app.route("/api/recent_logs", methods=["GET"])
def api_recent_logs():
    return jsonify(read_recent_log_lines(200))


@app.route("/downloads/<filename>", methods=["GET"])
def download_file(filename):
    try:
        return send_from_directory(str(DOWNLOAD_FOLDER), filename, as_attachment=True)
    except Exception as e:
        return jsonify({"error": "Archivo no encontrado"}), 404



# ================== SMS - PROGRAMACIONES (PÁGINA) ==================

@app.route("/sms/programaciones")
@app.route("/api/sms/ver_programaciones")  # dirección vieja
def sms_programaciones_pagina():
    """Crear programaciones SMS (simples o recurrentes) y ver/editar/cancelar/reintentar las existentes."""
    return render_template("config_sms.html", modo="programar")


@app.route("/programaciones")
def programaciones_pagina():
    """
    Historial de programaciones SMS y email en una sola tabla (como la de
    Programaciones Terminator): ver el mensaje, si se ejecutó, editar,
    cancelar, reintentar y eliminar. Usa las APIs de cada canal.
    """
    return render_template("programaciones.html")


# ==================== GALERÍA DE PLANTILLAS ====================

@app.route("/plantillas")
def plantillas_galeria():
    """Galería de plantillas de email y SMS: verlas, crear, editar, duplicar, activar/desactivar y eliminar."""
    return render_template("plantillas.html", momentos=_momentos(), etapas=ETAPAS_SMS)


# ==================== HISTORIAL DE ENVÍOS ====================
# Todo lo enviado (SMS y email, manual o programado), un envío por fila. Las
# consultas están en historial_envios.py.

@app.route("/envios")
def envios_pagina():
    """Historial de envíos: qué salió, cuándo, a cuántos, entregados/fallidos, costo y destinatarios."""
    return render_template("envios.html")


def _rango_envios():
    """(desde, hasta) en UTC de ?desde=&hasta= (días de Colombia, por defecto los últimos 7). ValueError si no sirven."""
    hoy = datetime.now(COLOMBIA_TZ).date()
    desde = datetime.strptime(request.args.get("desde") or (hoy - timedelta(days=6)).isoformat(), "%Y-%m-%d")
    hasta = datetime.strptime(request.args.get("hasta") or hoy.isoformat(), "%Y-%m-%d")
    if hasta < desde:
        raise ValueError("La fecha 'hasta' es anterior a 'desde'.")
    if (hasta - desde).days > 92:
        raise ValueError("El rango máximo es de 93 días.")
    return (COLOMBIA_TZ.localize(desde).astimezone(timezone.utc),
            COLOMBIA_TZ.localize(hasta + timedelta(days=1)).astimezone(timezone.utc))


def _a_colombia(fecha):
    """Timestamp de BigQuery (UTC) → 'AAAA-MM-DD HH:MM:SS' en hora de Colombia."""
    return fecha.astimezone(COLOMBIA_TZ).strftime("%Y-%m-%d %H:%M:%S") if fecha else ""


def _programaciones_para_origen():
    """[(canal, id, campaña en minúsculas, fecha o None, ['HH:MM', ...])] para saber qué envío salió de una programación."""
    from database import ProgramacionEmail, ProgramacionSms
    from services.sms_horarios import horas_del_dia
    progs = []
    for p in ProgramacionSms.query.all():
        if (p.tipo_programacion or "simple").lower() == "recurrente":
            try:
                horas = horas_del_dia(p.hora_inicio or "08:00", p.intervalo_minutos, p.hora_fin)
            except ValueError:
                horas = []
            progs.append(("sms", p.id, (p.campana or "").strip().lower(), None, horas))
        elif p.fecha_programada:
            progs.append(("sms", p.id, (p.campana or "").strip().lower(), p.fecha_programada.date(),
                          [p.fecha_programada.strftime("%H:%M")]))
    for p in ProgramacionEmail.query.all():
        if p.tipo == "recurrente":
            try:
                horas = horas_del_dia(p.hora_recurrente() or "08:00", p.intervalo_minutos, p.hora_fin)
            except ValueError:
                horas = []
            progs.append(("email", p.id, (p.campana or "").strip().lower(), None, horas))
        else:
            progs.append(("email", p.id, (p.campana or "").strip().lower(), p.fecha_programada.date(),
                          [p.fecha_programada.strftime("%H:%M")]))
    return progs


def _origen_envio(canal, campana, inicio, progs, ejecuciones):
    """
    {"tipo": "programacion"/"manual", "id"}: programación si hay una ejecución
    registrada de esa campaña a menos de 10 min, o una programación con esa
    campaña y un turno a esa hora (±15 min). inicio: hora de Colombia sin zona.
    """
    nombre = (campana or "").strip().lower()
    for e in ejecuciones:
        if e.canal == canal and (e.campana or "").strip().lower() == nombre and abs(e.fecha - inicio) <= timedelta(minutes=10):
            return {"tipo": "programacion", "id": e.programacion_id}
    minuto = inicio.hour * 60 + inicio.minute
    for c, pid, camp, fecha, horas in progs:
        if c != canal or camp != nombre or (fecha and fecha != inicio.date()):
            continue
        if any(abs(int(h[:2]) * 60 + int(h[3:5]) - minuto) <= 15 for h in horas):
            return {"tipo": "programacion", "id": str(pid)}
    return {"tipo": "manual", "id": None}


@app.route("/api/envios")
def api_envios():
    """
    Envíos entre ?desde y ?hasta (días de Colombia) de ?canal=sms|email (los dos
    si no va). ?destino= solo los envíos que incluyen ese teléfono o email. Si
    un canal falla, el otro responde igual y el error va en 'avisos'.
    """
    import historial_envios
    from bigquery import mensaje_error_bigquery
    from database import EjecucionProgramacion
    try:
        desde, hasta = _rango_envios()
    except ValueError as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 400
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo conectar a BigQuery."}), 500
    canal = request.args.get("canal") or ""
    destino = (request.args.get("destino") or "").strip()
    progs = _programaciones_para_origen()
    ejecuciones = EjecucionProgramacion.query.filter(
        EjecucionProgramacion.fecha >= desde.astimezone(COLOMBIA_TZ).replace(tzinfo=None) - timedelta(hours=1)).all()
    items, avisos = [], []

    if canal in ("", "sms"):
        try:
            for e in historial_envios.envios_sms(bq_client, desde, hasta, destino):
                inicio = e["inicio"].astimezone(COLOMBIA_TZ).replace(tzinfo=None)
                items.append({
                    "canal": "sms", "campana": e["campana"], "usuario": e["usuario"],
                    "inicio": _a_colombia(e["inicio"]), "fin": _a_colombia(e["fin"]),
                    "inicio_utc": e["inicio"].isoformat(), "fin_utc": e["fin"].isoformat(),
                    "mensaje": e["plantilla"], "ejemplo": e["ejemplo"], "consulta": e["consulta"],
                    "mensajes": e["mensajes"], "entregados": e["entregados"], "fallidos": e["fallidos"],
                    "pendientes": e["pendientes"], "sin_reporte": e["sin_reporte"], "costo": round(e["costo"]),
                    "bulk_ids": e["bulk_ids"], "origen": _origen_envio("sms", e["campana"], inicio, progs, ejecuciones),
                })
        except Exception as exc:
            logger_sms.exception("❌ Error leyendo el historial de envíos SMS")
            avisos.append(f"SMS: {mensaje_error_bigquery(exc)}")

    if canal in ("", "email"):
        try:
            for e in historial_envios.envios_email(bq_client, desde, hasta, destino):
                inicio = e["inicio"].astimezone(COLOMBIA_TZ).replace(tzinfo=None)
                items.append({
                    "canal": "email", "campana": e["campana"] or "", "usuario": e["usuario"],
                    "inicio": _a_colombia(e["inicio"]), "fin": _a_colombia(e["fin"]),
                    "mensaje": e["asunto"] or "", "ejemplo": e["contenido"] or "", "consulta": "",
                    "mensajes": e["mensajes"], "entregados": None, "fallidos": e["fallidos"],
                    "pendientes": None, "sin_reporte": None, "costo": None,
                    "bulk_ids": [e["bulk_id"]] if e["bulk_id"] else [], "campana_id": e["bulk_id"],
                    "origen": _origen_envio("email", e["campana"], inicio, progs, ejecuciones),
                })
        except Exception as exc:
            logger_email.exception("❌ Error leyendo el historial de envíos de email")
            avisos.append(f"Email: {mensaje_error_bigquery(exc)}")

    items.sort(key=lambda i: i["inicio"], reverse=True)
    return jsonify({"success": True, "items": items, "avisos": avisos,
                    "desde": desde.astimezone(COLOMBIA_TZ).date().isoformat(),
                    "hasta": (hasta.astimezone(COLOMBIA_TZ) - timedelta(days=1)).date().isoformat()})


def _detalle_envio():
    """Filas del detalle de un envío según ?canal (sms: campana, inicio_utc, fin_utc; email: campana_id)."""
    import historial_envios
    if request.args.get("canal") == "email":
        filas = historial_envios.detalle_email(bq_client, request.args.get("campana_id") or "")
        return [{"destino": f["email"], "fecha": _a_colombia(f["fecha_envio"]), "estado": f["resultado"] or "",
                 "error": f["error"] or "", "mensaje": f["asunto"] or ""} for f in filas]
    inicio = datetime.fromisoformat(request.args["inicio_utc"])
    fin = datetime.fromisoformat(request.args["fin_utc"])
    filas = historial_envios.detalle_sms(bq_client, request.args.get("campana") or "", inicio, fin)
    return [{"destino": f["telefono"], "fecha": _a_colombia(f["fecha_envio"]), "estado": f["categoria"],
             "estado_infobip": f["estado_infobip"] or f["resultado"] or "", "error": f["error"] or "",
             "mensaje": f["mensaje"] or "", "costo": round(f["costo"] or 0, 1), "bulk_id": f["bulk_id"] or "",
             "message_id": f["message_id"] or ""} for f in filas]


@app.route("/api/envios/detalle")
def api_envios_detalle():
    """Cada mensaje de un envío (máximo 5.000), con su estado."""
    from bigquery import mensaje_error_bigquery
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo conectar a BigQuery."}), 500
    try:
        return jsonify({"success": True, "filas": _detalle_envio()})
    except (KeyError, ValueError):
        return jsonify({"success": False, "message": "Faltan los datos del envío."}), 400
    except Exception as exc:
        logger.exception("Error leyendo el detalle del envío")
        return jsonify({"success": False, "message": mensaje_error_bigquery(exc)}), 500


@app.route("/api/envios/exportar")
def api_envios_exportar():
    """CSV (;) con cada mensaje de un envío."""
    import csv
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo conectar a BigQuery."}), 500
    try:
        filas = _detalle_envio()
    except (KeyError, ValueError):
        return jsonify({"success": False, "message": "Faltan los datos del envío."}), 400
    salida = io.StringIO()
    salida.write("﻿")
    columnas = ["destino", "fecha", "estado", "estado_infobip", "error", "costo", "mensaje", "bulk_id", "message_id"]
    escritor = csv.DictWriter(salida, fieldnames=columnas, delimiter=";", extrasaction="ignore")
    escritor.writeheader()
    escritor.writerows(filas)
    nombre = re.sub(r"[^\w\-]+", "_", request.args.get("campana") or request.args.get("campana_id") or "envio")[:60]
    return send_file(io.BytesIO(salida.getvalue().encode("utf-8")), as_attachment=True,
                     download_name=f"envio_{request.args.get('canal') or 'sms'}_{nombre}.csv", mimetype="text/csv")


@app.route("/api/envios/email/<campana_id>/estado")
def api_envios_email_estado(campana_id):
    """Estado de la campaña en la plataforma de email (si salió, a cuántos, cuándo empezó y terminó)."""
    email_config = load_config().get("email", {})
    if not email_config.get("api_key"):
        return jsonify({"success": False, "message": "Falta email.api_key en config.json."}), 400
    try:
        datos = EmailClient(email_config["api_key"]).detalle_campana(campana_id).get("data", {})
    except EmailClientError as exc:
        logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
        if "NotFound" in str(exc):
            return jsonify({"success": True, "existe": False, "message":
                            "Esta campaña no existe en la cuenta de email configurada: no salió por esta cuenta."})
        return jsonify({"success": False, "message": mensaje_error(exc, "la plataforma de email")}), 502
    return jsonify({"success": True, "existe": True, "status": datos.get("status"),
                    "destinatarios": datos.get("recipients_count"), "inicio": datos.get("start_date"),
                    "fin": datos.get("finish_date"), "remitente": datos.get("fromEmail")})


# ================== API PROGRAMACIONES ==================

def _prog_sms_a_dict(prog):
    """Programación SMS para la pantalla (lista y edición)."""
    return {
        'id': prog.id,
        'nombre': prog.campana,
        'tipo_programacion': prog.tipo_programacion,
        'consulta_sql': prog.consulta_sql,
        'origen': 'excel' if prog.filas_excel else 'consulta',
        'plantilla': prog.plantilla,
        'campana': prog.campana,
        'usuario': prog.usuario,
        'estado': prog.estado,
        'ultimo_error': prog.ultimo_error or '',
        'total_destinatarios': prog.total_destinatarios,
        'ultimo_enviados': prog.ultimo_enviados,
        'id_mensaje': prog.id_mensaje or '',
        'id_campana': prog.id_campana or '',
        'link': prog.link or '',
        'confirmar_reenvio': prog.confirmar_reenvio,
        'fecha_programada': prog.fecha_programada.isoformat() if prog.fecha_programada else None,
        'hora_inicio': prog.hora_inicio,
        'intervalo_minutos': prog.intervalo_minutos,
        'hora_fin': prog.hora_fin,
        'fecha_fin': prog.fecha_fin,
        'fecha_ejecucion': prog.fecha_ejecucion.isoformat() if prog.fecha_ejecucion else None,
        'fecha_creacion': prog.fecha_creacion.isoformat() if prog.fecha_creacion else None,
        'fecha_actualizacion': prog.fecha_actualizacion.isoformat() if prog.fecha_actualizacion else None,
    }


@app.route("/api/sms/programaciones", methods=["GET"])
def api_list_programaciones_sms():
    """API para listar programaciones de SMS (JSON) desde SQLite."""
    from database import ProgramacionSms

    estado = request.args.get("estado")
    tipo = request.args.get("tipo_programacion")
    query = ProgramacionSms.query
    if estado:
        query = query.filter_by(estado=estado)
    if tipo:
        query = query.filter_by(tipo_programacion=tipo)

    data = [_prog_sms_a_dict(prog) for prog in query.order_by(ProgramacionSms.id.desc()).all()]
    return jsonify({"success": True, "total": len(data), "data": data}), 200


@app.route("/api/sms/programaciones/<int:prog_id>", methods=["GET"])
def api_get_programacion(prog_id):
    from database import ProgramacionSms
    prog = db.session.get(ProgramacionSms, prog_id)
    if not prog:
        return jsonify({"success": False, "error": "No encontrada"}), 404
    return jsonify({"success": True, "data": _prog_sms_a_dict(prog)}), 200


@app.route("/api/sms/programaciones/<int:prog_id>", methods=["PATCH"])
def api_update_programacion(prog_id):
    """
    Edita una programación SMS: nombre, consulta, mensaje, tipo (simple o
    recurrente), fecha/hora, fecha fin, responsable y reenvío. Valida los
    campos (nombre de cada uno que falte), revisa la consulta en BigQuery (dry
    run) y reemplaza el job del scheduler.
    """
    from database import ProgramacionSms
    from consulta_segura import ConsultaNoPermitida, verificar_consulta_lectura
    from bigquery import mensaje_error_bigquery

    prog = db.session.get(ProgramacionSms, prog_id)
    if not prog:
        return jsonify({"success": False, "error": "No encontrada"}), 404

    data = request.get_json(silent=True) or {}
    campana = (data.get("campana") if "campana" in data else prog.campana) or ""
    plantilla = (data.get("plantilla") if "plantilla" in data else prog.plantilla) or ""
    consulta = (data.get("consulta_sql") if "consulta_sql" in data else prog.consulta_sql) or ""
    tipo = ((data.get("tipo_programacion") if "tipo_programacion" in data else prog.tipo_programacion) or "").lower()
    hora_inicio = ((data.get("hora_inicio") if "hora_inicio" in data else prog.hora_inicio) or "").strip()
    fecha_fin = ((data.get("fecha_fin") if "fecha_fin" in data else prog.fecha_fin) or "").strip() or None
    fecha_texto = (data.get("fecha_programada") or "").strip() if "fecha_programada" in data else (
        prog.fecha_programada.isoformat() if prog.fecha_programada else "")
    es_excel = bool(prog.filas_excel)

    faltan = []
    if not campana.strip():
        faltan.append("nombre de la campaña")
    if not es_excel and not consulta.strip():
        faltan.append("consulta SQL")
    if not plantilla.strip():
        faltan.append("mensaje")
    if tipo not in ("simple", "recurrente"):
        faltan.append("tipo (simple o recurrente)")
    if tipo == "simple" and not fecha_texto:
        faltan.append("fecha y hora de envío")
    if tipo == "recurrente" and not hora_inicio:
        faltan.append("hora de envío")
    if faltan:
        return _respuesta_faltantes(faltan)

    if tipo == "recurrente" and es_excel:
        return jsonify({"success": False, "message": "Una programación con base en Excel solo puede ser simple."}), 400
    if tipo == "recurrente" and not re.match(r'^\d{2}:\d{2}$', hora_inicio):
        return jsonify({"success": False, "message": "Hora de envío inválida. Use HH:MM."}), 400
    if "intervalo_minutos" in data:
        intervalo_minutos, hora_fin, error_horario = _leer_repeticion(data, hora_inicio)
    else:
        intervalo_minutos, hora_fin, error_horario = prog.intervalo_minutos, prog.hora_fin, None
    if tipo == "recurrente" and error_horario:
        return jsonify({"success": False, "message": error_horario}), 400

    fecha_programada = None
    if tipo == "simple":
        try:
            fecha_programada = datetime.fromisoformat(fecha_texto.replace('Z', '+00:00'))
        except ValueError:
            return jsonify({"success": False, "message": f"Fecha y hora de envío inválida: {fecha_texto}"}), 400
        if fecha_programada.tzinfo is not None:
            fecha_programada = fecha_programada.astimezone(COLOMBIA_TZ).replace(tzinfo=None)
        if COLOMBIA_TZ.localize(fecha_programada) <= datetime.now(COLOMBIA_TZ):
            return jsonify({"success": False, "message": "La fecha y hora de envío debe ser futura."}), 400

    # La consulta se revisa en BigQuery (dry run: no lee datos) si cambió.
    if not es_excel and consulta.strip() != (prog.consulta_sql or "").strip():
        if not _asegurar_bq():
            return jsonify({"success": False, "message": "No se pudo conectar a BigQuery para revisar la consulta."}), 500
        try:
            verificar_consulta_lectura(bq_client, consulta)
        except ConsultaNoPermitida as exc:
            logger.warning(f"⚠️ {request.method} {request.path}: {exc}")
            return jsonify({"success": False, "message": mensaje_error(exc)}), 400
        except Exception as exc:
            return jsonify({"success": False, "message": mensaje_error_bigquery(exc)}), 400

    prog.campana = campana.strip()
    prog.plantilla = plantilla.strip()
    if not es_excel:
        prog.consulta_sql = consulta.strip()
    prog.tipo_programacion = tipo
    if tipo == "simple":
        prog.fecha_programada = fecha_programada
        prog.hora_inicio = fecha_programada.strftime("%H:%M")
        prog.fecha_fin = None
    else:
        prog.fecha_programada = None
        prog.hora_inicio = hora_inicio
        prog.fecha_fin = fecha_fin
    # La repetición en el día solo aplica a recurrentes.
    prog.intervalo_minutos = intervalo_minutos if tipo == "recurrente" else None
    prog.hora_fin = hora_fin if tipo == "recurrente" else None
    if "usuario" in data:
        prog.usuario = (data.get("usuario") or "").strip()
    if "confirmar_reenvio" in data:
        prog.confirmar_reenvio = bool(data.get("confirmar_reenvio"))
    if "id_mensaje" in data:
        prog.id_mensaje = (data.get("id_mensaje") or "").strip() or None
    if "id_campana" in data:
        prog.id_campana = (data.get("id_campana") or "").strip() or None
    if "link" in data:
        prog.link = (data.get("link") or "").strip() or None
    # Editar una programación con error o ya ejecutada la deja pendiente otra vez.
    prog.estado = 'pendiente'
    prog.ultimo_error = None
    if tipo == "recurrente":
        prog.fecha_ejecucion = None

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        logger_sms.exception(f"❌ Error guardando la edición de la programación SMS #{prog_id}")
        return jsonify({"success": False, "message": mensaje_error(e)}), 500

    agendar_programacion_sms(prog)
    log_activity(f"✏️ Programación SMS '{prog.campana or prog.id}' editada (id {prog.id}).")
    return jsonify({"success": True, "data": _prog_sms_a_dict(prog)}), 200


@app.route("/api/sms/programaciones/<int:prog_id>", methods=["DELETE"])
def api_delete_programacion(prog_id):
    from database import ProgramacionSms
    prog = db.session.get(ProgramacionSms, prog_id)
    if not prog:
        return jsonify({"success": False, "error": "No encontrada"}), 404
    logger_sms.info(f"🗑️ Eliminando programación de SMS #{prog_id}")
    try:
        campana = prog.campana or prog_id
        _sms_desagendar(prog)
        db.session.delete(prog)
        db.session.commit()
        log_activity(f"🗑️ Programación SMS '{campana}' eliminada (id {prog_id}).")
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.exception(f"❌ Error en {request.method} {request.path}: {e}")
        db.session.rollback()
        return jsonify({"success": False, "error": mensaje_error(e), "message": mensaje_error(e)}), 500


@app.route("/api/sms/programaciones/<int:prog_id>/cancelar", methods=["POST"])
def api_cancelar_programacion(prog_id):
    from database import ProgramacionSms
    prog = db.session.get(ProgramacionSms, prog_id)
    if not prog:
        return jsonify({"success": False, "error": "No encontrada"}), 404
    logger_sms.info(f"⛔ Cancelando programación de SMS #{prog_id}")
    prog.estado = 'cancelado'
    try:
        db.session.commit()
        _sms_desagendar(prog)
        log_activity(f"🚫 Programación SMS '{prog.campana or prog_id}' cancelada (id {prog_id}).")
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.exception(f"❌ Error en {request.method} {request.path}: {e}")
        db.session.rollback()
        return jsonify({"success": False, "error": mensaje_error(e), "message": mensaje_error(e)}), 500


@app.route("/api/sms/programaciones/<int:prog_id>/reejecutar", methods=["POST"])
def api_reejecutar_programacion(prog_id):
    """Vuelve a dejar pendiente una programación (error o cancelada) y la agenda de nuevo."""
    from database import ProgramacionSms
    prog = db.session.get(ProgramacionSms, prog_id)
    if not prog:
        return jsonify({"success": False, "error": "No encontrada"}), 404
    prog.estado = 'pendiente'
    prog.ultimo_error = None
    prog.fecha_ejecucion = None
    prog.ultimo_intento = None  # quien reintenta quiere que el turno de ahora salga
    try:
        db.session.commit()
        # Simple con fecha ya pasada: agendar_programacion_sms la corre en 5 segundos.
        agendar_programacion_sms(prog)
        return jsonify({"success": True}), 200
    except Exception as e:
        logger.exception(f"❌ Error en {request.method} {request.path}: {e}")
        db.session.rollback()
        return jsonify({"success": False, "error": mensaje_error(e), "message": mensaje_error(e)}), 500

# ══════════════════════════════════════════════════════════════════════


# ==================================================
# 📊 DASHBOARD SMS
# ==================================================

@app.route("/dashboards", methods=["GET"])
def dashboards_page():
    """Página del dashboard"""
    return render_template("dashboard.html")


def _infobip_config():
    infobip = load_config().get("infobip", {})
    return infobip.get("api_key", "").strip(), infobip.get("base_url", "").strip()


@app.route("/api/dashboards", methods=["GET"])
def get_dashboards():
    """
    Dashboard de SMS (cálculos en dashboard_sms.py).
    Fuente por defecto: todos los SMS de la cuenta de Infobip, copiados a
    BigQuery (Temporal.SmsInfobip_Valoro) por el job sms_infobip_sync, con
    ?periodo=hoy|ayer|48h|7d o ?desde=AAAA-MM-DD&hasta=AAAA-MM-DD.
    ?fuente=smslog: solo lo enviado desde la app (SmsLog, histórico), con ?horas=N o fechas.
    """
    import dashboard_sms
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo inicializar BigQuery."}), 500
    desde, hasta = request.args.get("desde") or None, request.args.get("hasta") or None
    campana = request.args.get("campana") or ""
    try:
        if request.args.get("fuente") != "smslog":
            datos = dashboard_sms.obtener_dashboard_infobip(
                bq_client, request.args.get("periodo") or "hoy", desde, hasta, campana)
            return jsonify({"success": True, **datos})
        horas = request.args.get("horas", type=int)
        if horas is not None and not 1 <= horas <= 24 * 31:
            return jsonify({"success": False, "message": "Horas entre 1 y 744."}), 400
        datos = dashboard_sms.obtener_dashboard(bq_client, horas=horas, desde=desde, hasta=hasta, campana=campana)
        return jsonify({"success": True, **datos})
    except ValueError:
        return jsonify({"success": False, "message": "Fechas con formato inválido (AAAA-MM-DD)."}), 400
    except Exception as exc:
        logger.exception("Error en el dashboard de SMS")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500


def _filtros_detalle_infobip():
    a = request.args
    return dict(periodo=a.get("periodo") or "hoy", desde=a.get("desde") or None, hasta=a.get("hasta") or None,
                bulk_id=a.get("bulk_id") or "", estado=a.get("estado") or "", operador=a.get("operador") or "",
                error=a.get("error") or "", buscar=a.get("buscar") or "")


@app.route("/api/dashboards/mensajes", methods=["GET"])
def dashboards_mensajes():
    """SMS de Infobip (tabla SmsInfobip_Valoro) filtrados (bulk_id, estado, operador, error, buscar), de a 500 por página."""
    import dashboard_sms
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo inicializar BigQuery."}), 500
    try:
        datos = dashboard_sms.detalle_infobip(bq_client, **_filtros_detalle_infobip(),
                                              pagina=request.args.get("pagina", 1, type=int))
        return jsonify({"success": True, **datos})
    except ValueError:
        return jsonify({"success": False, "message": "Fechas con formato inválido (AAAA-MM-DD)."}), 400


@app.route("/api/dashboards/exportar", methods=["GET"])
def dashboards_exportar():
    """CSV (;) con los SMS de Infobip (tabla SmsInfobip_Valoro) que cumplen los filtros (máximo 300.000)."""
    import csv
    import dashboard_sms
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo inicializar BigQuery."}), 500
    datos = dashboard_sms.detalle_infobip(bq_client, **_filtros_detalle_infobip(), todo=True)
    columnas = ["enviado", "entregado", "demora_seg", "bulk_id", "destino", "sender", "operador", "estado",
                "nombre_estado", "error", "descripcion_error", "accion", "precio", "texto", "message_id"]
    salida = io.StringIO()
    salida.write("\ufeff")
    escritor = csv.DictWriter(salida, fieldnames=columnas, delimiter=";", extrasaction="ignore")
    escritor.writeheader()
    escritor.writerows(datos["filas"])
    nombre = f"sms_infobip_{request.args.get('bulk_id') or request.args.get('periodo') or 'hoy'}.csv"
    return send_file(io.BytesIO(salida.getvalue().encode("utf-8")), as_attachment=True,
                     download_name=nombre, mimetype="text/csv")


_sms_infobip_primera_sync = True


def _sincronizar_sms_infobip(horas=2, dia_anterior=False):
    """
    Jobs sms_infobip_sync (cada 5 min, últimas 2 horas: los SMS que todavía
    cambian de estado) y sms_infobip_sync_dia (03:00, el día anterior completo
    con sus estados finales): copian los SMS de Infobip a Temporal.SmsInfobip_Valoro.
    La primera corrida tras arrancar la app copia desde las 00:00 (cubre el
    tiempo que la app estuvo apagada). En la copia de prueba no corren (escriben en BigQuery).
    """
    global _sms_infobip_primera_sync
    import infobip_sync
    from modo_prueba import es_copia_prueba
    if es_copia_prueba():
        return
    api_key, base_url = _infobip_config()
    if not api_key or not base_url or not _asegurar_bq():
        return
    ahora = datetime.now(COLOMBIA_TZ)
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    if dia_anterior:
        desde, hasta = hoy - timedelta(days=1), hoy
    elif _sms_infobip_primera_sync:
        desde, hasta = min(hoy, ahora - timedelta(hours=horas)), ahora
    else:
        desde, hasta = ahora - timedelta(hours=horas), ahora
    try:
        resumen = infobip_sync.sincronizar(bq_client, api_key, base_url, desde, hasta)
        if not dia_anterior:
            _sms_infobip_primera_sync = False
        if resumen["ventanas_con_error"]:
            log_activity(f"⚠️ SMS Infobip: {len(resumen['ventanas_con_error'])} tramos sin sincronizar "
                         f"({', '.join(resumen['ventanas_con_error'][:5])}); se reintentan en la próxima corrida.", "WARNING")
        elif dia_anterior:
            log_activity(f"📨 SMS Infobip: día {desde:%Y-%m-%d} sincronizado ({resumen['sms']:,} SMS).")
    except Exception as exc:
        logger.exception(f"Error sincronizando SMS de Infobip: {exc}")
        log_activity(f"Dashboard SMS: no se pudieron copiar los SMS de Infobip. {mensaje_error(exc, 'Infobip (SMS)')} "
                     f"Se reintenta en la próxima corrida.", "WARNING")


scheduler.add_job(_sincronizar_sms_infobip, trigger=IntervalTrigger(minutes=5), id="sms_infobip_sync",
                  replace_existing=True, max_instances=1, coalesce=True,
                  next_run_time=datetime.now(COLOMBIA_TZ) + timedelta(seconds=30))
scheduler.add_job(_sincronizar_sms_infobip, trigger=CronTrigger(hour=3, minute=0), kwargs={"dia_anterior": True},
                  id="sms_infobip_sync_dia", replace_existing=True, max_instances=1, coalesce=True)


@app.route("/api/dashboards/actualizar-estados", methods=["POST"])
def dashboards_actualizar_estados():
    """Consulta ya a Infobip el estado de los SMS de las últimas 48 h (lo mismo que el polling)."""
    from modo_prueba import es_copia_prueba
    from services.sms_service import actualizar_estados_pendientes
    if es_copia_prueba():
        return jsonify({"success": False, "message": "En la copia de prueba no se actualizan estados (escribe en SmsLog)."}), 400
    infobip = load_config().get("infobip", {})
    api_key, base_url = infobip.get("api_key", "").strip(), infobip.get("base_url", "").strip()
    if not api_key or not base_url:
        return jsonify({"success": False, "message": "Falta la configuración de Infobip en config.json."}), 400
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo inicializar BigQuery."}), 500
    try:
        resultado = actualizar_estados_pendientes(api_key=api_key, base_url=base_url, client=bq_client)
        from dashboard_sms import _cache
        _cache.clear()
        log_activity(f"🔄 Estados de SMS actualizados a mano: {resultado.get('actualizados', 0)} actualizados "
                     f"de {resultado.get('pendientes', 0)} pendientes.")
        return jsonify({**resultado, "success": bool(resultado.get("success"))})
    except Exception as exc:
        logger.exception("Error actualizando estados SMS")
        return jsonify({"success": False, "message": mensaje_error(exc)}), 500
# ==================================================
# 📋 ENDPOINT: DETALLES DE UN BULKID
# ==================================================

@app.route("/api/dashboards/detalles/<path:bulk_id>", methods=["GET"])
def get_detalles_dashboard(bulk_id):
    """
    Devuelve todos los detalles de un bulkId leyendo desde SmsLog
    (los de Infobip van por /api/dashboards/mensajes).
    """
    try:
        from google.cloud import bigquery
        
        logger.info(f"📋 Consultando detalles para bulkId: {bulk_id}")
        
        # 🔥 Verificar qué columnas existen en SmsLog
        check_columns_query = """
            SELECT column_name
            FROM `capable-arbor-209819.Temporal.INFORMATION_SCHEMA.COLUMNS`
            WHERE table_name = 'SmsLog'
        """
        
        try:
            columnas_existentes = {row.column_name for row in bq_client.query(check_columns_query).result()}
        except Exception as e:
            logger.warning(f"⚠️ No se pudo verificar columnas: {e}")
            columnas_existentes = set()
        
        logger.info(f"📋 Columnas disponibles: {columnas_existentes}")
        
        # 🔥 Construir SELECT dinámico
        columnas_select = ["telefono", "campana", "usuario", "fecha_envio", "resultado"]
        
        if "message_id" in columnas_existentes:
            columnas_select.append("message_id")
        else:
            columnas_select.append("'' AS message_id")
        
        if "error" in columnas_existentes:
            columnas_select.append("error")
        else:
            columnas_select.append("'' AS error")
        
        if "fecha_actualizacion" in columnas_existentes:
            columnas_select.append("fecha_actualizacion")
        else:
            columnas_select.append("fecha_envio AS fecha_actualizacion")
        
        select_str = ", ".join(columnas_select)
        
        query = f"""
            SELECT {select_str}
            FROM `{SMS_LOG_TABLE}`
            WHERE bulk_id = @bulk_id
            ORDER BY fecha_envio DESC
            LIMIT 5000
        """
        
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("bulk_id", "STRING", bulk_id)
        ])
        
        resultados = list(bq_client.query(query, job_config=job_config).result())
        
        logger.info(f"📋 {len(resultados)} registros encontrados para {bulk_id}")
        
        if not resultados:
            return jsonify({
                "success": False,
                "message": f"No se encontraron mensajes para el bulkId {bulk_id}"
            }), 404
        
        # Mapear estado
        detalles = []
        for row in resultados:
            resultado = (row.resultado or "").upper()
            if resultado == "ENTREGADO":
                estado = "ENTREGADO"
            elif resultado in ["FALLIDO", "UNDELIVERABLE", "REJECTED", "EXPIRED"]:
                estado = "FALLIDO"
            elif resultado in ["PENDIENTE", "PENDING", "ENVIADO"]:
                estado = "PENDIENTE"
            else:
                estado = "SIN_REPORTE"
            
            fecha_act = getattr(row, 'fecha_actualizacion', None) or row.fecha_envio
            
            detalles.append({
                "telefono": row.telefono or "",
                "message_id": getattr(row, 'message_id', '') or "",
                "estado": estado,
                "group_name": resultado,
                "sent_at": row.fecha_envio.isoformat() if row.fecha_envio else "",
                "done_at": fecha_act.isoformat() if fecha_act else "",
                "precio": 0,
                "currency": "COP",
                "error": getattr(row, 'error', '') or ""
            })
        
        # Calcular estadísticas
        total = len(detalles)
        entregados = sum(1 for d in detalles if d["estado"] == "ENTREGADO")
        fallidos = sum(1 for d in detalles if d["estado"] == "FALLIDO")
        pendientes = sum(1 for d in detalles if d["estado"] == "PENDIENTE")
        tasa = (entregados / total * 100) if total > 0 else 0
        
        return jsonify({
            "success": True,
            "data": {
                "bulk_id": bulk_id,
                "campana": resultados[0].campana or "",
                "usuario": resultados[0].usuario or "",
                "fecha_envio": resultados[0].fecha_envio.strftime("%Y-%m-%d %H:%M:%S") if resultados[0].fecha_envio else "",
                "total": total,
                "entregados": entregados,
                "fallidos": fallidos,
                "pendientes": pendientes,
                "tasa_entrega": round(tasa, 2),
                "detalles": detalles
            },
            "detalles": detalles,
            "total_detalles": len(detalles)
        })
        
    except Exception as e:
        logger.exception(f"❌ Error obteniendo detalles: {e}")
        return jsonify({
            "success": False,
            "message": mensaje_error(e)
        }), 500


# ==================== CONSOLA ====================

def _resumen_pendientes_consola():
    """
    Línea de la consola con los turnos que faltan hoy (SMS y email), de los
    mismos datos que Inicio. backend.console_message_heartbeat la muestra cada
    vez que cambia (console_message_interval_seconds).
    """
    from services.sms_horarios import texto_pendientes
    with app.app_context():
        hoy = datetime.now(COLOMBIA_TZ).date()
        ejecuciones_hoy, desde_registro = _ejecuciones_de_hoy(hoy)
        return texto_pendientes(_programaciones_sms_de_hoy(hoy, ejecuciones_hoy, desde_registro)
                                + _programaciones_email_de_hoy(hoy, ejecuciones_hoy, desde_registro))


registrar_resumen_consola(_resumen_pendientes_consola)


def _tarea_con_error(evento):
    """Una tarea programada que falló sin manejar el error: una línea clara (el traceback queda en app_log.txt)."""
    log_activity(f"Tarea programada '{evento.job_id}' falló: {mensaje_error(evento.exception)}", "ERROR")


from apscheduler.events import EVENT_JOB_ERROR
scheduler.add_listener(_tarea_con_error, EVENT_JOB_ERROR)

# ==================== MAIN ====================

if __name__ == "__main__":
    init_bigquery()
    # Sin debug ni reloader: un solo proceso, y por lo tanto un solo scheduler.
    # Con el reloader se levantaban dos procesos y cada tarea programada al
    # arrancar corría dos veces. Tras editar un .py hay que reiniciar a mano.
    # Puerto configurable con la variable de entorno PORT (por defecto 5000),
    # para poder correr una copia de prueba sin chocar con la aplicación real.
    app.run(debug=False, use_reloader=False, host="0.0.0.0",
            port=int(os.environ.get("PORT", "5000")))
