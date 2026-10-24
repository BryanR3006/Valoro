import json
import logging
from logging.handlers import RotatingFileHandler
import os
import shutil
import threading
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path

import pytz
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from consola import LOGGER_ACTIVIDAD, ArchivoRotativo, FiltroClaro, FormatoClaro
from database import db, init_db  # noqa: F401  (db se importa desde backend en app.py)
from general_params import (
    get_console_message_interval_seconds,
    get_log_retention_hours,
)

# Nombre de la aplicación: se muestra en la interfaz (layout) desde aquí.
APP_NOMBRE = "Valoro"

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / "uploads"
DOWNLOAD_FOLDER = BASE_DIR / "downloads"
LOG_FILE = BASE_DIR / "execution_log.txt"
CONFIG_FILE = BASE_DIR / "config.json"
CONSOLE_MESSAGE_JOB_ID = "console_message_heartbeat"
LOG_MANTENIMIENTO_JOB_ID = "mantenimiento_logs"

# Logs por área: errores y eventos de SMS y de email, cada uno en su archivo.
LOGS_DIR = BASE_DIR / "logs"
SMS_LOG_FILE = LOGS_DIR / "sms.log"
EMAIL_LOG_FILE = LOGS_DIR / "email.log"
LOGGERS_SMS = ("sms", "services.sms_service", "services.sms_excel", "infobip_sync", "infobip_logs")
LOGGERS_EMAIL = ("email", "services.email_service", "services.email_client")

CONFIG = {}

def load_config():
    global CONFIG
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            CONFIG = json.load(f)
            logger.debug(f"Configuración cargada desde {CONFIG_FILE}")
    except FileNotFoundError:
        CONFIG = {}
        logger.warning(f"No se encontró {CONFIG_FILE}. Usando configuración vacía.")
    except Exception as e:
        CONFIG = {}
        logger.error(f"Error cargando {CONFIG_FILE}: {e}")
    return CONFIG




# Logging: tablero (memoria) = actividad en vivo; archivo = solo ejecución de tareas.
LOG_FORMAT = "%(asctime)s - %(levelname)s - %(message)s"
LOG_DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_MAX_LINES = 3000
# app_log.txt: todo lo que sale en consola; 5 MB por archivo, 10 respaldos
# (app_log.txt.1 ... .10), ~50 MB en total.
APP_LOG_FILE = BASE_DIR / "app_log.txt"
APP_LOG_MAX_BYTES = 5 * 1024 * 1024
APP_LOG_BACKUPS = 10
ACTIVITY_LOG_MAX_LINES = 300
_log_io_lock = threading.Lock()
_activity_log: deque[str] = deque(maxlen=ACTIVITY_LOG_MAX_LINES)
_activity_lock = threading.Lock()


# ============================================================
# NUEVO: ActivityLogHandler para capturar logs automáticamente
# ============================================================
class ActivityLogHandler(logging.Handler):
    """Envía los logs al panel del dashboard mediante _activity_log."""

    def emit(self, record):
        try:
            message = self.format(record)

            with _activity_lock:
                _activity_log.append(message)

        except Exception:
            self.handleError(record)


def _configure_logging() -> None:
    formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # Avisos de librerías (p. ej. LegacyAPIWarning de SQLAlchemy) al log, no a la consola.
    logging.captureWarnings(True)

    # -----------------------------
    # Consola y Actividad en vivo: solo las líneas claras de actividad
    # (consola.py). Lo técnico va a app_log.txt y logs/sms.log, logs/email.log.
    # -----------------------------
    consolas = [h for h in root.handlers
                if type(h) is logging.StreamHandler or isinstance(h, ActivityLogHandler)]
    if not any(type(h) is logging.StreamHandler for h in consolas):
        consolas.append(logging.StreamHandler())
    if not any(isinstance(h, ActivityLogHandler) for h in consolas):
        consolas.append(ActivityLogHandler())
    for handler in consolas:
        handler.setFormatter(FormatoClaro())
        if not any(isinstance(f, FiltroClaro) for f in handler.filters):
            handler.addFilter(FiltroClaro())
        if handler not in root.handlers:
            root.addHandler(handler)

    # -----------------------------
    # Archivo con todo lo de la consola (programaciones, envíos, errores).
    # El tablero solo guarda en memoria las últimas líneas (~1 hora) y se
    # borra al reiniciar; este archivo queda como historial. Rota por tamaño.
    # -----------------------------
    if not any(
        isinstance(h, RotatingFileHandler)
        and Path(h.baseFilename).resolve() == APP_LOG_FILE.resolve()
        for h in root.handlers
    ):
        app_file_handler = ArchivoRotativo(
            str(APP_LOG_FILE),
            maxBytes=APP_LOG_MAX_BYTES,
            backupCount=APP_LOG_BACKUPS,
            encoding="utf-8",
        )
        app_file_handler.setFormatter(formatter)
        root.addHandler(app_file_handler)

    # ==================================================
    # LOGGER EXECUTION
    # ==================================================
    execution_logger = logging.getLogger("execution")
    execution_logger.setLevel(logging.INFO)
    execution_logger.propagate = False

    if not any(
        isinstance(h, logging.FileHandler)
        for h in execution_logger.handlers
    ):
        file_handler = logging.FileHandler(
            str(LOG_FILE),
            encoding="utf-8"
        )

        file_handler.setFormatter(formatter)
        execution_logger.addHandler(file_handler)

    # ==================================================
    # LOGGERS RUIDOSOS
    # ==================================================
    for noisy in (
        "apscheduler",
        "apscheduler.scheduler",
        "werkzeug"
    ):
        logging.getLogger(noisy).setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger(__name__)
