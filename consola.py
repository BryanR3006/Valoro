"""
Consola clara: lo que se ve en la terminal y en "Actividad en vivo" (Inicio).

Solo salen las líneas de actividad (backend.log_activity / log_task, logger
"actividad"): una por paso que importa —consultando BigQuery, cuántos
registros trajo, cuántos se excluyeron, enviando, resultado, programaciones
pendientes—, en una sola línea y con la hora. El detalle técnico (tracebacks,
respuestas de la API, cada SMS) va solo a los archivos: app_log.txt,
logs/sms.log y logs/email.log.

Sin Flask, base de datos ni red.
"""
import logging
import time
from logging.handlers import RotatingFileHandler

LOGGER_ACTIVIDAD = "actividad"
LARGO_LINEA = 400


class FiltroClaro(logging.Filter):
    """Deja pasar solo las líneas de actividad; lo técnico queda en los archivos de log."""

    def filter(self, record):
        return record.name == LOGGER_ACTIVIDAD


class FormatoClaro(logging.Formatter):
    """'14:26:49  ✅ SMS …': hora y mensaje en una línea, sin traceback; ❌ / ⚠️ si es error o aviso."""

    def __init__(self):
        super().__init__(datefmt="%H:%M:%S")

    def format(self, record):
        texto = " ".join(record.getMessage().split())
        if record.levelno >= logging.ERROR and "❌" not in texto[:3]:
            texto = "❌ " + texto
        elif record.levelno == logging.WARNING and "⚠" not in texto[:3]:
            texto = "⚠️ " + texto
        if len(texto) > LARGO_LINEA:
            texto = texto[:LARGO_LINEA - 1] + "…"
        return f"{self.formatTime(record, self.datefmt)}  {texto}"


class ArchivoRotativo(RotatingFileHandler):
    """
    RotatingFileHandler que no se cae si Windows no deja renombrar el archivo
    (otro proceso lo tiene abierto, WinError 32): sigue escribiendo en el mismo
    archivo y reintenta la rotación en 10 minutos, en vez de sacar
    "--- Logging error ---" en la consola en cada línea.
    """
    ESPERA_REINTENTO = 600

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._rotar_desde = 0.0

    def shouldRollover(self, record):
        if time.time() < self._rotar_desde:
            return False
        return super().shouldRollover(record)

    def doRollover(self):
        try:
            super().doRollover()
        except OSError:
            self._rotar_desde = time.time() + self.ESPERA_REINTENTO
            if self.stream is None:
                self.stream = self._open()
