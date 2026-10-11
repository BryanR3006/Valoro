
import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

import pandas as pd

logger = logging.getLogger(__name__)

EMAIL_LOG_TABLE = "capable-arbor-209819.Temporal.EmailLog"
EMAIL_COLUMNS = ("email", "correo", "e-mail", "mail", "correo_electronico")


class EmailServiceError(Exception):
    """Error personalizado para el servicio de email."""
    pass


def detectar_columna_email(rows: List[Dict]) -> str:
    """Detecta la columna de email en los datos."""
    if not rows:
        raise EmailServiceError("La consulta no devolvió registros.")
    
    columns = {str(key).strip().lower(): str(key) for key in rows[0]}
    
    for candidate in EMAIL_COLUMNS:
        if candidate in columns:
            return columns[candidate]
    
    cols_disponibles = ", ".join(rows[0].keys())
    raise EmailServiceError(
        f"No se identificó una columna de email. Columnas disponibles: {cols_disponibles}. "
        "Use un alias como 'email', 'correo', 'mail'."
    )


def extraer_variables(texto: str) -> List[str]:
    """Extrae variables {{nombre}} del texto."""
    return re.findall(r"{{(.*?)}}", texto)


def _clave_variable(nombre) -> str:
    """Clave para comparar una variable con una columna: sin mayúsculas y con _ en vez de espacios."""
    return re.sub(r"\s+", "_", str(nombre).strip().lower())


def construir_contenido(row: Dict, plantilla: str, si_falta: Optional[str] = "") -> str:
    """
    Reemplaza las {{variables}} con los datos de la fila. Si la fila no trae la
    columna se pone si_falta; con si_falta=None la variable queda tal cual.
    """
    datos = {_clave_variable(k): v for k, v in row.items()}

    def reemplazo(m):
        clave = _clave_variable(m.group(1))
        if clave not in datos:
            return m.group(0) if si_falta is None else si_falta
        valor = datos[clave]
        return "" if valor is None else str(valor)

    return re.sub(r"{{\s*([^{}]+?)\s*}}", reemplazo, plantilla or "")


def _vacio(valor) -> bool:
    return valor is None or str(valor).strip() == "" or str(valor).strip().lower() == "nan"


def vista_previa_email(rows: List[Dict], plantilla: str, asunto: str, email_column: str,
                       limite: int = 5) -> List[Dict]:
    """
    Vista previa con los datos reales de las primeras filas: el HTML y el asunto
    como los recibiría cada cliente. "vacias" son las variables que en esa fila
    vienen sin dato (saldrían en blanco).
    """
    variables = variables_de_plantilla(plantilla, asunto)
    items = []
    for row in rows[:limite]:
        datos = {_clave_variable(k): v for k, v in row.items()}
        items.append({
            "email": str(row.get(email_column) or "").strip(),
            "asunto": construir_contenido(row, asunto, si_falta=None) if asunto else "(Sin asunto)",
            "contenido": construir_contenido(row, plantilla, si_falta=None),
            "vacias": [v for v in variables if _clave_variable(v) in datos and _vacio(datos[_clave_variable(v)])],
        })
    return items


def preview_email(rows: List[Dict], plantilla: str, asunto: str, limit: int = 3) -> Dict:
    """Genera una vista previa del email."""
    try:
        email_column = detectar_columna_email(rows)
        variables = extraer_variables(plantilla)
        
        preview_items = []
        for row in rows[:limit]:
            email = str(row.get(email_column, ""))
            contenido = construir_contenido(row, plantilla)
            asunto_personalizado = construir_contenido(row, asunto)
            
            preview_items.append({
                "email": email,
                "asunto": asunto_personalizado,
                "contenido": contenido
            })
        
        return {
            "success": True,
            "total_filas": len(rows),
            "email_column": email_column,
            "variables": variables,
            "preview": preview_items
        }
    
    except EmailServiceError as e:
        return {"success": False, "message": str(e)}