execution_logger = logging.getLogger("execution")


actividad_logger = logging.getLogger(LOGGER_ACTIVIDAD)


def log_activity(message: str, level: str = "INFO") -> None:
    """
    Línea clara de actividad: sale en la consola y en Actividad en vivo
    (/api/recent_logs) y queda en app_log.txt. Una línea por paso, sin detalle técnico.
    """
    actividad_logger.log(getattr(logging, level.upper(), logging.INFO), message)


def log_to_file(message: str, level: str = "INFO") -> None:
    """Persiste en execution_log.txt y refleja en el tablero web."""
    log_fn = getattr(execution_logger, level.lower(), execution_logger.info)
    log_fn(message)
    log_activity(message, level)


def log_task(message: str, level: str = "INFO") -> None:
    """Ejecución real de tarea: archivo + tablero (una línea por mensaje)."""
    log_to_file(message, level)


def log_gui_action(action: str, **fields) -> None:
    """Acción desde la interfaz: solo a app_log.txt (la consola muestra la línea clara de cada paso)."""
    if fields:
        detail = ", ".join(f"{key}={value}" for key, value in fields.items())
        logger.info(f"[GUI] {action} — {detail}")
    else:
        logger.info(f"[GUI] {action}")


def _execution_log_file_handlers():
    """Handlers que escriben en execution_log.txt."""
    log_path = LOG_FILE.resolve()
    for handler in execution_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            try:
                if Path(handler.baseFilename).resolve() == log_path:
                    yield handler
            except (TypeError, ValueError, OSError):
                continue


def _close_execution_log_handlers():
    for handler in list(_execution_log_file_handlers()):
        handler.close()
        execution_logger.removeHandler(handler)


def _attach_execution_log_handler():
    if any(True for _ in _execution_log_file_handlers()):
        return
    handler = logging.FileHandler(str(LOG_FILE), encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    execution_logger.addHandler(handler)


def _archivo_de_log_por_area(ruta: Path, nombres) -> None:
    """
    Agrega un archivo de log (rota a los 5 MB, 5 respaldos) a los loggers del área.
    Un solo handler para todos: con uno por logger el archivo quedaba abierto
    varias veces y Windows no dejaba rotarlo (WinError 32 en cada línea).
    """
    ruta.parent.mkdir(parents=True, exist_ok=True)
    handler = ArchivoRotativo(str(ruta), maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, datefmt=LOG_DATEFMT))
    for nombre in nombres:
        area_logger = logging.getLogger(nombre)
        area_logger.setLevel(logging.INFO)
        for viejo in [h for h in area_logger.handlers if isinstance(h, RotatingFileHandler)
                      and Path(h.baseFilename).resolve() == ruta.resolve()]:
            area_logger.removeHandler(viejo)
            viejo.close()
        area_logger.addHandler(handler)


_archivo_de_log_por_area(SMS_LOG_FILE, LOGGERS_SMS)
_archivo_de_log_por_area(EMAIL_LOG_FILE, LOGGERS_EMAIL)
logger_sms = logging.getLogger("sms")
logger_email = logging.getLogger("email")


