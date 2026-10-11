from datetime import datetime, timezone, timedelta
from flask import jsonify, render_template, request, send_from_directory, send_file
from uuid import uuid4
import io

# Copia de prueba (APP_COPIA_PRUEBA=1): revisa carpeta y puerto ANTES de
# importar backend, que arranca el scheduler, la base de datos y BigQuery. Si
# la carpeta no es la de esta copia o el puerto es 5000, termina aquí.
from modo_prueba import proteger_arranque_copia_prueba
proteger_arranque_copia_prueba(__file__)

import json
import os
import re
from apscheduler.triggers.cron import CronTrigger
import pytz
COLOMBIA_TZ = pytz.timezone("America/Bogota")

from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from backend import (
    app, db, scheduler, load_config, CONFIG, logger,
    log_task, log_gui_action, read_recent_log_lines,
    cleanup_old_log_files, reschedule_console_message_job, DOWNLOAD_FOLDER,
    log_activity, logger_sms, logger_email,
)
from general_params import load_general_parameters, save_general_parameters
from conexion_bigquery import get_bigquery_client
from bigquery import fetch_sms_query_rows
from services.sms_service import (
    SMS_LOG_TABLE, SmsServiceError, enviar_sms_desde_filas, preview_sms,
)
from services.email_client import EmailClient, EmailClientError
from services.email_service import (
    EMAIL_LOG_TABLE, EmailServiceError, preview_email,
    filtrar_destinatarios, enviar_campana_email,
)

# ==================== CONFIGURACIÓN GLOBAL ====================
PROJECT_ID = "capable-arbor-209819"
bq_client = None


OPERADORES_SMS = [
    "HELLO_BPO",
    "QNT_RBK_2",
    "MORACERO_CD2",
    "QNT_RBK_1.2",
    "AVALOGIC",
    "DIGITAL_MONTOS_BAJOS",
    "DIGITAL_ESPECIAL",
    "PAZ_SALVO",
    "MANTENIMIENTO_DIGITAL",
    "DIGITAL_1",
    "QNT_PERSONA_JURIDICA",
    "QNT_JUD",
    "MANTENIMIENTO-QNT",
    "DIGITAL_2",
    "GRADUADOS",
    "QNT_OPERADOR_ESPECIAL",
    "GENNIALS_BPO_CD",
    "QNT_COBRO",
    "VICBRA",
    "MORACERO_CD",
    "QNT_FALLECIDOS",
    "QNT_RECAUDO",
    "QNT_RBK_1.1",
    "SATELITE_1",
    "MANTENIMIENTO-QNT-MORAS-ALTAS",
    "MANTENIMIENTO-ESP-QNT",
    "VENTAS_TERCEROSJUD"
]



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
    """Inicio: accesos a SMS y Email, programaciones SMS pendientes/con error y la actividad reciente."""
    from database import ProgramacionSms
    return render_template(
        "index.html",
        sms_pendientes=ProgramacionSms.query.filter_by(estado='pendiente').count(),
        sms_con_error=ProgramacionSms.query.filter(ProgramacionSms.estado.in_(['error', 'fallido'])).count(),
    )



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
    from services.sms_excel import aplicar_link_whatsapp

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
        return filas, f"Excel: {cache['archivo']}", None

    query = (data.get("query") or "").strip()
    rows, error_response = _get_sms_rows(query)
    if rows:
        aplicar_link_whatsapp(rows, link_whatsapp, id_mensaje)
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
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger_sms.exception("Error procesando Excel SMS")
        return jsonify({"success": False, "message": f"Error: {exc}"}), 500

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
    return render_template("config_sms.html")



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
        result = preview_sms(rows, plantilla)
        if not result.get("success"):
            return jsonify(result), 400
        return jsonify({"success": True, **result})
    except Exception as exc:
        logger_sms.exception("❌ Error en la vista previa de SMS")
        return jsonify({"success": False, "message": str(exc)}), 500


# ==================== SMS - ENVIAR ====================

@app.route("/api/sms/enviar", methods=["POST"])
def sms_send():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    campaign = (data.get("campana") or "").strip()
    usuario = (data.get("usuario") or "").strip()
    allow_resend = bool(data.get("confirmar_reenvio", False))

    faltan = _faltantes_sms(data)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        rows, origen, error_response = _sms_filas_de_peticion(data)
        if error_response:
            return error_response
        infobip_config = (CONFIG or load_config()).get("infobip", {})
        result = enviar_sms_desde_filas(
            rows, plantilla, infobip_config,
            client=bq_client, campaign=campaign, usuario=usuario,
            query_sql=origen, allow_resend=allow_resend
        )
        registrar_resumen_sms(campaign, usuario, origen, plantilla,
                             result["total_preparados"], result["enviados"],
                             result["fallidos"], "exitoso" if result["fallidos"] == 0 else "parcial")
        return jsonify({"success": result["fallidos"] == 0, **result})
    except SmsServiceError as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger_sms.exception(f"❌ Error enviando SMS de la campaña '{campaign}'")
        return jsonify({"success": False, "message": str(exc)}), 500


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
        preview = preview_sms(rows, plantilla)
        if not preview.get("success"):
            return jsonify(preview), 400

        from services.sms_service import preparar_sms, verificar_lista_negra, verificar_duplicados
        prepared, details = preparar_sms(rows, plantilla)
        if not _asegurar_bq():
            return jsonify({"success": False, "message": "No se pudo conectar a BigQuery para revisar la lista negra."}), 500
        phones = [item["phone"] for item in prepared]
        blocked = verificar_lista_negra(bq_client, phones)
        duplicates = verificar_duplicados(bq_client, phones)

        allowed = [item for item in prepared if item["phone"] not in blocked]
        if not allow_resend:
            allowed = [item for item in allowed if item["phone"] not in duplicates]

        preview_mensajes = [{"telefono": item["phone"], "mensaje": item["text"], "longitud": len(item["text"])} for item in allowed[:3]]

        return jsonify({
            "success": True,
            "total_consulta": len(rows),
            "total_validos": details.get("total_validos", 0),
            "invalidos": details.get("invalid_numbers", 0),
            "duplicados": len(duplicates),
            "blacklist": len(blocked),
            "excluidos_reglas": details.get("excluidos_reglas", {}),
            "a_enviar": len(allowed),
            "preview": preview_mensajes
        })
    except Exception as exc:
        logger_sms.exception("❌ Error validando la base de SMS")
        return jsonify({"success": False, "message": str(exc)}), 500
    

