#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from datetime import timedelta

import json
import re
import time 
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Set
from uuid import uuid4
from database import db, ProgramacionSms
from listas_negras import LISTA_NEGRA_SMS
import requests
import logging
import pandas as pd

logger = logging.getLogger(__name__)
COLOMBIA_TZ = timezone(timedelta(hours=-5))

# ========== CONSTANTES ==========
PHONE_COLUMNS = (
    "celular", "telefono", "teléfono", "tel", "movil", "móvil", "phone",
    "numero", "número", "telephone", "tel1", "cel", "cellphone",
)
VARIABLE_PATTERN = re.compile(r"{{\s*([^{}]+?)\s*}}")
SMS_LOG_TABLE = "capable-arbor-209819.Temporal.SmsLog"
SCHEDULE_TABLE = "capable-arbor-209819.Temporal.ProgramacionSMS"
BLACKLIST_TABLE = LISTA_NEGRA_SMS  # se cambia en listas_negras.py

# 🔥 NUEVO: Tabla para reportes de entrega
SMS_DELIVERY_REPORTS_TABLE = "capable-arbor-209819.Temporal.SmsDeliveryReports"

BATCH_SIZE = 100
MAX_WORKERS = 3
MAX_REINTENTOS = 2

# 🔥 NUEVO: Configuración de polling
POLLING_BATCH_SIZE = 1000
POLLING_MAX_HOURS = 48  # Solo consultar mensajes de las últimas 48h

# SmsLog se guarda en INSERTs de esta cantidad de filas: cada fila lleva 14
# parámetros y BigQuery no acepta tantos parámetros en una sola consulta (un
# envío grande se quedaba sin registrar).
SMS_LOG_FILAS_POR_INSERT = 500


class SmsServiceError(Exception):
    """Error que se puede mostrar de forma segura en la interfaz."""
    pass


# ==================================================
# 📡 CLIENTE INFOBIP V2 (CON ACORTAMIENTO)
# ==================================================