def _clave_sesiones():
    """
    Clave para firmar las sesiones (login): variable de entorno SECRET_KEY o
    "secret_key" de config.json. Si no hay, se genera una al azar y cada
    reinicio cierra las sesiones abiertas.
    """
    clave = os.environ.get("SECRET_KEY", "").strip()
    if not clave:
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as f:
                clave = str(json.load(f).get("secret_key") or "").strip()
        except Exception:
            clave = ""
    if not clave:
        import secrets
        clave = secrets.token_hex(32)
        logger.warning("⚠️ Sin secret_key en config.json ni SECRET_KEY: se usa una clave al azar "
                       "y las sesiones se cierran en cada reinicio.")
    return clave


app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["SECRET_KEY"] = _clave_sesiones()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(hours=12)
app.config["UPLOAD_FOLDER"] = str(UPLOAD_FOLDER)
app.config["DOWNLOAD_FOLDER"] = str(DOWNLOAD_FOLDER)
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{BASE_DIR / 'app.db'}"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False


@app.context_processor
def _nombre_app():
    """APP_NOMBRE disponible en todas las plantillas."""
    return {"app_nombre": APP_NOMBRE}


init_db(app)
COLOMBIA_TZ = pytz.timezone("America/Bogota")

scheduler = BackgroundScheduler(
    timezone=COLOMBIA_TZ
)
scheduler.start()
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
DOWNLOAD_FOLDER.mkdir(parents=True, exist_ok=True)
load_config()


def reschedule_console_message_job(seconds: int | None = None) -> int:
    """Reprograma el mensaje en consola."""
    interval = (
        int(seconds)
        if seconds is not None
        else get_console_message_interval_seconds()
    )
    scheduler.reschedule_job(
        CONSOLE_MESSAGE_JOB_ID,
        trigger=IntervalTrigger(seconds=interval),
    )
    logger.info(f"Mensaje consola reprogramado: cada {interval} segundos")
    return interval


LOG_BACKUP_GLOB = "execution_log_backup_*.txt"


def cleanup_old_log_files() -> list[str]:
    """Elimina archivos de log rotados más antiguos que log_retention_hours."""
    retention_hours = get_log_retention_hours()
    cutoff_ts = datetime.now().timestamp() - (retention_hours * 3600)
    deleted: list[str] = []

    for path in BASE_DIR.glob(LOG_BACKUP_GLOB):
        try:
            if path.resolve() == LOG_FILE.resolve():
                continue
            if path.stat().st_mtime < cutoff_ts:
                path.unlink()
                deleted.append(path.name)
        except OSError as e:
            logger.warning(f"No se pudo eliminar log antiguo {path.name}: {e}")

    if deleted:
        log_activity(
            f"Logs de respaldo eliminados (retención {retention_hours} h): {', '.join(deleted)}"
        )
    return deleted


def read_recent_log_lines(limit: int = 200) -> list[str]:
    """Últimas líneas de actividad para el tablero."""
    with _activity_lock:
        if not _activity_log:
            return []
        return list(_activity_log)[-limit:]