def registrar_resumen_sms(campaign, usuario, query, plantilla, total, enviados, fallidos, estado):
    """Registra un resumen del envío en el log (no interrumpe el flujo)."""
    try:
        log_task(
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

        prog = ProgramacionSms.query.get(int(schedule_id))
        if not prog:
            logger_sms.warning(f"⚠️ Programación SMS {schedule_id} no existe")
            return

        if prog.estado != 'pendiente':
            logger_sms.info(f"⏭️ SMS #{prog.id}: estado={prog.estado}, se omite")
            return

        tipo = (prog.tipo_programacion or 'simple').lower()
        ahora = _sms_now()
        hoy = ahora.date()

        # ---------- VALIDACIONES SOLO PARA RECURRENTE ----------
        if tipo == 'recurrente':
            hora_str = (prog.hora_inicio or '08:00').strip()
            try:
                hh, mm = map(int, hora_str.split(':'))
            except Exception:
                hh, mm = 8, 0

            # 1) ¿Ya se ejecutó hoy?
            if prog.fecha_ejecucion:
                fe = prog.fecha_ejecucion
                if fe.tzinfo is None:
                    fe = pytz.utc.localize(fe).astimezone(COLOMBIA_TZ)
                if fe.date() == hoy:
                    logger_sms.info(f"✅ Recurrente SMS #{prog.id}: ya se ejecutó hoy")
                    return

            # 2) ¿Ya llegó la hora?
            objetivo = COLOMBIA_TZ.localize(
                datetime.combine(hoy, datetime.min.time())
            ).replace(hour=hh, minute=mm, second=0, microsecond=0)

            if ahora < objetivo:
                logger_sms.info(f"⏰ Recurrente SMS #{prog.id}: aún no es la hora")
                return

            # 3) ¿Ya pasó fecha_fin?
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

        # ---------- EJECUTAR ----------
        try:
            logger_sms.info(f"📤 Ejecutando SMS {tipo.upper()} #{prog.id} - {prog.campana}")

            config = (CONFIG or load_config())
            infobip_config = config.get("infobip", {})

            if prog.filas_excel:
                filas = json.loads(prog.filas_excel)
            else:
                if not _asegurar_bq():
                    raise SmsServiceError("No se pudo conectar a BigQuery.")
                fetched = fetch_sms_query_rows(bq_client, prog.consulta_sql)
                if not fetched.get("success"):
                    raise SmsServiceError(fetched.get("message", "Error en consulta"))
                filas = fetched["rows"]
                if prog.link_whatsapp:
                    from services.sms_excel import aplicar_link_whatsapp
                    aplicar_link_whatsapp(filas, prog.link_whatsapp)

            resultado = enviar_sms_desde_filas(
                filas,
                prog.plantilla,
                infobip_config,
                client=bq_client,
                campaign=prog.campana or "",
                usuario=prog.usuario or "sistema",
                query_sql=prog.consulta_sql,
                allow_resend=bool(prog.confirmar_reenvio),
            )

            if tipo == 'simple':
                prog.estado = 'enviado'

            prog.fecha_ejecucion = datetime.utcnow()
            prog.total_destinatarios = resultado.get("total_preparados", 0)
            prog.ultimo_error = None
            prog.fecha_actualizacion = datetime.utcnow()
            db.session.commit()

            logger_sms.info(f"✅ SMS {tipo.capitalize()} enviado: #{prog.id}")
            registro = resultado.get("registro_smslog") or {}
            if registro.get("errores"):
                log_activity(f"⚠️ SMS programado #{prog.id}: solo {registro.get('guardados', 0)} de "
                             f"{registro.get('total', 0)} SMS quedaron en SmsLog ({registro['errores'][0]})", "WARNING")
            log_gui_action("SMS programado ejecutado", programacion=str(prog.id))

        except Exception as exc:
            # Queda en la programación (estado 'error' + mensaje) para verlo en la lista.
            logger_sms.exception(f"❌ Error ejecutando la programación SMS #{prog.id} '{prog.campana}': {exc}")
            log_activity(f"❌ Programación SMS #{prog.id} '{prog.campana}' falló: {exc}", "ERROR")
            try:
                db.session.rollback()
                prog.estado = 'error'
                prog.ultimo_error = str(exc)[:1000]
                prog.fecha_actualizacion = datetime.utcnow()
                db.session.commit()
                _sms_desagendar(prog)
            except Exception:
                logger_sms.exception(f"No se pudo guardar el error de la programación SMS #{prog.id}")




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

    # ---------- RECURRENTE: IntervalTrigger cada 10 min ----------
    elif tipo == 'recurrente':
        
        scheduler.add_job(
            execute_sms_schedule,
            trigger=IntervalTrigger(minutes=10),
            args=[str(prog.id)],
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )
        logger_sms.info(f"🔄 SMS recurrente #{prog.id} agendado cada 10 min")


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
    usuario = (data.get("usuario") or "").strip()
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

        mensajes_validos, _ = aplicar_validaciones(
            rows, plantilla, bq_client, allow_resend=allow_resend
        )
        if not mensajes_validos:
            raise SmsServiceError("La programación no tiene destinatarios válidos.")

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
        )

        from database import ProgramacionSms
        prog = ProgramacionSms.query.get(schedule_id)
        agendar_programacion_sms(prog)

        log_gui_action(
            "SMS programado (simple)",
            programacion=schedule_id,
            fecha=run_date.isoformat(),
        )

        return jsonify({
            "success": True,
            "id": schedule_id,
            "tipo_programacion": "simple",
            "fecha_programada": run_date.isoformat(),
            "total_destinatarios": len(mensajes_validos),
            "message": "SMS programado correctamente.",
        })

    except (ValueError, SmsServiceError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger_sms.exception("Error programando SMS simple")
        return jsonify({"success": False, "message": str(exc)}), 500


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
    usuario = (data.get("usuario") or "").strip()
    allow_resend = bool(data.get("confirmar_reenvio", False))
    hora_inicio = (data.get("hora_inicio") or "").strip()
    fecha_fin = (data.get("fecha_fin") or "").strip() or None

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

        mensajes_validos, _ = aplicar_validaciones(
            rows, plantilla, bq_client, allow_resend=allow_resend
        )
        if not mensajes_validos:
            raise SmsServiceError("La programación no tiene destinatarios válidos.")

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
        )

        from database import ProgramacionSms
        prog = ProgramacionSms.query.get(schedule_id)
        agendar_programacion_sms(prog)

        logger_sms.info(f"✅ SMS recurrente creado: #{schedule_id} (hora={hora_inicio}, fecha_fin={fecha_fin})")

        return jsonify({
            "success": True,
            "id": schedule_id,
            "tipo_programacion": "recurrente",
            "hora_inicio": hora_inicio,
            "fecha_fin": fecha_fin,
            "total_destinatarios": len(mensajes_validos),
            "message": f"SMS recurrente programado para las {hora_inicio}.",
        })

    except (ValueError, SmsServiceError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger_sms.exception("Error programando SMS recurrente")
        return jsonify({"success": False, "message": str(exc)}), 500


# ==================== ARRANQUE SCHEDULER SMS ====================

def _recargar_programaciones_sms():
    with app.app_context():
        cargar_programaciones_sms_al_iniciar()


# Al arrancar (unos segundos después, para no demorar el inicio de la app).
scheduler.add_job(_recargar_programaciones_sms, trigger=DateTrigger(
    run_date=datetime.now(COLOMBIA_TZ) + timedelta(seconds=15)), id="sms_recargar_programaciones",
    replace_existing=True)



@app.route("/api/sms/operadores", methods=["GET"])
def sms_operadores():
    try:
        return jsonify({
            "success": True, "items": OPERADORES_SMS,
            "message": "Lista de operadores obtenida correctamente"
            })
        
    except Exception as exc:
        return jsonify({
            "success": False, "message": str(exc)
            }), 500


@app.route("/api/sms/tipos-por-operador", methods=["GET"])
def sms_tipos_por_operador():
    try:
        from database import MensajeOperacion
        operador = (request.args.get("operador") or "").strip()
        if not operador:
            return jsonify({"success": False, "message": "Se requiere operador"}), 400
        tipos = MensajeOperacion.query.filter(
            MensajeOperacion.Estado == 1,
            (MensajeOperacion.Operador == operador) | (MensajeOperacion.Operadores.contains(operador))
        ).with_entities(MensajeOperacion.Tipo).distinct().order_by(MensajeOperacion.Tipo).all()
        items = [t[0] for t in tipos if t[0]]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/tipos-por-categoria", methods=["GET"])
def sms_tipos_por_categoria():
    try:
        from database import MensajeOperacion
        categoria = (request.args.get("categoria") or "").strip()
        if not categoria:
            return jsonify({"success": False, "message": "Se requiere categoría"}), 400
        tipos = MensajeOperacion.query.filter(
            MensajeOperacion.Estado == 1, MensajeOperacion.Categoria == categoria
        ).filter(MensajeOperacion.Tipo.isnot(None), MensajeOperacion.Tipo != '') \
            .with_entities(MensajeOperacion.Tipo).distinct().order_by(MensajeOperacion.Tipo).all()
        items = [t[0] for t in tipos if t[0]]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/categorias", methods=["GET"])
def sms_categorias():
    try:
        from database import MensajeOperacion
        categorias = MensajeOperacion.query.filter(
            MensajeOperacion.Estado == 1,
            MensajeOperacion.Categoria.isnot(None), MensajeOperacion.Categoria != ''
        ).with_entities(MensajeOperacion.Categoria).distinct().order_by(MensajeOperacion.Categoria).all()
        items = [c[0] for c in categorias if c[0]]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/categorias-por-operador", methods=["GET"])
def sms_categorias_por_operador():
    try:
        from database import MensajeOperacion
        operador = (request.args.get("operador") or "").strip()
        if not operador:
            return jsonify({"success": False, "message": "Se requiere operador"}), 400
        categorias = MensajeOperacion.query.filter(
            MensajeOperacion.Estado == 1,
            (MensajeOperacion.Operador == operador) | (MensajeOperacion.Operadores.contains(operador)),
            MensajeOperacion.Categoria.isnot(None), MensajeOperacion.Categoria != ''
        ).with_entities(MensajeOperacion.Categoria).distinct().order_by(MensajeOperacion.Categoria).all()
        items = [c[0] for c in categorias if c[0]]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/tipos", methods=["GET"])
def sms_tipos_flexibles():
    try:
        from database import MensajeOperacion
        operador = (request.args.get("operador") or "").strip()
        categoria = (request.args.get("categoria") or "").strip()
        query = MensajeOperacion.query.filter(MensajeOperacion.Estado == 1)
        if operador:
            query = query.filter((MensajeOperacion.Operador == operador) | (MensajeOperacion.Operadores.contains(operador)))
        if categoria:
            query = query.filter(MensajeOperacion.Categoria == categoria)
        tipos = query.filter(MensajeOperacion.Tipo.isnot(None), MensajeOperacion.Tipo != '') \
            .with_entities(MensajeOperacion.Tipo).distinct().order_by(MensajeOperacion.Tipo).all()
        items = [t[0] for t in tipos if t[0]]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/mensajes-operacion", methods=["GET"])
def sms_mensajes_operacion_flexible():
    try:
        from database import MensajeOperacion
        operador = (request.args.get("operador") or "").strip()
        tipo = (request.args.get("tipo") or "").strip()
        categoria = (request.args.get("categoria") or "").strip()
        query = MensajeOperacion.query.filter(MensajeOperacion.Estado == 1)
        if operador:
            query = query.filter((MensajeOperacion.Operador == operador) | (MensajeOperacion.Operadores.contains(operador)))
        if tipo:
            query = query.filter(MensajeOperacion.Tipo == tipo)
        if categoria:
            query = query.filter(MensajeOperacion.Categoria == categoria)
        mensajes = query.order_by(MensajeOperacion.Operador, MensajeOperacion.Tipo, MensajeOperacion.id.desc()).all()
        items = [m.to_dict() for m in mensajes]
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


# ==================== SMS - CRUD MENSAJES (SQLite) ====================

@app.route("/config-mensajes")
def config_mensajes():
    return render_template("config_mensajes.html")


@app.route("/api/sms/mensajes", methods=["GET"])
def api_mensajes_list():
    try:
        from database import MensajeOperacion
        mensajes = MensajeOperacion.query.order_by(MensajeOperacion.Operador, MensajeOperacion.Tipo, MensajeOperacion.id.desc()).all()
        return jsonify({"success": True, "items": [m.to_dict() for m in mensajes]})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/mensajes", methods=["POST"])
def api_mensajes_create():
    try:
        from database import MensajeOperacion, db
        data = request.get_json() or {}
        operador = (data.get("operador") or "").strip()
        mensaje_texto = (data.get("mensaje") or "").strip()
        tipo = (data.get("tipo") or "").strip()
        operadores = (data.get("operadores") or "").strip()
        categoria = (data.get("categoria") or "").strip()
        estado = int(data.get("estado", 1))
        if not operador or not mensaje_texto:
            return jsonify({"success": False, "message": "Operador y mensaje son obligatorios"}), 400
        nuevo = MensajeOperacion(Operador=operador, Mensaje=mensaje_texto, Tipo=tipo, Categoria=categoria, Operadores=operadores, Estado=estado)
        db.session.add(nuevo)
        db.session.commit()
        return jsonify({"success": True, "message": "Mensaje creado", "item": nuevo.to_dict()})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/mensajes/<int:mensaje_id>", methods=["PUT"])
def api_mensajes_update(mensaje_id):
    try:
        from database import MensajeOperacion, db
        mensaje = MensajeOperacion.query.get(mensaje_id)
        if not mensaje:
            return jsonify({"success": False, "message": "Mensaje no encontrado"}), 404
        data = request.get_json() or {}
        if "operador" in data: mensaje.Operador = data["operador"].strip()
        if "mensaje" in data: mensaje.Mensaje = data["mensaje"].strip()
        if "tipo" in data: mensaje.Tipo = data["tipo"].strip()
        if "categoria" in data: mensaje.Categoria = data["categoria"].strip()
        if "estado" in data: mensaje.Estado = int(data["estado"])
        if "operadores" in data: mensaje.Operadores = data["operadores"].strip()
        db.session.commit()
        return jsonify({"success": True, "message": "Mensaje actualizado", "item": mensaje.to_dict()})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/mensajes/<int:mensaje_id>", methods=["DELETE"])
def api_mensajes_delete(mensaje_id):
    try:
        from database import MensajeOperacion, db
        mensaje = MensajeOperacion.query.get(mensaje_id)
        if not mensaje:
            return jsonify({"success": False, "message": "Mensaje no encontrado"}), 404
        db.session.delete(mensaje)
        db.session.commit()
        return jsonify({"success": True, "message": "Mensaje eliminado"})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/sms/mensajes/<int:mensaje_id>/toggle", methods=["POST"])
def api_mensajes_toggle(mensaje_id):
    try:
        from database import MensajeOperacion, db
        mensaje = MensajeOperacion.query.get(mensaje_id)
        if not mensaje:
            return jsonify({"success": False, "message": "Mensaje no encontrado"}), 404
        mensaje.Estado = 0 if mensaje.Estado == 1 else 1
        db.session.commit()
        return jsonify({"success": True, "message": f"Mensaje {'activado' if mensaje.Estado == 1 else 'desactivado'}", "estado": mensaje.Estado})
    except Exception as exc:
        return jsonify({"success": False, "message": str(exc)}), 500





# ==================== EMAIL - PÁGINA ====================

@app.route("/config-email")
def config_email():
    return render_template("config_email.html")


# ==================== EMAIL - CAMPOS OBLIGATORIOS ====================

# Programaciones de email (en BigQuery; las de SMS están en SQLite).
PROGRAMACION_EMAIL_TABLE = "capable-arbor-209819.Temporal.ProgramacionEmail"


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
    if con_fecha and not (data.get("fecha_programada") or "").strip():
        faltan.append("fecha y hora de envío")
    if con_fecha and (data.get("tipo") or "simple") not in ("simple", "recurrente"):
        faltan.append("tipo (simple o recurrente)")
    return faltan


def _error_columnas_email(exc):
    """Mensaje claro si a ProgramacionEmail le faltan las columnas tipo o ultimo_error (ver README)."""
    if "Unrecognized name" in str(exc) or "no such field" in str(exc).lower():
        return ("A la tabla ProgramacionEmail le faltan columnas. Corre en BigQuery: "
                f"ALTER TABLE `{PROGRAMACION_EMAIL_TABLE}` ADD COLUMN IF NOT EXISTS tipo STRING, "
                "ADD COLUMN IF NOT EXISTS ultimo_error STRING;")
    return str(exc)


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
        return jsonify({"success": False, "message": str(exc)}), 500


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
        email_config = (CONFIG or load_config()).get("email", {})
        if not email_config.get("api_key"):
            return [], "No se revisaron los campos de la cuenta de email: falta email.api_key en config.json."
        campos_api = EmailClient(email_config["api_key"]).obtener_campos_personalizados()
        if not campos_api.get("success"):
            return [], "No se pudieron leer los campos personalizados de la cuenta de email."
        mapeo = construir_mapeo_campos(email_config, campos_api.get("data", []))
        return variables_sin_campo(plantilla, asunto, mapeo), None
    except Exception as exc:
        return [], f"No se pudieron revisar los campos de la cuenta de email: {exc}"


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
            filtro = filtrar_destinatarios(bq_client, rows, confirmar_reenvio)
        except EmailServiceError as exc:
            return jsonify({"success": False, "message": str(exc)}), 400
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
        return jsonify({"success": False, "message": str(exc)}), 500


# ==================== EMAIL - ENVIAR ====================

@app.route("/api/email/enviar", methods=["POST"])
def email_send():
    """Envía emails ya (la lógica vive en services.email_service.enviar_campana_email)."""
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    asunto = (data.get("asunto") or "").strip()
    campana_nombre = (data.get("campana") or "").strip()
    usuario = (data.get("usuario") or "").strip()
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
        resultado = enviar_campana_email(
            bq_client, rows, plantilla, asunto, _campana_con_plantilla(campana_nombre, plantilla_guardada),
            usuario, reply_email, confirmar_reenvio, (CONFIG or load_config()).get("email", {}),
        )
        _registrar_uso_plantilla_email(plantilla_guardada)
        log_gui_action("Envio Email", campana_id=resultado["campana_id"], enviados=resultado["enviados"])
        return jsonify({"success": True, **resultado, "message": f"{resultado['enviados']} emails enviados"})
    except (EmailServiceError, EmailClientError) as exc:
        logger_email.error(f"❌ Envío de email '{campana_nombre}' rechazado: {exc}")
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger_email.exception(f"❌ Error enviando los emails de la campaña '{campana_nombre}'")
        return jsonify({"success": False, "message": str(exc)}), 500

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
        return jsonify({"success": False, "message": str(exc)}), 500


# ==================== EMAIL - PROGRAMAR ====================

@app.route("/api/email/programar", methods=["POST"])
def email_schedule():
    data = request.get_json(silent=True) or {}
    query = (data.get("query") or "").strip()
    plantilla = (data.get("plantilla") or "").strip()
    asunto = (data.get("asunto") or "").strip()
    campana_nombre = (data.get("campana") or "").strip()
    usuario = (data.get("usuario") or "").strip()
    reply_email = (data.get("reply_email") or "").strip()
    fecha_programada = (data.get("fecha_programada") or "").strip()
    tipo = (data.get("tipo") or "simple").strip()

    faltan = _faltantes_email(data, con_fecha=True)
    if faltan:
        return _respuesta_faltantes(faltan)

    try:
        run_date = datetime.fromisoformat(fecha_programada)
        if run_date.replace(tzinfo=None) <= datetime.now(COLOMBIA_TZ).replace(tzinfo=None):
            raise EmailServiceError("La fecha programada debe ser futura.")

        rows, error_response = _get_sms_rows(query)
        if error_response:
            return error_response

        from services.email_service import validar_variables_plantilla
        validacion = validar_variables_plantilla(rows, plantilla, asunto)
        if not validacion["valido"]:
            return jsonify({"success": False, "message": validacion["error"], "validacion": validacion}), 400
        sin_campo, _ = _variables_sin_campo_email(plantilla, asunto)
        if sin_campo:
            return jsonify({"success": False, "message":
                f"Estas variables no tienen campo personalizado en la cuenta de email y el envío fallaría: "
                f"{', '.join(sin_campo)}. Créalas en la cuenta o quítalas de la plantilla."}), 400

        plantilla_guardada = _plantilla_email_del_envio(data)
        schedule_id = str(uuid4())
        # Hora de Colombia (sin zona), como el resto de programaciones.
        if run_date.tzinfo is not None:
            run_date = run_date.astimezone(COLOMBIA_TZ).replace(tzinfo=None)
        now = datetime.now(timezone.utc).isoformat()

        from google.cloud import bigquery
        job_config = bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", schedule_id),
            bigquery.ScalarQueryParameter("fecha_prog", "TIMESTAMP", run_date.isoformat()),
            bigquery.ScalarQueryParameter("consulta", "STRING", query),
            bigquery.ScalarQueryParameter("plantilla_param", "STRING", plantilla),
            bigquery.ScalarQueryParameter("asunto_param", "STRING", asunto),
            bigquery.ScalarQueryParameter("campana", "STRING", _campana_con_plantilla(campana_nombre, plantilla_guardada) or ""),
            bigquery.ScalarQueryParameter("usuario", "STRING", usuario or ""),
            bigquery.ScalarQueryParameter("reply", "STRING", reply_email or ""),
            bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter("tipo", "STRING", tipo),
        ])

        insert_sql = f"""
            INSERT INTO `{PROGRAMACION_EMAIL_TABLE}`
            (id, fecha_programada, consulta_sql, plantilla, asunto, campana, estado, usuario, reply_email, fecha_creacion, fecha_actualizacion, tipo)
            VALUES (@id, @fecha_prog, @consulta, @plantilla_param, @asunto_param, @campana, 'pendiente', @usuario, @reply, @now, @now, @tipo)
        """
        try:
            bq_client.query(insert_sql, job_config=job_config).result()
        except Exception as exc:
            raise EmailServiceError(_error_columnas_email(exc))

        scheduler.add_job(
            execute_email_schedule, trigger="date", run_date=run_date,
            args=[schedule_id], id=f"email_programado_{schedule_id}", replace_existing=True,
            misfire_grace_time=3600,
        )

        _registrar_uso_plantilla_email(plantilla_guardada)
        return jsonify({"success": True, "id": schedule_id, "fecha_programada": run_date.isoformat(), "tipo": tipo,
                        "message": "Envío programado." if tipo == "simple" else "Envío recurrente programado (todos los días a esa hora)."})
    except (ValueError, EmailServiceError) as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        logger_email.exception("❌ Error programando el email")
        return jsonify({"success": False, "message": str(exc)}), 500