class InfobipSenderV2:
    def __init__(self, api_key: str, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"App {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        })

    def enviar_lote(self, lista_mensajes: List[Dict], reintentos: int = MAX_REINTENTOS) -> Optional[Dict]:
        """
        Envía un lote de mensajes a Infobip con ACORTAMIENTO DE URLs.
        """
        url = f"{self.base_url}/sms/2/text/advanced"
        
        payload = {
            "messages": lista_mensajes,
            "urlOptions": {
                "shortenUrl": True,
                "trackClicks": True,
                "removeProtocol": False
            }
        }
        
        logger.info(f"📤 Enviando lote de {len(payload['messages'])} SMS a Infobip (URLs se acortarán automáticamente)")
        logger.debug(f"📦 Payload: {json.dumps(payload, ensure_ascii=False)[:500]}...")
        
        for intento in range(1, reintentos + 1):
            try:
                resp = self.session.post(url, json=payload, timeout=30)
                if resp.status_code in [200, 201]:
                    logger.info(f"✅ Lote enviado exitosamente (URLs acortadas)")
                    return resp.json()
                else:
                    logger.warning(f"⚠️ Intento {intento}/{reintentos}: Error {resp.status_code} - {resp.text[:200]}")
                    if intento < reintentos:
                        time.sleep(2 * intento)
            except requests.exceptions.RequestException as e:
                logger.warning(f"⚠️ Intento {intento}/{reintentos}: {e}")
                if intento < reintentos:
                    time.sleep(2 * intento)
        return None


# ==================================================
# 📱 LIMPIEZA DE NÚMEROS
# ==================================================

def telefono_10(valor: Any) -> str:
    """Últimos 10 dígitos del teléfono (sin 57 ni signos), para comparar formatos distintos."""
    return re.sub(r"\D", "", str(valor or ""))[-10:]


def limpiar_numero(value: Any) -> Optional[str]:
    """Limpia y normaliza número de teléfono al formato E.164 de Colombia."""
    if value is None:
        return None
    
    # Convertir a string y eliminar todo lo que no sea dígito
    num = re.sub(r"\D", "", str(value))
    
    if not num:
        return None
    
    # Caso 1: Ya está en formato 57 + 10 dígitos (ej: 573001234567)
    if num.startswith("57") and len(num) == 12:
        return num
    
    # Caso 2: Número colombiano de 10 dígitos que empieza con 3
    if len(num) == 10 and num.startswith("3"):
        return "57" + num
    
    # Caso 3: Solo dígitos sin código de país - asumir Colombia
    if len(num) == 10:
        return "57" + num
    
    # Caso 4: Ya tiene código de país diferente
    if len(num) > 10:
        return num
    
    # Caso 5: Muy corto, inválido
    logger.debug(f"Número inválido (muy corto): {num}")
    return None


# ==================================================
# 🧠 MANEJO DE VARIABLES
# ==================================================

def extraer_variables(texto: str) -> List[str]:
    """Extrae variables {{nombre}} de la plantilla."""
    return re.findall(r"{{(.*?)}}", texto)


def remover_tildes(texto: str) -> str:
    """Elimina tildes del texto para evitar problemas con encoding SMS."""
    nfkd_form = unicodedata.normalize('NFKD', texto)
    return "".join([c for c in nfkd_form if not unicodedata.combining(c)])


def validar_variables(rows: List[Dict], variables: List[str]) -> Tuple[List[str], Dict[str, int]]:
    """Valida que las variables existan en los datos y cuenta valores vacíos."""
    if not rows:
        return [], {}
    
    # Obtener columnas disponibles (case-insensitive)
    columns_lower = {str(key).strip().lower(): str(key) for key in rows[0].keys()}
    faltantes = []
    vacias = {}
    
    for var in variables:
        var_lower = var.strip().lower()
        if var_lower not in columns_lower:
            faltantes.append(var)
        else:
            # Contar valores vacíos o nulos
            real_col = columns_lower[var_lower]
            vacios_count = sum(
                1 for row in rows 
                if row.get(real_col) is None or str(row.get(real_col, "")).strip() == ""
            )
            if vacios_count > 0:
                vacias[var] = vacios_count
    
    return faltantes, vacias


def construir_mensaje(row: Dict, plantilla: str, variables: List[str]) -> str:
    """Construye el mensaje personalizado reemplazando variables."""
    msg = plantilla
    
    # Mapa de columnas (case-insensitive) para búsqueda flexible
    row_lower = {str(k).strip().lower(): v for k, v in row.items()}
    
    for var in variables:
        var_lower = var.strip().lower()
        valor = row_lower.get(var_lower, "")
        if valor is None:
            valor = ""
        # Reemplazar {{variable}} en la plantilla
        msg = re.sub(
            r"{{\s*" + re.escape(var) + r"\s*}}",
            str(valor),
            msg,
            flags=re.IGNORECASE
        )
    
    return remover_tildes(msg)


def detectar_columna_telefono(rows: List[Dict]) -> str:
    """Detecta la columna de teléfono en los datos (case-insensitive)."""
    if not rows:
        raise SmsServiceError("La consulta no devolvió registros.")
    
    columns = {str(key).strip().lower(): str(key) for key in rows[0]}
    
    for candidate in PHONE_COLUMNS:
        if candidate in columns:
            return columns[candidate]
    
    # Mostrar columnas disponibles para ayudar al usuario
    cols_disponibles = ", ".join(rows[0].keys())
    raise SmsServiceError(
        f"No se identificó una columna de teléfono. Columnas disponibles: {cols_disponibles}. "
        "Use un alias como 'celular', 'telefono', 'movil' o 'phone'."
    )


def senuelos_configurados() -> List[Dict]:
    """
    Señuelos (números de control) de config.json, sección infobip:
    "senuelos": [{"telefono": "3001234567", "nombre": "...", "link": "https://..."}].
    Cada envío les llega también, para comprobar que los SMS salen bien.
    Van en config.json (no en git) porque son datos personales.
    """
    import os
    ruta = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json")
    try:
        with open(ruta, "r", encoding="utf-8") as f:
            senuelos = (json.load(f).get("infobip") or {}).get("senuelos") or []
    except (OSError, ValueError):
        return []
    return [s for s in senuelos if isinstance(s, dict) and str(s.get("telefono") or "").strip()]


def preparar_sms(rows: List[Dict], plantilla: str) -> Tuple[List[Dict], Dict]:
    """
    Prepara los mensajes: valida números, detecta columnas, reemplaza variables.
    Retorna lista de mensajes preparados y diccionario con detalles.
    """
    if not plantilla or not plantilla.strip():
        raise SmsServiceError("La plantilla del SMS es obligatoria.")
    
    phone_column = detectar_columna_telefono(rows)
    variables = extraer_variables(plantilla)
    
    if not variables:
        raise SmsServiceError(
            "La plantilla no contiene variables ({{Nombre Cliente}}, {{Valor Oferta Esp 2}}, etc.). "
            "Agregue al menos una variable para personalizar los mensajes."
        )
    
    missing, empty_variables = validar_variables(rows, variables)
    
    if missing:
        raise SmsServiceError(
            f"Variables no encontradas en la consulta: {', '.join(missing)}. "
            "Verifique los nombres de las variables en la plantilla."
        )

    # Exclusiones del equipo de SMS (#38): bancos NO y filas sin el dato
    # de una variable que usa la plantilla (oferta, fecha de pago, porcentaje).
    from services.sms_exclusiones import aplicar_exclusiones
    total_filas = len(rows)
    rows, excluidos = aplicar_exclusiones(rows, variables)
    if excluidos:
        logger.info(f"🚫 Excluidos por reglas SMS: {excluidos}")

    seen = set()
    prepared = []
    invalid_numbers = 0

    # Señuelos: números de control que reciben cada envío (config.json → infobip.senuelos).
    for senuelo in senuelos_configurados():
        telefono = str(senuelo["telefono"]).strip()
        row = {
            "Nombre Cliente": senuelo.get("nombre", ""),
            "Link": senuelo.get("link", ""),
            "telefono": telefono
        }
        prepared.append({
            "phone": telefono,
            "text": construir_mensaje(row, plantilla, variables),
            "row": row
        })
    for row in rows:
            phone = limpiar_numero(row.get(phone_column))
            if not phone:
                invalid_numbers += 1
                continue
            # Eliminar duplicados dentro del mismo lote
            if phone in seen:
                continue
            seen.add(phone)
            
            prepared.append({
                "phone": phone,
                "text": construir_mensaje(row, plantilla, variables),
                "row": row
            })
        
    return prepared, {
            "phone_column": phone_column,
            "invalid_numbers": invalid_numbers,
            "duplicates": len(rows) - invalid_numbers - len(prepared),
            "empty_variables": empty_variables,
            "total_validos": len(prepared),
            "total_filas": total_filas,
            "excluidos_reglas": excluidos,
        }


def preview_sms(rows: List[Dict], plantilla: str, limit: int = 3) -> Dict:
    """Genera una vista previa sin enviar."""
    try:
        prepared, details = preparar_sms(rows, plantilla)
        
        preview_items = []
        for item in prepared[:limit]:
            preview_items.append({
                "telefono": item["phone"],
                "mensaje": item["text"],
                "longitud": len(item["text"])
            })
        
        return {
            "success": True,
            "total_validos": len(prepared),
            "preview": preview_items,
            "phone_column": details.get("phone_column"),
            "invalid_numbers": details.get("invalid_numbers", 0),
            "duplicates": details.get("duplicates", 0),
            "empty_variables": details.get("empty_variables", {}),
            "total_filas": len(rows)
        }
    except SmsServiceError as e:
        return {"success": False, "message": str(e)}


# ==================================================
# 📤 ENVÍO DE LOTES CON CALLBACKDATA Y ACORTAMIENTO
# ==================================================


def enviar_sms_desde_filas(
    rows: List[Dict], 
    plantilla: str, 
    config: Dict, 
    client=None,
    campaign: str = "", 
    usuario: str = "", 
    query_sql: str = "", 
    allow_resend: bool = False
) -> Dict:
    """
    Función principal: valida, genera mensajes, envía por lotes con callbackData.
    🔥 MODIFICADO: Ahora captura y guarda message_id individual.
    """
    api_key = (config.get("api_key") or "").strip()
    base_url = (config.get("base_url") or "").strip()
    sender_id = (config.get("sender_id") or "").strip()
    
    # Validar configuración
    if not api_key or api_key.startswith("REEMPLAZAR_") or not base_url or not sender_id:
        raise SmsServiceError(
            "Configure infobip.api_key, infobip.base_url e infobip.sender_id en config.json antes de enviar."
        )
    
    # Preparar mensajes
    prepared, details = preparar_sms(rows, plantilla)
    logger.debug(f"🔍 DEBUG - Details: {details}")
    for i, item in enumerate(prepared):
        logger.debug(f"🔍 DEBUG - Preparado {i}: phone={item['phone']}")
    # Aplicar validaciones (lista negra, duplicados)
    if client:
        phones = [item["phone"] for item in prepared]
        blocked = verificar_lista_negra(client, phones)
        duplicates = verificar_duplicados(client, phones)
        
        allowed = [item for item in prepared if item["phone"] not in blocked]
        
        if not allow_resend and duplicates:
            allowed = [item for item in allowed if item["phone"] not in duplicates]
        
        details.update({
            "excluidos_lista_negra": len(blocked),
            "duplicados_hoy": len(duplicates),
            "total_validos": len(allowed)
        })
        
        prepared = allowed
    # En enviar_sms_desde_filas, después de verificar duplicados
    logger.debug(f"🔍 DEBUG - Blocked: {blocked}")
    logger.debug(f"🔍 DEBUG - Duplicates: {duplicates}")
    logger.debug(f"🔍 DEBUG - Allow resend: {allow_resend}")
    logger.debug(f"🔍 DEBUG - Allowed antes del filtro: {[item['phone'] for item in allowed]}")

    logger.debug(f"🔍 DEBUG - Aplicando filtro de duplicados...")
    logger.debug(f"🔍 DEBUG - Allowed después del filtro: {[item['phone'] for item in allowed]}")
    if not prepared:
        raise SmsServiceError("No hay destinatarios válidos después de aplicar las validaciones.")
    
    # Construir mensajes para Infobip con callbackData
    batch_size = max(1, min(int(config.get("batch_size", BATCH_SIZE)), 500))
    messages = []
    
    for item in prepared:
        # callbackData con todos los datos de la fila
        datos_fila = {}
        for key, value in item["row"].items():
            if value is not None and str(value).strip() != "":
                datos_fila[key] = str(value).strip()
            else:
                datos_fila[key] = ""
        
        callback_str = json.dumps(datos_fila, ensure_ascii=False)
        if len(callback_str) > 4000:
            callback_str = callback_str[:3997] + "..."
        
        messages.append({
            "from": sender_id,
            "destinations": [{"to": item["phone"]}],
            "text": item["text"],
            "callbackData": callback_str
        })
    
    # Dividir en lotes
    lotes = [messages[i:i + batch_size] for i in range(0, len(messages), batch_size)]
    
    logger.info(f"📦 Total mensajes: {len(messages)}, Lotes: {len(lotes)}, Batch size: {batch_size}")
    
    # Enviar lotes
    sender = InfobipSenderV2(api_key, base_url)
    results = []
    max_workers = max(1, min(int(config.get("max_workers", MAX_WORKERS)), 5))
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(sender.enviar_lote, lote) for lote in lotes]
        for future in as_completed(futures):
            try:
                result = future.result(timeout=60)
                if result:
                    results.append({"success": True, "data": result})
                    # Log de resultados individuales
                    if "messages" in result:
                        for msg in result["messages"]:
                            status = msg.get("status", {}).get("name", "?")
                            logger.info(f"   📱 {msg.get('to', '?')} | {status}")
                else:
                    results.append({"success": False, "message": "Error en lote"})
            except Exception as e:
                logger.error(f"Error en lote: {e}")
                results.append({"success": False, "message": f"Error en lote: {e}"})
    
    # Procesar resultados
    successful = [r for r in results if r.get("success")]
    failed = [r for r in results if not r.get("success")]
    bulk_ids = []
    
    # message_id y bulk_id de cada SMS: con ellos el polling actualiza su estado.
    # El teléfono se compara por sus últimos 10 dígitos (Infobip puede devolverlo con 57).
    sin_asignar = {}
    for item in prepared:
        sin_asignar.setdefault(telefono_10(item["phone"]), []).append(item)
    for r in successful:
        data = r.get("data", {})
        bulk_id = data.get("bulkId")
        if bulk_id:
            bulk_ids.append(bulk_id)
        for msg_resp in data.get("messages", []):
            candidatos = sin_asignar.get(telefono_10(msg_resp.get("to")), [])
            if candidatos:
                item = candidatos.pop(0)
                item["message_id"] = msg_resp.get("messageId") or ""
                item["bulk_id"] = bulk_id or ""
    sin_id = sum(len(v) for v in sin_asignar.values())
    if sin_id:
        logger.warning(f"⚠️ {sin_id} SMS quedaron sin message_id (el polling los busca por lote y teléfono)")
    
    # ============================================================
    # GUARDAR BULKID EN ARCHIVO .TXT
    # ============================================================
    if bulk_ids:
        archivo = "bulk_ids_registrados.txt"
        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        try:
            # Leer archivo existente
            try:
                with open(archivo, "r", encoding="utf-8") as f:
                    lineas = f.readlines()
            except FileNotFoundError:
                lineas = []
            
            # Agregar nuevos bulkIds
            for bulk_id in bulk_ids:
                # Verificar si ya existe
                existe = False
                for linea in lineas:
                    if f"BULK_ID: {bulk_id}" in linea:
                        existe = True
                        break
                
                if not existe:
                    nueva_linea = (
                        f"BULK_ID: {bulk_id} | "
                        f"CAMPAÑA: {campaign} | "
                        f"USUARIO: {usuario} | "
                        f"TOTAL: {len(messages)} | "
                        f"FECHA_ENVIO: {fecha_actual} | "
                        f"ESTADO: PENDIENTE\n"
                    )
                    lineas.append(nueva_linea)
                    logger.info(f"💾 BulkId guardado en archivo: {bulk_id}")
            
            # Guardar archivo
            with open(archivo, "w", encoding="utf-8") as f:
                f.writelines(lineas)
            
            logger.info(f"✅ Archivo actualizado: {archivo} ({len(bulk_ids)} bulkIds guardados)")
            
        except Exception as e:
            logger.error(f"⚠️ Error guardando bulkId en archivo: {e}")
    
    # Recolectar errores
    errores = []
    for result in failed:
        errores.append(result.get("message", "Error desconocido"))
    
    # Guardar en SmsLog (por partes). Un error aquí no deshace el envío, pero
    # queda en el resultado para que se vea: sin registro no hay seguimiento.
    registro_smslog = {"total": len(prepared), "guardados": 0, "errores": []}
    if client and prepared:
        try:
            registro_smslog = guardar_sms_log(
                client, prepared,
                campaign=campaign,
                usuario=usuario,
                bulk_ids=bulk_ids,
                reenvios=set(),
                status="PENDIENTE",
                plantilla=plantilla
            )
        except Exception as e:
            logger.error(f"❌ Error guardando SmsLog: {e}")
            registro_smslog["errores"].append(str(e)[:300])
    if registro_smslog["errores"]:
        logger.error(f"❌ SmsLog: {registro_smslog['guardados']} de {registro_smslog['total']} SMS registrados. "
                     f"Errores: {registro_smslog['errores'][:3]}")
    
    # SMS que Infobip aceptó (uno por mensaje en la respuesta de cada lote).
    enviados = sum(len(r["data"].get("messages") or []) for r in successful)
    fallidos = len(messages) - enviados
    
    # ============================================================
    # NUEVO: Agregar bulk_ids al resultado
    # ============================================================
    return {
        "total_preparados": len(messages),
        "lotes_enviados": len(successful),
        "lotes_fallidos": len(failed),
        "enviados": enviados,
        "fallidos": fallidos,
        "bulk_ids": bulk_ids,
        "errores": errores[:10],
        "details": details,
        "archivo_guardado": "bulk_ids_registrados.txt" if bulk_ids else None,
        "registro_smslog": registro_smslog,
    }