def guardar_email_log(client, registros: List[Dict]) -> None:
    """Guarda registros en EmailLog usando INSERT SQL con escaping seguro."""
    if not client or not registros:
        return
    
    logger.info(f"📧 Guardando {len(registros)} registros con INSERT SQL")
    
    try:
        for registro in registros:
            id_val = _escape_sql(registro.get("id", ""))
            email_val = _escape_sql(registro.get("email", ""))
            asunto_val = _escape_sql(registro.get("asunto", ""))
            contenido_val = _escape_sql(registro.get("contenido", ""))
            campana_id_val = _escape_sql(registro.get("campana_id", ""))
            campana_nombre_val = _escape_sql(registro.get("campana_nombre", ""))
            fecha_envio_val = registro.get("fecha_envio", "")
            resultado_val = _escape_sql(registro.get("resultado", ""))
            bulk_id_val = _escape_sql(registro.get("bulk_id", ""))
            error_val = _escape_sql(registro.get("error", ""))
            campana_val = _escape_sql(registro.get("campana", ""))
            usuario_val = _escape_sql(registro.get("usuario", ""))
            fecha_creacion_val = registro.get("fecha_creacion", "")
            fecha_actualizacion_val = registro.get("fecha_actualizacion", "")
            
            insert_sql = f"""
                INSERT INTO `{EMAIL_LOG_TABLE}` 
                (id, email, asunto, contenido, campana_id, campana_nombre, 
                 fecha_envio, resultado, bulk_id, error, campana, usuario, 
                 fecha_creacion, fecha_actualizacion)
                VALUES (
                    '{id_val}', '{email_val}', '{asunto_val}', '''{contenido_val}''', 
                    '{campana_id_val}', '{campana_nombre_val}', 
                    TIMESTAMP('{fecha_envio_val}'), '{resultado_val}', 
                    '{bulk_id_val}', '{error_val}', '{campana_val}', 
                    '{usuario_val}', TIMESTAMP('{fecha_creacion_val}'), 
                    TIMESTAMP('{fecha_actualizacion_val}')
                )
            """
            client.query(insert_sql).result()
        
        logger.info(f"✅ {len(registros)} registros guardados en EmailLog")
    except Exception as e:
        logger.error(f"Error guardando logs: {e}")

def traducir_a_member(html: str, mapeo: Dict[str, int]) -> str:
    import re
    
    resultado = html
    variables = re.findall(r"{{\s*([^{}]+?)\s*}}", html)
    
    for var in variables:
        var_original = var.strip()
        var_lower = var_original.lower()
        var_guion = var_lower.replace(" ", "_")
        var_sin_espacios = var_lower.replace(" ", "")
        
        campo_id = None
        if var_lower in mapeo:
            campo_id = mapeo[var_lower]
        elif var_guion in mapeo:
            campo_id = mapeo[var_guion]
        elif var_sin_espacios in mapeo:
            campo_id = mapeo[var_sin_espacios]
        
        if campo_id:
            resultado = re.sub(
                r"{{\s*" + re.escape(var_original) + r"\s*}}",
                f"%Member:CustomField{campo_id}%",
                resultado,
                flags=re.IGNORECASE
            )
            logger.info(f"✅ Mapeado: '{var_original}' → CustomField{campo_id}")
        else:
            logger.warning(f"⚠️ Variable sin mapeo: '{var_original}' (buscada como: {var_lower}, {var_guion}, {var_sin_espacios})")
    
    return resultado