def rotate_log_if_needed():
    """Rota execution_log.txt al superar LOG_MAX_LINES."""
    with _log_io_lock:
        if not LOG_FILE.exists():
            return
        try:
            with LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
                line_count = sum(1 for _ in f)
        except OSError as e:
            logger.warning(f"No se pudo leer el log para rotación: {e}")
            return

        if line_count < LOG_MAX_LINES:
            return

        backup_file = BASE_DIR / (
            f"execution_log_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        )
        _close_execution_log_handlers()
        try:
            shutil.copy2(LOG_FILE, backup_file)
            LOG_FILE.unlink(missing_ok=True)
            _attach_execution_log_handler()
            log_to_file(f"Archivo de log rotado: {backup_file.name}")
            cleanup_old_log_files()
        except OSError as e:
            _attach_execution_log_handler()
            logger.error(f"No se pudo rotar el log: {e}")


# Resumen de programaciones pendientes de hoy para la consola: app.py registra
# la función (registrar_resumen_consola) y se muestra solo cuando cambia.
_resumen_consola = None
_ultimo_resumen_consola = ""


def registrar_resumen_consola(funcion) -> None:
    global _resumen_consola
    _resumen_consola = funcion


def console_message_heartbeat():
    """Cada N segundos (console_message_interval_seconds): las programaciones pendientes de hoy, si cambiaron."""
    global _ultimo_resumen_consola
    if _resumen_consola is None:
        return
    try:
        texto = _resumen_consola()
    except Exception:
        logger.exception("No se pudo armar el resumen de programaciones pendientes")
        return
    if texto and texto != _ultimo_resumen_consola:
        _ultimo_resumen_consola = texto
        log_activity(texto)


def mantenimiento_logs():
    """Rota execution_log.txt y borra los respaldos viejos (cada 5 minutos)."""
    try:
        rotate_log_if_needed()
        cleanup_old_log_files()
    except Exception as e:
        logger.error(f"Error en el mantenimiento de logs: {e}")


# ==================================================
# 🔄 POLLING DE ESTADOS SMS (CORREGIDO)
# ==================================================

def polling_estados_sms():
    """
    Consulta el estado de los SMS pendientes y actualiza BigQuery.
    Se ejecuta cada 10 minutos.
    """
    try:
        # Obtener configuración de Infobip
        config = load_config()
        infobip_config = config.get("infobip", {})
        api_key = infobip_config.get("api_key", "").strip()
        base_url = infobip_config.get("base_url", "").strip()
        
        if not api_key or not base_url:
            log_activity("Estados de SMS: falta api_key o base_url en la sección infobip de config.json.", "ERROR")
            return

        # Obtener cliente de BigQuery
        from conexion_bigquery import get_bigquery_client
        client = get_bigquery_client()

        if not client:
            log_activity("Estados de SMS: no se pudo conectar a BigQuery.", "ERROR")
            return
        
        # 🔥 Importar la función CORRECTA desde sms_service
        from services.sms_service import actualizar_estados_pendientes
        
        # Ejecutar polling
        resultado = actualizar_estados_pendientes(
            api_key=api_key,
            base_url=base_url,
            client=client
        )
        
        logger_sms.info(f"📊 Resultado del polling: {resultado}")
        # En la consola solo si algo cambió o falló (cada 10 min, sin ruido).
        if not resultado.get("success"):
            log_activity(f"Estados de SMS: no se pudieron consultar. {_explicar(resultado.get('message'))}",
                         "WARNING")
        elif resultado.get("actualizados") or resultado.get("errores"):
            log_activity(f"🔄 Estados de SMS: {resultado.get('actualizados', 0)} actualizados "
                         f"(entregados / fallidos) de {resultado.get('pendientes', 0)} pendientes en BigQuery"
                         + (f"; {resultado['errores']} lotes no se guardaron en SmsDeliveryReports (ver logs/sms.log)."
                            if resultado.get("errores") else "."))

    except Exception as e:
        logger_sms.exception(f"❌ Error en el polling de estados SMS: {e}")
        log_activity(f"Estados de SMS: no se pudieron actualizar. {_explicar(e)}", "WARNING")


def _explicar(error) -> str:
    """Mensaje claro de un error (services/errores.py) para la consola."""
    from services.errores import explicar_error
    return explicar_error(error if isinstance(error, BaseException) else Exception(str(error or "")),
                          servicio="Infobip (SMS)")


# ==================================================
#  POLLING DE ESTADOS SMS - CADA 10 MINUTOS
# ==================================================

# En una copia de prueba (APP_COPIA_PRUEBA=1) no se programa: el polling
# actualiza en BigQuery los estados de los SMS de producción.
from modo_prueba import es_copia_prueba

if es_copia_prueba():
    logger.warning("🧪 APP_COPIA_PRUEBA=1: no se programa el polling de estados SMS.")
else:
    scheduler.add_job(
        polling_estados_sms,
        trigger=IntervalTrigger(minutes=10),
        id="sms_polling_estados",
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(seconds=60)  # Primera en 60s
    )
    logger.info("✅ Polling de estados SMS programado cada 10 minutos")


# ========== INICIALIZAR SCHEDULER ==========
_console_interval = get_console_message_interval_seconds()
_now = datetime.now()
scheduler.add_job(
    console_message_heartbeat,
    trigger=IntervalTrigger(seconds=_console_interval),
    id=CONSOLE_MESSAGE_JOB_ID,
    replace_existing=True,
    next_run_time=_now,
)
scheduler.add_job(
    mantenimiento_logs,
    trigger=IntervalTrigger(minutes=5),
    id=LOG_MANTENIMIENTO_JOB_ID,
    replace_existing=True,
)
logger.info(
    f"Mensaje consola (independiente): cada {_console_interval} segundos"
)
log_activity(f"Aplicación {APP_NOMBRE} iniciada; programaciones de SMS y email activas")
console_message_heartbeat()