def leer_bulkids_guardados():
    """
    Lee el archivo bulk_ids_registrados.txt y devuelve una lista
    con todos los bulkIds guardados.
    
    Returns:
        Lista de diccionarios con: bulk_id, campana, usuario, total, fecha_envio, estado
    """
    archivo = "bulk_ids_registrados.txt"
    
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            lineas = f.readlines()
        
        bulkids = []
        for linea in lineas:
            linea = linea.strip()
            if not linea:
                continue
            
            # Parsear la línea: BULK_ID: xxx | CAMPAÑA: xxx | USUARIO: xxx | TOTAL: xxx | FECHA_ENVIO: xxx | ESTADO: xxx
            datos = {}
            partes = linea.split(" | ")
            for parte in partes:
                if ": " in parte:
                    clave, valor = parte.split(": ", 1)
                    datos[clave.strip()] = valor.strip()
            
            if "BULK_ID" in datos:
                bulkids.append({
                    "bulk_id": datos.get("BULK_ID", ""),
                    "campana": datos.get("CAMPAÑA", ""),
                    "usuario": datos.get("USUARIO", ""),
                    "total": int(datos.get("TOTAL", 0)),
                    "fecha_envio": datos.get("FECHA_ENVIO", ""),
                    "estado": datos.get("ESTADO", "PENDIENTE")
                })
        
        return bulkids
    except FileNotFoundError:
        print("📭 No hay archivo de bulkIds aún. Ejecuta un envío primero.")
        return []
    except Exception as e:
        print(f"⚠️ Error leyendo archivo: {e}")
        return []