def validar_variables_plantilla(rows: List[Dict], plantilla: str, asunto: str = "") -> Dict:
    """
    Valida que todas las variables de la plantilla existan en los datos.
    Retorna errores si faltan columnas.
    """
    import re
    
    # Extraer todas las variables de la plantilla y el asunto
    vars_plantilla = re.findall(r"{{\s*([^{}]+?)\s*}}", plantilla)
    vars_asunto = re.findall(r"{{\s*([^{}]+?)\s*}}", asunto) if asunto else []
    todas_vars = set(vars_plantilla + vars_asunto)
    
    if not rows:
        return {
            "valido": False,
            "error": "La consulta no devolvió registros.",
            "variables_faltantes": list(todas_vars),
            "variables_encontradas": []
        }
    
    # Columnas disponibles en la primera fila
    columnas_disponibles = {_clave_variable(k) for k in rows[0].keys()}
    
    # Verificar cada variable
    faltantes = []
    encontradas = []
    
    for var in todas_vars:
        if _clave_variable(var) in columnas_disponibles:
            encontradas.append(var)
        else:
            faltantes.append(var)
    
    return {
        "valido": len(faltantes) == 0,
        "error": f"Faltan {len(faltantes)} variables en la consulta: {', '.join(faltantes)}" if faltantes else None,
        "variables_faltantes": faltantes,
        "variables_encontradas": encontradas,
        "total_variables": len(todas_vars),
        "columnas_disponibles": list(columnas_disponibles)
    }

def _escape_sql(valor: str) -> str:
    """Escapa un valor para ser usado en SQL."""
    if valor is None:
        return ""
    # Escapar backslash primero, luego comillas simples
    return str(valor).replace("\\", "\\\\").replace("'", "\\'")


# ==================== ENVÍO DE UNA CAMPAÑA DE EMAIL ====================
# Lo usan el envío inmediato (/api/email/enviar), los programados
# (execute_email_schedule) y, más adelante, el flujo multicanal: por eso recibe
# filas ya consultadas y no SQL.

from listas_negras import LISTA_NEGRA_EMAIL

EMAIL_TUTELA_TABLE = LISTA_NEGRA_EMAIL  # se cambia en listas_negras.py

# Columna de la consulta -> CustomField de la cuenta de email. Se completa con los
# campos personalizados que devuelve la API (por su nombre).
MAPEO_CAMPOS_DEFECTO = {
    "customer_name": 1, "nombre": 1, "customer_id": 3, "id_cliente": 3,
    "cuotas": 5, "segmento": 7, "link_pago": 8, "link": 8,
    "campaign_id": 10, "id_campana": 10, "id_mensaje": 11,
    "descripcion_campana": 12, "objetivo_campana": 13,
    "nombre_banco": 14, "nombre_campana": 15,
    "canal_ley": 16, "ley_contacto": 17,
    "id_asunto": 18, "asunto": 19,
    "tel1": 21, "telefono": 21, "celular": 21,
    "valor_pagar": 22, "porcentaje": 23, "fecha_pago": 24,
    "valor_pagar_2": 2, "valor_oferta_esp_2": 4,
    "porcentaje_2": 6, "link_2": 9, "fecha_pago_2": 20,
}


class ErrorListaNegraEmail(EmailServiceError):
    """No se pudo consultar la lista negra de emails: el envío se cancela."""


def hoy_colombia() -> str:
    """Fecha de hoy en Colombia (AAAA-MM-DD). En UTC, después de las 7 p. m. ya es mañana."""
    import pytz
    return datetime.now(pytz.timezone("America/Bogota")).strftime("%Y-%m-%d")


def _variantes(nombre: str) -> List[str]:
    base = str(nombre).strip().lower()
    return [base, base.replace(" ", "_"), base.replace(" ", "")]


def construir_mapeo_campos(email_config: Dict, campos_api: List[Dict]) -> Dict[str, int]:
    """
    Mapeo columna -> CustomField: el de config.json (email.field_mapping) o el
    de por defecto, más los campos personalizados de la cuenta (por su nombre).
    """
    mapeo = dict(email_config.get("field_mapping") or MAPEO_CAMPOS_DEFECTO)
    for campo in campos_api or []:
        for variante in _variantes(campo["name"]):
            mapeo[variante] = campo["id"]
    return mapeo