def _email_programacion_estado(client, schedule_id, nuevo, solo_si=None):
    """
    Cambia el estado de una programación de email. Con solo_si, solo si está en
    ese estado; devuelve cuántas filas cambió (para que una sola app la tome).
    """
    from google.cloud import bigquery
    condicion = " AND estado = @solo_si" if solo_si else ""
    params = [bigquery.ScalarQueryParameter("id", "STRING", schedule_id),
              bigquery.ScalarQueryParameter("estado", "STRING", nuevo)]
    if solo_si:
        params.append(bigquery.ScalarQueryParameter("solo_si", "STRING", solo_si))
    job = client.query(f"""
        UPDATE `{PROGRAMACION_EMAIL_TABLE}`
        SET estado = @estado, fecha_actualizacion = CURRENT_TIMESTAMP()
        WHERE id = @id{condicion}
    """, job_config=bigquery.QueryJobConfig(query_parameters=params))
    job.result()
    return job.num_dml_affected_rows or 0


def _email_programacion_error(client, schedule_id, mensaje):
    """
    Deja la programación en estado 'error' con el mensaje en ultimo_error, para
    verlo en la lista. Si la tabla aún no tiene la columna ultimo_error (ver
    README), solo cambia el estado.
    """
    from google.cloud import bigquery
    try:
        client.query(f"""
            UPDATE `{PROGRAMACION_EMAIL_TABLE}`
            SET estado = 'error', ultimo_error = @error, fecha_actualizacion = CURRENT_TIMESTAMP()
            WHERE id = @id
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", schedule_id),
            bigquery.ScalarQueryParameter("error", "STRING", str(mensaje)[:1000])])).result()
    except Exception as exc:
        logger_email.warning(f"⚠️ No se guardó el mensaje de error de la programación {schedule_id} "
                             f"(¿falta la columna ultimo_error en ProgramacionEmail?): {exc}")
        _email_programacion_estado(client, schedule_id, "error")


def _email_proxima_fecha(fecha, ahora):
    """Siguiente día (a la misma hora) posterior a ahora. Ambas en hora de Colombia, sin zona."""
    fecha = fecha.replace(tzinfo=None)
    while fecha <= ahora:
        fecha += timedelta(days=1)
    return fecha


def _email_reagendar_manana(client, schedule_id, fecha_actual):
    """Recurrente: tras enviar, queda pendiente para el día siguiente a la misma hora."""
    from google.cloud import bigquery
    siguiente = _email_proxima_fecha(fecha_actual, datetime.now(COLOMBIA_TZ).replace(tzinfo=None))
    client.query(f"""
        UPDATE `{PROGRAMACION_EMAIL_TABLE}`
        SET estado = 'pendiente', fecha_programada = @fecha, ultimo_error = NULL,
            fecha_actualizacion = CURRENT_TIMESTAMP()
        WHERE id = @id
    """, job_config=bigquery.QueryJobConfig(query_parameters=[
        bigquery.ScalarQueryParameter("id", "STRING", schedule_id),
        bigquery.ScalarQueryParameter("fecha", "TIMESTAMP", siguiente.isoformat())])).result()
    scheduler.add_job(
        execute_email_schedule, trigger=DateTrigger(run_date=COLOMBIA_TZ.localize(siguiente)),
        args=[schedule_id], id=f"email_programado_{schedule_id}", replace_existing=True,
        misfire_grace_time=3600,
    )


def execute_email_schedule(schedule_id: str):
    """Job email_programado_<id>: envía una programación de email con los mismos filtros que el envío inmediato."""
    from google.cloud import bigquery
    if not _asegurar_bq():
        logger_email.error(f"❌ Email programado {schedule_id}: no se pudo conectar a BigQuery")
        return
    client = bq_client
    try:
        # Reservarla: si otra app ya la tomó (o se canceló), no se envía dos veces.
        if _email_programacion_estado(client, schedule_id, "enviando", solo_si="pendiente") != 1:
            logger.info(f"Email programado {schedule_id}: ya no está pendiente, no se envía")
            return
        prog = dict(next(iter(client.query(
            f"SELECT * FROM `{PROGRAMACION_EMAIL_TABLE}` WHERE id = @id",
            job_config=bigquery.QueryJobConfig(query_parameters=[
                bigquery.ScalarQueryParameter("id", "STRING", schedule_id)])).result())).items())

        fetched = fetch_sms_query_rows(client, prog["consulta_sql"])
        if not fetched.get("success"):
            raise EmailServiceError(fetched.get("message", "Error en la consulta"))
        resultado = enviar_campana_email(
            client, fetched["rows"], prog["plantilla"], prog.get("asunto") or "",
            prog.get("campana") or "", prog.get("usuario") or "sistema", prog.get("reply_email") or "",
            False, (CONFIG or load_config()).get("email", {}),
        )
        if prog.get("tipo") == "recurrente":
            _email_reagendar_manana(client, schedule_id, prog["fecha_programada"])
        else:
            _email_programacion_estado(client, schedule_id, "enviado")
        log_activity(f"📧 Email programado '{prog.get('campana') or schedule_id}': "
                     f"{resultado['enviados']} enviados (campaña {resultado['campana_id']}).")
    except Exception as exc:
        logger_email.exception(f"❌ Error ejecutando el email programado {schedule_id}: {exc}")
        log_activity(f"❌ Email programado {schedule_id} falló: {exc}", "ERROR")
        try:
            _email_programacion_error(client, schedule_id, exc)
        except Exception:
            logger_email.exception(f"No se pudo marcar con error la programación de email {schedule_id}")


def cargar_programaciones_email_al_iniciar():
    """
    Vuelve a agendar las programaciones de email pendientes (los jobs se pierden
    al reiniciar). Si ya pasó la hora: menos de 1 hora, se envía en 5 segundos;
    más de 1 hora, queda fallida. En la copia de prueba no se recargan: la tabla
    es compartida y la app de producción las enviaría dos veces.
    """
    from modo_prueba import es_copia_prueba
    if es_copia_prueba():
        logger_email.warning("🧪 Copia de prueba: no se recargan las programaciones de email (las envía la app de producción).")
        return
    if not _asegurar_bq():
        logger_email.error("❌ No se pudieron recargar las programaciones de email: sin BigQuery")
        return
    client = bq_client
    try:
        pendientes = list(client.query(
            f"SELECT * FROM `{PROGRAMACION_EMAIL_TABLE}` WHERE estado = 'pendiente'"
        ).result())
        ahora = datetime.now(COLOMBIA_TZ)
        for fila in pendientes:
            # Se guardó la hora de Colombia como si fuera UTC: se toma tal cual, como hora de Colombia.
            fecha = COLOMBIA_TZ.localize(fila["fecha_programada"].replace(tzinfo=None))
            if fila.get("tipo") == "recurrente" and ahora - fecha > timedelta(hours=1):
                # Recurrente con la app apagada a su hora: sigue al día siguiente.
                _email_reagendar_manana(client, fila["id"], fila["fecha_programada"])
                log_activity(f"⚠️ Email recurrente '{fila['campana'] or fila['id']}' no salió el "
                             f"{fecha:%Y-%m-%d %H:%M} (app apagada): sigue mañana a la misma hora.", "WARNING")
                continue
            if ahora - fecha > timedelta(hours=1):
                _email_programacion_error(client, fila["id"], "No se envió: pasó más de 1 hora con la app apagada.")
                log_activity(f"⚠️ Email programado '{fila['campana'] or fila['id']}' para {fecha:%Y-%m-%d %H:%M} "
                             f"no se envió (pasó más de 1 hora con la app apagada): queda fallido.", "WARNING")
                continue
            scheduler.add_job(
                execute_email_schedule, trigger=DateTrigger(run_date=max(fecha, ahora + timedelta(seconds=5))),
                args=[fila["id"]], id=f"email_programado_{fila['id']}", replace_existing=True,
                misfire_grace_time=3600,
            )
        logger.info(f"🔁 Programaciones de email pendientes revisadas: {len(pendientes)}")
    except Exception:
        logger_email.exception("❌ Error recargando las programaciones de email")


# Al arrancar (unos segundos después, para no demorar el inicio de la app).
scheduler.add_job(cargar_programaciones_email_al_iniciar, trigger=DateTrigger(
    run_date=datetime.now(COLOMBIA_TZ) + timedelta(seconds=15)), id="email_recargar_programaciones",
    replace_existing=True)


# ==================== EMAIL - PROGRAMACIONES ====================

@app.route("/api/email/programaciones", methods=["GET"])
def email_programaciones():
    try:
        if not _asegurar_bq():
            return jsonify({"success": False, "message": "No se pudo conectar a BigQuery"}), 500
        client = bq_client

        # SELECT *: incluye ultimo_error cuando la tabla ya tiene esa columna.
        query = f"""
            SELECT * FROM `{PROGRAMACION_EMAIL_TABLE}`
            ORDER BY fecha_programada DESC LIMIT 100
        """
        result = client.query(query).result()
        items = []
        for row in result:
            item = {}
            for key, value in row.items():
                if value is None:
                    item[key] = None
                elif hasattr(value, 'strftime'):
                    item[key] = value.strftime('%Y-%m-%d %H:%M:%S')
                elif hasattr(value, 'isoformat'):
                    item[key] = value.isoformat()
                else:
                    item[key] = value
            items.append(item)
        return jsonify({"success": True, "items": items})
    except Exception as exc:
        logger_email.exception("❌ Error listando las programaciones de email")
        return jsonify({"success": False, "message": str(exc)}), 500


@app.route("/api/email/programaciones/<schedule_id>", methods=["PATCH"])
def email_editar_programacion(schedule_id):
    """
    Edita una programación de email (nombre, consulta, asunto, mensaje, reply-to,
    responsable, fecha y hora). Solo si no se está enviando ni se envió. Revisa
    la consulta en BigQuery (dry run), la deja pendiente y reemplaza su job.
    """
    from google.cloud import bigquery
    from consulta_segura import ConsultaNoPermitida, verificar_consulta_lectura
    from bigquery import mensaje_error_bigquery

    data = request.get_json(silent=True) or {}
    faltan = _faltantes_email(data, con_fecha=True)
    if faltan:
        return _respuesta_faltantes(faltan)
    try:
        run_date = datetime.fromisoformat(data["fecha_programada"].strip())
    except ValueError:
        return jsonify({"success": False, "message": f"Fecha y hora de envío inválida: {data['fecha_programada']}"}), 400
    if run_date.tzinfo is not None:
        run_date = run_date.astimezone(COLOMBIA_TZ).replace(tzinfo=None)
    if run_date <= datetime.now(COLOMBIA_TZ).replace(tzinfo=None):
        return jsonify({"success": False, "message": "La fecha y hora de envío debe ser futura."}), 400
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo conectar a BigQuery"}), 500
    try:
        verificar_consulta_lectura(bq_client, data["query"])
    except ConsultaNoPermitida as exc:
        return jsonify({"success": False, "message": str(exc)}), 400
    except Exception as exc:
        return jsonify({"success": False, "message": mensaje_error_bigquery(exc)}), 400

    try:
        job = bq_client.query(f"""
            UPDATE `{PROGRAMACION_EMAIL_TABLE}`
            SET campana = @campana, consulta_sql = @consulta, asunto = @asunto, plantilla = @plantilla,
                reply_email = @reply, usuario = @usuario, fecha_programada = @fecha, tipo = @tipo,
                estado = 'pendiente', ultimo_error = NULL, fecha_actualizacion = CURRENT_TIMESTAMP()
            WHERE id = @id AND estado NOT IN ('enviando', 'enviado')
        """, job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("id", "STRING", schedule_id),
            bigquery.ScalarQueryParameter("campana", "STRING", data["campana"].strip()),
            bigquery.ScalarQueryParameter("consulta", "STRING", data["query"].strip()),
            bigquery.ScalarQueryParameter("asunto", "STRING", data["asunto"].strip()),
            bigquery.ScalarQueryParameter("plantilla", "STRING", data["plantilla"].strip()),
            bigquery.ScalarQueryParameter("reply", "STRING", (data.get("reply_email") or "").strip()),
            bigquery.ScalarQueryParameter("usuario", "STRING", (data.get("usuario") or "").strip()),
            # Hora de Colombia (sin zona), como al programar.
            bigquery.ScalarQueryParameter("fecha", "TIMESTAMP", run_date.isoformat()),
            bigquery.ScalarQueryParameter("tipo", "STRING", (data.get("tipo") or "simple").strip()),
        ]))
        job.result()
        if not job.num_dml_affected_rows:
            return jsonify({"success": False, "message": "No existe o ya se envió (o se está enviando): no se puede editar."}), 409
    except Exception as exc:
        logger_email.exception(f"❌ Error editando la programación de email {schedule_id}")
        return jsonify({"success": False, "message": _error_columnas_email(exc)}), 500

    scheduler.add_job(
        execute_email_schedule, trigger=DateTrigger(run_date=COLOMBIA_TZ.localize(run_date)),
        args=[schedule_id], id=f"email_programado_{schedule_id}", replace_existing=True,
        misfire_grace_time=3600,
    )
    log_activity(f"✏️ Email programado {schedule_id} editado: {data['campana'].strip()} para {run_date:%Y-%m-%d %H:%M}")
    return jsonify({"success": True, "message": "Programación actualizada.", "fecha_programada": run_date.isoformat()})


@app.route("/api/email/cancelar/<schedule_id>", methods=["POST"])
def email_cancelar_programacion(schedule_id):
    try:
        if not _asegurar_bq():
            return jsonify({"success": False, "message": "No se pudo conectar a BigQuery"}), 500
        client = bq_client
        # Solo si sigue pendiente: si ya se está enviando o ya salió, no se puede cancelar.
        if _email_programacion_estado(client, schedule_id, "cancelado", solo_si="pendiente") != 1:
            return jsonify({"success": False, "message": "Esta programación ya no está pendiente: no se puede cancelar."}), 409
        try:
            scheduler.remove_job(f"email_programado_{schedule_id}")
        except Exception:
            pass
        log_activity(f"🗑️ Email programado {schedule_id} cancelado")
        return jsonify({"success": True, "message": "Programación cancelada"})
    except Exception as exc:
        logger_email.exception(f"❌ Error cancelando la programación de email {schedule_id}")
        return jsonify({"success": False, "message": str(exc)}), 500


# ==================== EMAIL - PLANTILLAS ====================
# Plantillas guardadas (PlantillaEmail): asunto + HTML con {{variables}}. Una
# plantilla ya usada no cambia su contenido: al editarla se guarda como "<nombre> v2".

@app.route("/email/plantillas", methods=["GET"])
def email_plantillas_pagina():
    return render_template("email_plantillas.html")


@app.route("/api/email/plantillas", methods=["GET"])
def email_plantillas_listar():
    from database import PlantillaEmail
    consulta = PlantillaEmail.query
    if request.args.get("todas") != "1":
        consulta = consulta.filter_by(activa=True)
    plantillas = consulta.order_by(PlantillaEmail.activa.desc(), PlantillaEmail.nombre).all()
    return jsonify({"success": True, "plantillas": [p.a_dict() for p in plantillas]})


def _leer_plantilla_formulario():
    """(nombre, asunto, html, usuario, errores) del JSON recibido."""
    from services.email_service import errores_plantilla
    data = request.get_json(silent=True) or {}
    nombre = re.sub(r"\s+", " ", str(data.get("nombre") or "")).strip()
    asunto = str(data.get("asunto") or "").strip()
    html = str(data.get("html") or "").strip()
    usuario = str(data.get("usuario") or "").strip() or None
    return nombre, asunto, html, usuario, errores_plantilla(nombre, asunto, html)


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
    nombre, asunto, html, usuario, errores = _leer_plantilla_formulario()
    if not errores and _nombre_plantilla_repetido(nombre):
        errores.append(f"Ya existe una plantilla llamada '{nombre}'.")
    if errores:
        return jsonify({"success": False, "message": " ".join(errores)}), 400
    plantilla = PlantillaEmail(nombre=nombre, asunto=asunto, html=html, usuario=usuario, activa=True)
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
    nombre, asunto, html, usuario, errores = _leer_plantilla_formulario()
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
        nueva = PlantillaEmail(nombre=nombre_libre(nombre, _nombres_plantillas(), "v"),
                               asunto=asunto, html=html, usuario=usuario, activa=True)
        db.session.add(nueva)
        db.session.commit()
        log_activity(f"📧 Plantilla de email '{plantilla.nombre}' guardada como nueva: {nueva.nombre}")
        return jsonify({"success": True, "plantilla": nueva.a_dict(), "nueva": True})
    if _nombre_plantilla_repetido(nombre, excepto_id=plantilla.id):
        return jsonify({"success": False, "message": f"Ya existe una plantilla llamada '{nombre}'."}), 400
    plantilla.nombre, plantilla.asunto, plantilla.html = nombre, asunto, html
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
    usuario = str((request.get_json(silent=True) or {}).get("usuario") or "").strip() or original.usuario
    copia = PlantillaEmail(nombre=nombre_libre(original.nombre, _nombres_plantillas(), "copia "),
                           asunto=original.asunto, html=original.html, usuario=usuario, activa=True)
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

@app.route("/api/sms/ver_programaciones")
def sms_programaciones_pagina():
    """Página para ver todas las programaciones de SMS."""
    return render_template("sms/programaciones.html")


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
        'confirmar_reenvio': prog.confirmar_reenvio,
        'fecha_programada': prog.fecha_programada.isoformat() if prog.fecha_programada else None,
        'hora_inicio': prog.hora_inicio,
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
            return jsonify({"success": False, "message": str(exc)}), 400
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
    if "usuario" in data:
        prog.usuario = (data.get("usuario") or "").strip()
    if "confirmar_reenvio" in data:
        prog.confirmar_reenvio = bool(data.get("confirmar_reenvio"))
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
        return jsonify({"success": False, "message": str(e)}), 500

    agendar_programacion_sms(prog)
    log_gui_action("Programación SMS editada", programacion=str(prog.id), tipo=tipo)
    return jsonify({"success": True, "data": _prog_sms_a_dict(prog)}), 200


@app.route("/api/sms/programaciones/<int:prog_id>", methods=["DELETE"])
def api_delete_programacion(prog_id):
    from database import ProgramacionSms
    prog = db.session.get(ProgramacionSms, prog_id)
    if not prog:
        return jsonify({"success": False, "error": "No encontrada"}), 404
    logger_sms.info(f"🗑️ Eliminando programación de SMS #{prog_id}")
    try:
        _sms_desagendar(prog)
        db.session.delete(prog)
        db.session.commit()
        return jsonify({"success": True}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


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
        return jsonify({"success": True}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500


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
    try:
        db.session.commit()
        # Simple con fecha ya pasada: agendar_programacion_sms la corre en 5 segundos.
        agendar_programacion_sms(prog)
        return jsonify({"success": True}), 200
    except Exception as e:
        db.session.rollback()
        return jsonify({"success": False, "error": str(e)}), 500

# ══════════════════════════════════════════════════════════════════════


# ==================================================
# 📊 DASHBOARD SMS
# ==================================================

@app.route("/dashboards", methods=["GET"])
def dashboards_page():
    """Página del dashboard"""
    return render_template("dashboard.html")


def _infobip_config():
    infobip = (CONFIG or load_config()).get("infobip", {})
    return infobip.get("api_key", "").strip(), infobip.get("base_url", "").strip()


@app.route("/api/dashboards", methods=["GET"])
def get_dashboards():
    """
    Dashboard de SMS (cálculos en dashboard_sms.py).
    Fuente por defecto: todos los SMS de la cuenta de Infobip, copiados a
    BigQuery (Temporal.SmsInfobip) por el job sms_infobip_sync, con
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
        return jsonify({"success": False, "message": str(exc)}), 500


def _filtros_detalle_infobip():
    a = request.args
    return dict(periodo=a.get("periodo") or "hoy", desde=a.get("desde") or None, hasta=a.get("hasta") or None,
                bulk_id=a.get("bulk_id") or "", estado=a.get("estado") or "", operador=a.get("operador") or "",
                error=a.get("error") or "", buscar=a.get("buscar") or "")


@app.route("/api/dashboards/mensajes", methods=["GET"])
def dashboards_mensajes():
    """SMS de Infobip (tabla SmsInfobip) filtrados (bulk_id, estado, operador, error, buscar), de a 500 por página."""
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
    """CSV (;) con los SMS de Infobip (tabla SmsInfobip) que cumplen los filtros (máximo 300.000)."""
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
    con sus estados finales): copian los SMS de Infobip a Temporal.SmsInfobip.
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
    infobip = (CONFIG or load_config()).get("infobip", {})
    api_key, base_url = infobip.get("api_key", "").strip(), infobip.get("base_url", "").strip()
    if not api_key or not base_url:
        return jsonify({"success": False, "message": "Falta la configuración de Infobip en config.json."}), 400
    if not _asegurar_bq():
        return jsonify({"success": False, "message": "No se pudo inicializar BigQuery."}), 500
    try:
        resultado = actualizar_estados_pendientes(api_key=api_key, base_url=base_url, client=bq_client)
        from dashboard_sms import _cache
        _cache.clear()
        log_activity(f"📨 Estados SMS actualizados a mano: {resultado}")
        return jsonify({**resultado, "success": bool(resultado.get("success"))})
    except Exception as exc:
        logger.exception("Error actualizando estados SMS")
        return jsonify({"success": False, "message": str(exc)}), 500
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
        logger.error(f"❌ Error obteniendo detalles: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({
            "success": False,
            "message": str(e)
        }), 500

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