def actualizar_estado_bulkid(bulk_id, nuevo_estado):
    """
    Actualiza el estado de un bulkId en el archivo .txt
    """
    archivo = "bulk_ids_registrados.txt"
    
    try:
        with open(archivo, "r", encoding="utf-8") as f:
            lineas = f.readlines()
        
        for i, linea in enumerate(lineas):
            if f"BULK_ID: {bulk_id}" in linea:
                # Reemplazar ESTADO
                if "| ESTADO:" in linea:
                    partes = linea.split("| ESTADO:")
                    nueva_linea = partes[0] + f"| ESTADO: {nuevo_estado}\n"
                    lineas[i] = nueva_linea
                else:
                    lineas[i] = linea.strip() + f" | ESTADO: {nuevo_estado}\n"
                break
        
        with open(archivo, "w", encoding="utf-8") as f:
            f.writelines(lineas)
            
    except Exception as e:
        print(f"⚠️ Error actualizando estado: {e}")


# ==================================================
# 🔥 NUEVO: GUARDAR SMS LOG CON MESSAGE_ID
# ==================================================

def guardar_sms_log(
    client, 
    mensajes: List[Dict], 
    *, 
    campaign: str, 
    usuario: str,
    bulk_ids: List[str], 
    reenvios: set, 
    status: str, 
    plantilla: str = ""
) -> Dict:
    """
    Guarda los SMS en SmsLog (INSERT por query job, no streaming insert), en
    partes de SMS_LOG_FILAS_POR_INSERT filas. Si una parte falla sigue con
    las demás y lo devuelve: {"total", "guardados", "errores": [...]}.
    """
    from google.cloud import bigquery

    now = datetime.now(timezone.utc).isoformat()
    bulk_id = bulk_ids[0] if bulk_ids else None

    if not mensajes or not client:
        return {"total": len(mensajes or []), "guardados": 0, "errores": []}
    if len(mensajes) > SMS_LOG_FILAS_POR_INSERT:
        resumen = {"total": len(mensajes), "guardados": 0, "errores": []}
        for i in range(0, len(mensajes), SMS_LOG_FILAS_POR_INSERT):
            parte = guardar_sms_log(client, mensajes[i:i + SMS_LOG_FILAS_POR_INSERT], campaign=campaign,
                                    usuario=usuario, bulk_ids=bulk_ids, reenvios=reenvios,
                                    status=status, plantilla=plantilla)
            resumen["guardados"] += parte["guardados"]
            resumen["errores"] += parte["errores"]
        return resumen

    insert_sql = f"""
        INSERT INTO `{SMS_LOG_TABLE}` (
            telefono, mensaje, plantilla, consulta_sql, fecha_envio,
            resultado, bulk_id, message_id, error, campana, usuario, es_reenvio,
            fecha_creacion, fecha_actualizacion
        )
        VALUES
    """
    # Construir múltiples filas con parámetros indexados
    value_rows = []
    parameters = []
    for i, item in enumerate(mensajes):
        value_rows.append(
            f"(@telefono_{i}, @mensaje_{i}, @plantilla_{i}, @consulta_sql_{i}, @fecha_envio_{i}, "
            f"@resultado_{i}, @bulk_id_{i}, @message_id_{i}, @error_{i}, @campana_{i}, @usuario_{i}, @es_reenvio_{i}, "
            f"@fecha_creacion_{i}, @fecha_actualizacion_{i})"
        )
        parameters.extend([
            bigquery.ScalarQueryParameter(f"telefono_{i}", "STRING", item["phone"]),
            bigquery.ScalarQueryParameter(f"mensaje_{i}", "STRING", item["text"]),
            bigquery.ScalarQueryParameter(f"plantilla_{i}", "STRING", plantilla),
            bigquery.ScalarQueryParameter(f"consulta_sql_{i}", "STRING", ""),
            bigquery.ScalarQueryParameter(f"fecha_envio_{i}", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter(f"resultado_{i}", "STRING", status),
            bigquery.ScalarQueryParameter(f"bulk_id_{i}", "STRING", item.get("bulk_id") or bulk_id),
            bigquery.ScalarQueryParameter(f"message_id_{i}", "STRING", item.get("message_id", "")),  # 🔥 NUEVO
            bigquery.ScalarQueryParameter(f"error_{i}", "STRING", ""),
            bigquery.ScalarQueryParameter(f"campana_{i}", "STRING", campaign or ""),
            bigquery.ScalarQueryParameter(f"usuario_{i}", "STRING", usuario or ""),
            bigquery.ScalarQueryParameter(f"es_reenvio_{i}", "BOOL", item["phone"] in reenvios),
            bigquery.ScalarQueryParameter(f"fecha_creacion_{i}", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter(f"fecha_actualizacion_{i}", "TIMESTAMP", now),
        ])

    insert_sql += ", ".join(value_rows)
    job_config = bigquery.QueryJobConfig(query_parameters=parameters)

    try:
        client.query(insert_sql, job_config=job_config).result()
        logger.info(f"✅ {len(mensajes)} registros guardados en SmsLog (vía query job)")
        return {"total": len(mensajes), "guardados": len(mensajes), "errores": []}
    except Exception as e:
        logger.error(f"❌ Error guardando {len(mensajes)} SMS en SmsLog: {e}")
        return {"total": len(mensajes), "guardados": 0, "errores": [str(e)[:300]]}


# ==================================================
# 🔥 NUEVO: FUNCIONES DE POLLING Y ACTUALIZACIÓN DE ESTADOS
# ==================================================

def _normalizar_item_infobip(item: Dict) -> Dict:
    """Mismo formato para logs y reportes (v1 usa "to", v3 de logs usa "destination")."""
    return {
        "messageId": item.get("messageId"),
        "to": item.get("to") or item.get("destination") or "",
        "status": item.get("status") or {},
        "error": item.get("error") or {},
        "price": item.get("price") or {},
        "sentAt": item.get("sentAt"),
        "doneAt": item.get("doneAt"),
    }


def consultar_estados_infobip(bulk_id: str, api_key: str, base_url: str) -> List[Dict]:
    """
    Estado de cada SMS de un lote. Primero los logs (/sms/3/logs: se pueden
    consultar varias veces durante 48 horas); si no responden, los reportes
    (/sms/3/reports, que Infobip entrega una sola vez).
    """
    try:
        response = requests.get(
            f"{base_url}/sms/3/logs",
            headers={"Authorization": f"App {api_key}", "Accept": "application/json"},
            params={"bulkId": bulk_id, "limit": 1000},
            timeout=30,
        )
        if response.status_code == 200:
            resultados = response.json().get("results", [])
            if resultados:
                return [_normalizar_item_infobip(r) for r in resultados]
        else:
            logger.warning(f"⚠️ Logs de Infobip {response.status_code} para {bulk_id}: {response.text[:200]}")
    except Exception as e:
        logger.warning(f"⚠️ No se pudieron leer los logs de Infobip para {bulk_id}: {e}")
    return [_normalizar_item_infobip(r) for r in consultar_reporte_infobip(bulk_id, api_key, base_url)]


def consultar_reporte_infobip(bulk_id: str, api_key: str, base_url: str) -> List[Dict]:
    """
    🔥 NUEVO: Consulta TODOS los datos de un bulkId manejando la paginación.
    El endpoint /sms/2/reports solo devuelve 1000 registros por petición.
    
    Args:
        bulk_id: ID del lote
        api_key: API key de Infobip
        base_url: URL base de Infobip
    
    Returns:
        Lista con TODOS los resultados
    """
    todos_los_resultados = []
    offset = 0
    limite = 1000
    max_paginas = 50
    pagina = 0
    
    while pagina < max_paginas:
        url = f"{base_url}/sms/3/reports"  # 🔥 MODIFICADO: v3
        headers = {
            "Authorization": f"App {api_key}",
            "Accept": "application/json"
        }
        params = {
            "bulkId": bulk_id,
            "limit": limite,
            "offset": offset
        }
        
        try:
            response = requests.get(url, headers=headers, params=params, timeout=30)
            
            if response.status_code != 200:
                logger.error(f"❌ Error {response.status_code} consultando {bulk_id}: {response.text[:200]}")
                break
            
            data = response.json()
            resultados = data.get("results", [])
            
            if not resultados:
                break
            
            todos_los_resultados.extend(resultados)
            
            # Si obtuvimos menos del límite, ya no hay más
            if len(resultados) < limite:
                break
            
            offset += limite
            pagina += 1
            
        except Exception as e:
            logger.error(f"❌ Error en paginación de {bulk_id}: {e}")
            break
    
    logger.info(f"📊 {bulk_id}: {len(todos_los_resultados)} registros obtenidos")
    return todos_los_resultados


def actualizar_estados_pendientes(api_key: str, base_url: str, client=None):
    """
    🔥 NUEVO: Consulta el estado de mensajes PENDIENTES en BigQuery.
    Guarda los resultados en SmsDeliveryReports y actualiza SmsLog.
    
    Esta función debe ejecutarse periódicamente (cada 10 minutos).
    """
    from google.cloud import bigquery
    
    if client is None:
        logger.error("❌ Se requiere cliente de BigQuery")
        return {"success": False, "message": "Cliente de BigQuery requerido"}
    
    logger.info("🔄 Iniciando polling de estados pendientes...")
    
    # 1. Mensajes sin estado final en SmsLog (últimas 48h), con o sin message_id:
    #    los que no lo tienen se actualizan por lote y teléfono.
    query_pendientes = f"""
        SELECT DISTINCT message_id, telefono, bulk_id, campana, usuario
        FROM `{SMS_LOG_TABLE}`
        WHERE UPPER(resultado) IN ('PENDIENTE', 'ENVIADO', 'SIN_REPORTE', 'PENDING')
          AND bulk_id IS NOT NULL AND bulk_id != ''
          AND fecha_envio >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {POLLING_MAX_HOURS} HOUR)
        LIMIT {POLLING_BATCH_SIZE}
    """
    
    try:
        pendientes = list(client.query(query_pendientes).result())
    except Exception as e:
        logger.error(f"❌ Error consultando pendientes: {e}")
        return {"success": False, "message": str(e)}
    
    if not pendientes:
        logger.info("✅ No hay mensajes pendientes")
        return {"success": True, "actualizados": 0, "sin_cambios": 0}
    
    logger.info(f"📋 {len(pendientes)} mensajes pendientes encontrados")
    
    # 2. Agrupar por bulkId para optimizar consultas
    por_bulk = {}
    for p in pendientes:
        bulk_id = p.bulk_id
        if bulk_id not in por_bulk:
            por_bulk[bulk_id] = []
        por_bulk[bulk_id].append(p)
    
    total_actualizados = 0
    total_sin_cambios = 0
    total_errores = 0
    
    # 3. Consultar cada bulkId a Infobip
    for bulk_id, mensajes in por_bulk.items():
        logger.info(f"🔍 Consultando bulkId: {bulk_id} ({len(mensajes)} mensajes)")
        
        todos_los_datos = consultar_estados_infobip(bulk_id, api_key, base_url)
        
        if not todos_los_datos:
            logger.warning(f"⚠️ Sin datos para {bulk_id}")
            continue
        
        # 4. Guardar en SmsDeliveryReports
        registros = []
        for item in todos_los_datos:
            status = item.get("status", {})
            error = item.get("error", {})
            price = item.get("price", {})
            
            registros.append({
                "message_id": item.get("messageId"),
                "bulk_id": bulk_id,
                "telefono": item.get("to"),
                "estado": status.get("groupName", "PENDING"),
                "status_name": status.get("name", ""),
                "status_description": status.get("description", ""),
                "error_name": error.get("name", ""),
                "error_description": error.get("description", ""),
                "sent_at": item.get("sentAt"),
                "done_at": item.get("doneAt"),
                "precio": price.get("pricePerMessage", 0),
                "currency": price.get("currency", "COP"),
                "fecha_consulta": datetime.now(timezone.utc).isoformat(),
                "campana": mensajes[0].campana if mensajes else "",
                "usuario": mensajes[0].usuario if mensajes else "",
                "fecha_creacion": datetime.now(timezone.utc).isoformat(),
            })
        
        if registros:
            try:
                errors = client.insert_rows_json(SMS_DELIVERY_REPORTS_TABLE, registros)
                if errors:
                    logger.error(f"Error guardando reportes: {errors}")
                    total_errores += 1
                else:
                    logger.info(f"✅ {len(registros)} reportes guardados en SmsDeliveryReports")
            except Exception as e:
                logger.error(f"Error guardando reportes: {e}")
                total_errores += 1
        
        # 5. Actualizar estado en SmsLog
        actualizados = actualizar_estados_sms_log(client, todos_los_datos, bulk_id=bulk_id)
        total_actualizados += actualizados
        total_sin_cambios += len(todos_los_datos) - actualizados
    
    logger.info(f"✅ Polling completado: {total_actualizados} actualizados, {total_sin_cambios} sin cambios, {total_errores} errores")
    
    return {
        "success": True,
        "actualizados": total_actualizados,
        "sin_cambios": total_sin_cambios,
        "errores": total_errores,
        "bulkids_procesados": len(por_bulk)
    }


def estado_sms_desde_infobip(item: Dict) -> Tuple[str, str]:
    """(resultado para SmsLog, error) según el groupName de Infobip."""
    grupo = (item.get("status") or {}).get("groupName", "PENDING")
    if grupo == "DELIVERED":
        return "ENTREGADO", ""
    if grupo in ("UNDELIVERABLE", "REJECTED", "EXPIRED"):
        return "FALLIDO", (item.get("error") or {}).get("description", "") or grupo
    if grupo in ("PENDING", "ACCEPTED"):
        return "PENDIENTE", ""
    return "SIN_REPORTE", ""


def actualizar_estados_sms_log(client, resultados: List[Dict], bulk_id: str = None) -> int:
    """
    Actualiza el estado en SmsLog con dos MERGE (no un UPDATE por mensaje):
    por message_id y, para las filas guardadas sin message_id, por lote y
    últimos 10 dígitos del teléfono (de paso les guarda el message_id).
    Devuelve cuántas filas cambiaron.
    """
    from google.cloud import bigquery

    if not resultados:
        return 0
    por_id, por_telefono = {}, {}
    for item in resultados:
        resultado, error = estado_sms_desde_infobip(item)
        fila = (item.get("messageId") or "", telefono_10(item.get("to")), resultado, error[:500])
        if fila[0]:
            por_id[fila[0]] = fila
        if fila[1]:
            por_telefono.setdefault(fila[1], fila)  # una fila por teléfono: MERGE no admite duplicados

    def struct_array(filas):
        return bigquery.ArrayQueryParameter("reportes", "STRUCT", [
            bigquery.StructQueryParameter(
                None,
                bigquery.ScalarQueryParameter("message_id", "STRING", f[0]),
                bigquery.ScalarQueryParameter("tel10", "STRING", f[1]),
                bigquery.ScalarQueryParameter("resultado", "STRING", f[2]),
                bigquery.ScalarQueryParameter("error", "STRING", f[3]),
            ) for f in filas])

    actualizados = 0
    consultas = [
        (f"""
            MERGE `{SMS_LOG_TABLE}` T
            USING UNNEST(@reportes) R
            ON T.message_id = R.message_id AND R.message_id != ''
            WHEN MATCHED AND T.resultado != R.resultado THEN UPDATE SET
              resultado = R.resultado, error = R.error, fecha_actualizacion = CURRENT_TIMESTAMP()
        """, list(por_id.values()), []),
    ]
    if bulk_id and por_telefono:
        consultas.append((f"""
            MERGE `{SMS_LOG_TABLE}` T
            USING UNNEST(@reportes) R
            ON T.bulk_id = @bulk_id AND IFNULL(T.message_id, '') = ''
               AND RIGHT(REGEXP_REPLACE(IFNULL(T.telefono, ''), r'\\D', ''), 10) = R.tel10
            WHEN MATCHED THEN UPDATE SET
              resultado = R.resultado, error = R.error, message_id = R.message_id,
              fecha_actualizacion = CURRENT_TIMESTAMP()
        """, list(por_telefono.values()), [bigquery.ScalarQueryParameter("bulk_id", "STRING", bulk_id)]))

    for sql, filas, extra in consultas:
        if not filas:
            continue
        try:
            job = client.query(sql, job_config=bigquery.QueryJobConfig(
                query_parameters=[struct_array(filas), *extra]))
            job.result()
            actualizados += job.num_dml_affected_rows or 0
        except Exception as e:
            logger.error(f"❌ Error actualizando estados en SmsLog: {e}")
    return actualizados


def guardar_programacion(
    query: str,
    plantilla: str,
    *,
    campaign: str = "",
    usuario: str = "",
    allow_resend: bool = False,
    total_dest: int = 0,
    tipo_programacion: str = "simple",
    fecha_programada: str = None,
    hora_inicio: str = None,
    fecha_fin: str = None,
    filas_excel: str = None,
    link_whatsapp: str = None
) -> int:

    try:

        fecha_prog = None

        if fecha_programada:
            fecha_prog = datetime.fromisoformat(
                fecha_programada
            )

        nueva_programacion = ProgramacionSms(
            tipo_programacion=tipo_programacion,
            consulta_sql=query,
            plantilla=plantilla,
            campana=campaign or "",
            usuario=usuario or "",
            estado="pendiente",
            total_destinatarios=total_dest,
            confirmar_reenvio=allow_resend,
            fecha_programada=fecha_prog,
            hora_inicio=hora_inicio,
            fecha_fin=fecha_fin,
            filas_excel=filas_excel,
            link_whatsapp=link_whatsapp
        )

        db.session.add(nueva_programacion)
        db.session.commit()

        logger.info(
            f"✅ Programación guardada en SQLite: "
            f"{nueva_programacion.id} "
            f"({tipo_programacion})"
        )

        return nueva_programacion.id

    except Exception as e:

        db.session.rollback()

        logger.exception(
            "❌ Error guardando programación en SQLite"
        )

        raise SmsServiceError(
            f"No se pudo guardar la programación: {e}"
        )


def verificar_lista_negra(client, phones: List[str]) -> Set[str]:
    """Verifica qué números están en la lista negra."""
    if not phones:
        return set()
    
    if not client:
        raise SmsServiceError("No hay conexión a BigQuery para validar lista negra.")
    
    try:
        from google.cloud import bigquery
        
        # Normalizar a 10 dígitos para comparar con la tabla
        phones_10 = set()
        for p in phones:
            cleaned = limpiar_numero(p)
            if not cleaned:
                continue
            # Convertir a formato 10 dígitos (sin 57)
            if cleaned.startswith("57") and len(cleaned) == 12:
                phones_10.add(cleaned[2:])
            elif len(cleaned) == 10:
                phones_10.add(cleaned)
        
        if not phones_10:
            return set()
        
        # Consultar la tabla
        query = f"""
            SELECT DISTINCT Telefono AS telefono
            FROM `{BLACKLIST_TABLE}`
            WHERE Telefono IN UNNEST(@telefonos)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("telefonos", "STRING", list(phones_10))
            ]
        )
        
        rows = client.query(query, job_config=job_config).result()
        blocked_10 = {str(row.telefono).strip() for row in rows}
        
        logger.info(f"📋 Lista negra encontrados: {blocked_10}")
        
        # Encontrar qué teléfonos originales están bloqueados
        result = set()
        for p in phones:
            cleaned = limpiar_numero(p)
            if not cleaned:
                continue
            
            # Convertir a 10 dígitos para comparar
            if cleaned.startswith("57") and len(cleaned) == 12:
                phone_10 = cleaned[2:]
            else:
                phone_10 = cleaned
            
            if phone_10 in blocked_10:
                result.add(p)
        
        if result:
            logger.info(f"🚫 {len(result)} números en lista negra: {sorted(result)}")
        else:
            logger.info(f"✅ Ningún número en lista negra")
        
        return result
    
    except SmsServiceError:
        raise
    except Exception as e:
        logger.error(f"Error al validar lista negra: {e}")
        raise SmsServiceError(f"Error al validar lista negra: {e}")


def verificar_duplicados(client, phones: List[str]) -> Set[str]:
    """Verifica qué números ya recibieron SMS hoy."""
    if not phones or not client:
        return set()
    
    try:
        from google.cloud import bigquery
        
        # Consultar envíos del día actual
        query = f"""
            SELECT DISTINCT telefono
            FROM `{SMS_LOG_TABLE}`
            WHERE DATE(fecha_envio, 'America/Bogota') = CURRENT_DATE('America/Bogota')
              AND telefono IN UNNEST(@telefonos)
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter("telefonos", "STRING", phones)
            ]
        )
        
        rows = client.query(query, job_config=job_config).result()
        duplicates = {row.telefono for row in rows}
        
        if duplicates:
            logger.info(f"🔄 {len(duplicates)} números ya recibieron SMS hoy")
        
        return duplicates
    
    except Exception as e:
        logger.warning(f"Error verificando duplicados: {e}")
        return set()