def campo_de(nombre: str, mapeo: Dict[str, int]) -> Optional[int]:
    """CustomField de una columna o variable (sin importar mayúsculas, espacios o guiones bajos)."""
    for variante in _variantes(nombre):
        if variante in mapeo:
            return mapeo[variante]
    return None


def variables_sin_campo(plantilla: str, asunto: str, mapeo: Dict[str, int]) -> List[str]:
    """Variables {{...}} que no tienen CustomField: saldrían literales en el correo."""
    variables = re.findall(r"{{\s*([^{}]+?)\s*}}", f"{plantilla or ''} {asunto or ''}")
    return sorted({v.strip() for v in variables if campo_de(v, mapeo) is None})


def _emails_en_tabla(client_bq, sql: str, emails: List[str], extra_params=None) -> set:
    """Ejecuta una consulta con @emails (ARRAY<STRING>) y devuelve el set de la columna email."""
    from google.cloud import bigquery
    config = bigquery.QueryJobConfig(query_parameters=[
        bigquery.ArrayQueryParameter("emails", "STRING", emails), *(extra_params or [])])
    return {str(r["email"]).strip().lower() for r in client_bq.query(sql, job_config=config).result()}


def filtrar_destinatarios(client_bq, rows: List[Dict], confirmar_reenvio: bool = False) -> Dict:
    """
    Quita correos inválidos, lista negra (Email_Tutela) y, salvo confirmar_reenvio,
    los ya enviados hoy (EmailLog, fecha de Colombia).
    Si la lista negra no carga lanza ErrorListaNegraEmail: no se envía.
    """
    from google.cloud import bigquery
    email_column = detectar_columna_email(rows)
    todos = [str(r.get(email_column) or "").strip().lower() for r in rows]
    validos = sorted({e for e in todos if "@" in e})
    invalidos = sum(1 for e in todos if "@" not in e)

    blacklist, duplicados = set(), set()
    if validos:
        try:
            blacklist = _emails_en_tabla(client_bq, f"""
                SELECT DISTINCT LOWER(EMAIL) AS email FROM `{EMAIL_TUTELA_TABLE}`
                WHERE LOWER(EMAIL) IN UNNEST(@emails)""", validos)
        except Exception as exc:
            raise ErrorListaNegraEmail(
                f"No se pudo consultar la lista negra de emails (Email_Tutela): {exc}. "
                "El envío se canceló.") from exc
        if not confirmar_reenvio:
            try:
                duplicados = _emails_en_tabla(client_bq, f"""
                    SELECT DISTINCT LOWER(email) AS email FROM `{EMAIL_LOG_TABLE}`
                    WHERE DATE(fecha_envio, 'America/Bogota') = @hoy AND LOWER(email) IN UNNEST(@emails)""",
                    validos, [bigquery.ScalarQueryParameter("hoy", "DATE", hoy_colombia())])
            except Exception as exc:
                logger.warning(f"⚠️ No se pudieron revisar los duplicados del día en EmailLog: {exc}")

    excluir = blacklist | duplicados
    vistos, a_enviar = set(), []
    for row in rows:
        email = str(row.get(email_column) or "").strip().lower()
        if "@" not in email or email in excluir or email in vistos:
            continue
        vistos.add(email)
        a_enviar.append(row)

    return {
        "email_column": email_column,
        "rows": a_enviar,
        "total_consulta": len(todos),
        "total_validos": len(validos),
        "invalidos": invalidos,
        "blacklist": len(blacklist),
        "duplicados": len(duplicados & set(validos)),
        "a_enviar": len(a_enviar),
    }


