"""
Parámetros generales de la aplicación (persistencia en config.json).
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

_lock = threading.Lock()
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config.json")
GENERAL_PARAMS_KEY = "general_parameters"

DEFAULT_LOG_RETENTION_HOURS = 72
MIN_LOG_RETENTION_HOURS = 1
MAX_LOG_RETENTION_HOURS = 8760

DEFAULT_CONSOLE_MESSAGE_INTERVAL_SECONDS = 60
MIN_CONSOLE_MESSAGE_INTERVAL_SECONDS = 60
MAX_CONSOLE_MESSAGE_INTERVAL_SECONDS = 3600

# Horario de envío (hora de Colombia): fuera de él no sale ningún SMS ni email,
# ni manual ni programado (services/sms_horarios.fuera_de_horario).
DEFAULT_HORARIO_ENVIO_DESDE = "07:00"
DEFAULT_HORARIO_ENVIO_HASTA = "18:00"


def _ensure_config() -> None:
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump({}, f, indent=2, ensure_ascii=False)


def _read_config() -> dict:
    _ensure_config()
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_config(cfg: dict) -> None:
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def _normalize_log_retention_hours(value) -> int:
    try:
        hours = int(value)
    except (TypeError, ValueError):
        hours = DEFAULT_LOG_RETENTION_HOURS
    return max(MIN_LOG_RETENTION_HOURS, min(hours, MAX_LOG_RETENTION_HOURS))


def _normalize_console_message_interval_seconds(value) -> int:
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        seconds = DEFAULT_CONSOLE_MESSAGE_INTERVAL_SECONDS
    return max(
        MIN_CONSOLE_MESSAGE_INTERVAL_SECONDS,
        min(seconds, MAX_CONSOLE_MESSAGE_INTERVAL_SECONDS),
    )


def _normalize_hora(value, default: str) -> str:
    """'HH:MM' válida (07:00), o el valor por defecto."""
    try:
        hh, mm = (int(x) for x in str(value).strip().split(":")[:2])
    except (TypeError, ValueError):
        return default
    return f"{hh:02d}:{mm:02d}" if 0 <= hh < 24 and 0 <= mm < 60 else default


def _normalize_horario_envio(desde, hasta) -> tuple[str, str]:
    """(desde, hasta) válidos; si no hay un rango (desde antes de hasta), el de por defecto."""
    desde = _normalize_hora(desde, DEFAULT_HORARIO_ENVIO_DESDE)
    hasta = _normalize_hora(hasta, DEFAULT_HORARIO_ENVIO_HASTA)
    if desde >= hasta:
        return DEFAULT_HORARIO_ENVIO_DESDE, DEFAULT_HORARIO_ENVIO_HASTA
    return desde, hasta


def _defaults() -> dict:
    return {
        "log_retention_hours": DEFAULT_LOG_RETENTION_HOURS,
        "console_message_interval_seconds": DEFAULT_CONSOLE_MESSAGE_INTERVAL_SECONDS,
        "horario_envio_desde": DEFAULT_HORARIO_ENVIO_DESDE,
        "horario_envio_hasta": DEFAULT_HORARIO_ENVIO_HASTA,
    }


def _merge_params(raw: dict | None) -> dict:
    base = _defaults()
    if isinstance(raw, dict):
        base["log_retention_hours"] = _normalize_log_retention_hours(
            raw.get("log_retention_hours", DEFAULT_LOG_RETENTION_HOURS)
        )
        base["console_message_interval_seconds"] = (
            _normalize_console_message_interval_seconds(
                raw.get(
                    "console_message_interval_seconds",
                    DEFAULT_CONSOLE_MESSAGE_INTERVAL_SECONDS,
                )
            )
        )
        base["horario_envio_desde"], base["horario_envio_hasta"] = _normalize_horario_envio(
            raw.get("horario_envio_desde"), raw.get("horario_envio_hasta"))
    return base


def get_horario_envio() -> tuple[str, str]:
    """
    (desde, hasta) del horario de envío, 'HH:MM' en hora de Colombia. Se lee
    en cada envío: un cambio en config.json aplica sin reiniciar. Si no se
    puede leer, el de por defecto (07:00 a 18:00): nunca queda sin límite.
    """
    try:
        with _lock:
            cfg = _read_config()
        params = _merge_params(cfg.get(GENERAL_PARAMS_KEY))
        return params["horario_envio_desde"], params["horario_envio_hasta"]
    except Exception:
        return DEFAULT_HORARIO_ENVIO_DESDE, DEFAULT_HORARIO_ENVIO_HASTA


def get_log_retention_hours() -> int:
    """Horas que se conservan los archivos de log rotados (backups)."""
    try:
        with _lock:
            cfg = _read_config()
        return _merge_params(cfg.get(GENERAL_PARAMS_KEY))["log_retention_hours"]
    except Exception:
        return DEFAULT_LOG_RETENTION_HOURS


def get_console_message_interval_seconds() -> int:
    """Intervalo del mensaje en consola (segundos)."""
    try:
        with _lock:
            cfg = _read_config()
        return _merge_params(cfg.get(GENERAL_PARAMS_KEY))[
            "console_message_interval_seconds"
        ]
    except Exception:
        return DEFAULT_CONSOLE_MESSAGE_INTERVAL_SECONDS


def load_general_parameters() -> dict:
    """Devuelve parámetros generales para la interfaz."""
    try:
        with _lock:
            cfg = _read_config()
            if GENERAL_PARAMS_KEY not in cfg or not isinstance(
                cfg.get(GENERAL_PARAMS_KEY), dict
            ):
                cfg[GENERAL_PARAMS_KEY] = _defaults()
                _write_config(cfg)
            params = _merge_params(cfg.get(GENERAL_PARAMS_KEY))
        return {
            "success": True,
            "parameters": params,
            "limits": {
                "log_retention_hours_min": MIN_LOG_RETENTION_HOURS,
                "log_retention_hours_max": MAX_LOG_RETENTION_HOURS,
                "console_message_interval_seconds_min": MIN_CONSOLE_MESSAGE_INTERVAL_SECONDS,
                "console_message_interval_seconds_max": MAX_CONSOLE_MESSAGE_INTERVAL_SECONDS,
            },
        }
    except Exception as exc:
        return {
            "success": False,
            "message": str(exc),
            "parameters": _defaults(),
            "limits": {
                "log_retention_hours_min": MIN_LOG_RETENTION_HOURS,
                "log_retention_hours_max": MAX_LOG_RETENTION_HOURS,
                "console_message_interval_seconds_min": MIN_CONSOLE_MESSAGE_INTERVAL_SECONDS,
                "console_message_interval_seconds_max": MAX_CONSOLE_MESSAGE_INTERVAL_SECONDS,
            },
        }


def save_general_parameters(data: dict) -> dict:
    """Guarda parámetros generales en config.json."""
    try:
        with _lock:
            cfg = _read_config()
            current = _merge_params(cfg.get(GENERAL_PARAMS_KEY))
            payload = data or {}
            hours = _normalize_log_retention_hours(
                payload.get("log_retention_hours", current["log_retention_hours"])
            )
            console_seconds = _normalize_console_message_interval_seconds(
                payload.get(
                    "console_message_interval_seconds",
                    current["console_message_interval_seconds"],
                )
            )
            desde, hasta = _normalize_horario_envio(
                payload.get("horario_envio_desde", current["horario_envio_desde"]),
                payload.get("horario_envio_hasta", current["horario_envio_hasta"]),
            )
            cfg[GENERAL_PARAMS_KEY] = {
                "log_retention_hours": hours,
                "console_message_interval_seconds": console_seconds,
                "horario_envio_desde": desde,
                "horario_envio_hasta": hasta,
                "updated_at": datetime.now().isoformat(),
            }
            _write_config(cfg)
        return {
            "success": True,
            "message": "Parámetros guardados correctamente.",
            "parameters": {
                "log_retention_hours": hours,
                "console_message_interval_seconds": console_seconds,
                "horario_envio_desde": desde,
                "horario_envio_hasta": hasta,
            },
            "scheduler_applied": True,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}