def aplicar_validaciones(
    rows: List[Dict], 
    plantilla: str, 
    client, 
    allow_resend: bool = False
) -> Tuple[List[Dict], Dict]:
    """Aplica validaciones de lista negra y duplicados."""
    prepared, details = preparar_sms(rows, plantilla)
    
    if not prepared:
        return [], details
    
    phones = [item["phone"] for item in prepared]
    
    blocked = verificar_lista_negra(client, phones)
    duplicates = verificar_duplicados(client, phones)
    
    # Excluir números bloqueados
    allowed = [item for item in prepared if item["phone"] not in blocked]
    
    # Excluir duplicados (a menos que se permita reenvío)
    if not allow_resend and duplicates:
        allowed = [item for item in allowed if item["phone"] not in duplicates]
    
    details.update({
        "total_consulta": len(rows),
        "excluidos_lista_negra": len(blocked),
        "telefonos_lista_negra": sorted(blocked),
        "duplicados_hoy": len(duplicates),
        "telefonos_duplicados": sorted(duplicates),
        "total_validos": len(allowed)
    })
    
    return allowed, details


def obtener_lista_negra(client) -> List[Dict]:
    """Obtiene la lista negra de SMS (listas_negras.LISTA_NEGRA_SMS)."""
    query = f"""
        SELECT 
            Telefono AS telefono,
            Motivo AS motivo,
            Fecha AS fecha_creacion
        FROM `{BLACKLIST_TABLE}`
        ORDER BY Fecha DESC
        LIMIT 500
    """
    try:
        df = client.query(query).to_dataframe()
        return df.to_dict('records') if not df.empty else []
    except Exception as e:
        logger.error(f"Error obteniendo lista negra: {e}")
        return []