def _crear_contacto(client, email: str, campos: Dict[int, str]) -> Optional[int]:
    """Crea el contacto; si ya existe, lo borra y lo crea de nuevo con los datos de hoy."""
    resultado = client.crear_contacto(email, campos)
    if not resultado.get("success"):
        raise EmailServiceError(str(resultado.get("error", "Error creando contacto")))
    contact_id = resultado.get("data", {}).get("id")
    if contact_id:
        return contact_id
    if not resultado.get("ya_existe"):
        raise EmailServiceError("No se obtuvo ID del contacto")
    buscar = client.obtener_contactos(email=email)
    encontrados = buscar.get("data", {}).get("data", []) if buscar.get("success") else []
    if not encontrados:
        raise EmailServiceError("Contacto existente no encontrado")
    client.eliminar_contactos([encontrados[0]["id"]])
    nuevo = client.crear_contacto(email, campos)
    if nuevo.get("success") and nuevo.get("data", {}).get("id"):
        return nuevo["data"]["id"]
    raise EmailServiceError("No se pudo recrear el contacto")


def enviar_campana_email(client_bq, rows: List[Dict], plantilla: str, asunto: str, campana: str,
                         usuario: str, reply_email: str, confirmar_reenvio: bool,
                         email_config: Dict, email_client=None) -> Dict:
    """
    Envía una campaña de email a las filas dadas: filtros (lista negra y
    duplicados del día), validación de variables, contactos, lista, campaña,
    envío y EmailLog. Lanza EmailServiceError (o ErrorListaNegraEmail) si no se puede.
    """
    from services.email_client import EmailClient

    api_key = email_config.get("api_key", "")
    from_email = email_config.get("from_email", "")
    if not api_key:
        raise EmailServiceError("Configure email.api_key en config.json")
    if not from_email:
        raise EmailServiceError("Configure email.from_email en config.json")
    if not plantilla:
        raise EmailServiceError("La plantilla (HTML) es obligatoria.")

    filtro = filtrar_destinatarios(client_bq, rows, confirmar_reenvio)
    destinatarios, email_column = filtro["rows"], filtro["email_column"]
    if not destinatarios:
        raise EmailServiceError(
            f"No hay destinatarios después de los filtros (lista negra: {filtro['blacklist']}, "
            f"duplicados de hoy: {filtro['duplicados']}, inválidos: {filtro['invalidos']}).")

    validacion = validar_variables_plantilla(destinatarios, plantilla, asunto)
    if not validacion["valido"]:
        raise EmailServiceError(validacion["error"])

    client = email_client or EmailClient(api_key)
    campos_api = client.obtener_campos_personalizados()
    if not campos_api.get("success"):
        raise EmailServiceError("No se pudieron obtener los campos personalizados de la cuenta de email.")
    mapeo = construir_mapeo_campos(email_config, campos_api.get("data", []))
    sin_campo = variables_sin_campo(plantilla, asunto, mapeo)
    if sin_campo:
        raise EmailServiceError(
            f"Estas variables no tienen campo personalizado en la cuenta de email y saldrían "
            f"como texto: {', '.join(sin_campo)}. Créalas en la cuenta o quítalas de la plantilla.")

    lista = client.crear_lista(f"API_Email_{datetime.now().strftime('%Y%m%d_%H%M%S')}")
    if not lista.get("success"):
        raise EmailServiceError("No se pudo crear la lista de contactos")
    lista_id = lista["data"]["id"]

    contact_ids, enviados, errores = [], [], []
    for row in destinatarios:
        email = str(row.get(email_column) or "").strip()
        campos = {}
        for columna, valor in row.items():
            if valor is None or str(valor).strip() == "":
                continue
            campo_id = campo_de(columna, mapeo)
            if campo_id is not None:
                campos[campo_id] = str(valor).strip()
        try:
            contact_ids.append(_crear_contacto(client, email, campos))
            enviados.append(row)
        except Exception as exc:
            errores.append({"email": email, "error": str(exc)})
    if not contact_ids:
        raise EmailServiceError("No se pudieron crear contactos")
    client.suscribir_contactos(contact_ids, lista_id)

    nombre_campana = campana or f"Email {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    resultado = client.crear_campana({
        "name": nombre_campana,
        "subject": traducir_a_member(asunto, mapeo) if asunto else "Sin asunto",
        "fromAlias": email_config.get("from_alias") or "QNT",
        "fromEmail": from_email,
        "replyEmail": reply_email or from_email,
        "content": traducir_a_member(plantilla, mapeo),
        "mailListsIds": [lista_id],
    })
    if not resultado.get("success"):
        raise EmailServiceError("No se pudo crear la campaña")
    campana_id = resultado["data"]["id"]
    if not client.enviar_campana(campana_id, send_now=1).get("success"):
        raise EmailServiceError("No se pudo enviar la campaña")

    ahora = datetime.now(timezone.utc).isoformat()
    primera = enviados[0]
    asunto_ejemplo = construir_contenido(primera, asunto) if asunto else "Sin asunto"
    contenido_ejemplo = construir_contenido(primera, plantilla)[:1000]
    guardar_email_log(client_bq, [{
        "id": str(uuid4()),
        "email": str(row.get(email_column) or "").strip(),
        "asunto": asunto_ejemplo,
        "contenido": contenido_ejemplo,
        "campana_id": str(campana_id),
        "campana_nombre": campana or "",
        "fecha_envio": ahora,
        "resultado": "enviado",
        "bulk_id": str(campana_id),
        "error": "",
        "campana": campana or "",
        "usuario": usuario or "",
        "fecha_creacion": ahora,
        "fecha_actualizacion": ahora,
    } for row in enviados])

    return {
        "campana_id": campana_id,
        "enviados": len(enviados),
        "fallidos": len(errores),
        "errores": errores[:10],
        "blacklist_excluidos": filtro["blacklist"],
        "duplicados_excluidos": filtro["duplicados"],
        "invalidos": filtro["invalidos"],
    }


