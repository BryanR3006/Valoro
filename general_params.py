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


def _defaults() -> dict:
    return {
        "log_retention_hours": DEFAULT_LOG_RETENTION_HOURS,
        "console_message_interval_seconds": DEFAULT_CONSOLE_MESSAGE_INTERVAL_SECONDS,
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
    return base


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
            cfg[GENERAL_PARAMS_KEY] = {
                "log_retention_hours": hours,
                "console_message_interval_seconds": console_seconds,
                "updated_at": datetime.now().isoformat(),
            }
            _write_config(cfg)
        return {
            "success": True,
            "message": "Parámetros guardados correctamente.",
            "parameters": {
                "log_retention_hours": hours,
                "console_message_interval_seconds": console_seconds,
            },
            "scheduler_applied": True,
        }
    except Exception as exc:
        return {"success": False, "message": str(exc)}