# ==================== PLANTILLAS GUARDADAS ====================

def variables_de_plantilla(html: str, asunto: str = "") -> List[str]:
    """Variables {{...}} del asunto y el HTML, sin repetir y en orden de aparición."""
    vistas, resultado = set(), []
    for var in re.findall(r"{{\s*([^{}]+?)\s*}}", f"{asunto or ''} {html or ''}"):
        clave = var.strip().lower()
        if clave not in vistas:
            vistas.add(clave)
            resultado.append(var.strip())
    return resultado


def errores_plantilla(nombre: str, asunto: str, html: str) -> List[str]:
    """Problemas de una plantilla antes de guardarla."""
    errores = []
    if not (nombre or "").strip():
        errores.append("El nombre es obligatorio.")
    elif len(nombre.strip()) > 120:
        errores.append("El nombre tiene más de 120 caracteres.")
    if not (html or "").strip():
        errores.append("El HTML no puede estar vacío.")
    if len(asunto or "") > 255:
        errores.append("El asunto tiene más de 255 caracteres.")
    for campo, texto in (("asunto", asunto), ("HTML", html)):
        sin_variables = re.sub(r"{{\s*[^{}]+?\s*}}", "", texto or "")
        if "{{" in sin_variables or "}}" in sin_variables:
            errores.append(f"El {campo} tiene una variable mal cerrada: cada {{{{ debe tener su }}}}.")
    return errores


def nombre_libre(base: str, existentes, sufijo: str) -> str:
    """
    Primer nombre libre: "<base> <sufijo>2", "... 3"... (sufijo "v" -> "<base> v2";
    sufijo "copia " -> "<base> copia 2"). Quita una versión vieja del final de base.
    """
    usados = {str(n).strip().lower() for n in existentes}
    base = re.sub(r"\s+v\d+$", "", base.strip()) if sufijo == "v" else base.strip()
    if sufijo.strip() == "copia" and f"{base} copia".lower() not in usados:
        return f"{base} copia"  # la primera copia va sin número
    n = 2
    while f"{base} {sufijo}{n}".lower() in usados:
        n += 1
    return f"{base} {sufijo}{n}"
