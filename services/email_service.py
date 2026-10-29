
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from uuid import uuid4

import pandas as pd

from datos_personales import enmascarar_email
from services.email_client import EmailTimeoutError
from services.errores import marcar_claro
from services.variables import buscar_columna, es_nombre

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


def construir_contenido(row: Dict, plantilla: str, si_falta: Optional[str] = "") -> str:
    """
    Reemplaza las {{variables}} con los datos de la fila. Si la fila no trae la
    columna se pone si_falta; con si_falta=None la variable queda tal cual.
    """
    columnas = list(row.keys())

    def reemplazo(m):
        # Misma comparación que SMS (services/variables.buscar_columna).
        columna = buscar_columna(m.group(1), columnas)
        if columna is None:
            return m.group(0) if si_falta is None else si_falta
        valor = row[columna]
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
        columnas = {v: buscar_columna(v, row.keys()) for v in variables}
        items.append({
            "email": str(row.get(email_column) or "").strip(),
            "asunto": construir_contenido(row, asunto, si_falta=None) if asunto else "(Sin asunto)",
            "contenido": construir_contenido(row, plantilla, si_falta=None),
            "vacias": [v for v, c in columnas.items() if c is not None and _vacio(row[c])],
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


# Filas de EmailLog por INSERT (14 parámetros por fila; BigQuery admite 10.000).
FILAS_POR_INSERT = 300
_COLUMNAS_EMAIL_LOG = ("id", "email", "asunto", "contenido", "campana_id", "campana_nombre", "fecha_envio",
                       "resultado", "bulk_id", "error", "campana", "usuario", "fecha_creacion",
                       "fecha_actualizacion")
_FECHAS_EMAIL_LOG = {"fecha_envio", "fecha_creacion", "fecha_actualizacion"}


def guardar_email_log(client, registros: List[Dict]) -> None:
    """
    Guarda los registros en EmailLog con INSERT de varias filas con parámetros
    (de a FILAS_POR_INSERT). Antes era un INSERT por correo: 800 correos, 800
    consultas seguidas a BigQuery (más de 15 minutos).
    """
    if not client or not registros:
        return
    from google.cloud import bigquery

    guardados = 0
    try:
        for inicio in range(0, len(registros), FILAS_POR_INSERT):
            parte = registros[inicio:inicio + FILAS_POR_INSERT]
            valores, parametros = [], []
            for i, registro in enumerate(parte):
                valores.append("(" + ", ".join(f"@{c}_{i}" for c in _COLUMNAS_EMAIL_LOG) + ")")
                for c in _COLUMNAS_EMAIL_LOG:
                    if c in _FECHAS_EMAIL_LOG:
                        parametros.append(bigquery.ScalarQueryParameter(f"{c}_{i}", "TIMESTAMP", registro.get(c) or None))
                    else:
                        parametros.append(bigquery.ScalarQueryParameter(f"{c}_{i}", "STRING", str(registro.get(c) or "")))
            client.query(
                f"INSERT INTO `{EMAIL_LOG_TABLE}` ({', '.join(_COLUMNAS_EMAIL_LOG)}) VALUES {', '.join(valores)}",
                job_config=bigquery.QueryJobConfig(query_parameters=parametros),
            ).result()
            guardados += len(parte)
        logger.info(f"✅ {guardados} registros guardados en EmailLog")
    except Exception as e:
        logger.error(f"Error guardando EmailLog ({guardados} de {len(registros)} guardados): {e}")


def traducir_a_member(html: str, mapeo: Dict[str, int]) -> str:
    """{{variable}} -> %Member:CustomFieldN% (el campo que da campo_de)."""
    resultado = html
    variables = re.findall(r"{{\s*([^{}]+?)\s*}}", html)

    for var in variables:
        var_original = var.strip()
        campo_id = campo_de(var_original, mapeo)

        if campo_id:
            resultado = re.sub(
                r"{{\s*" + re.escape(var_original) + r"\s*}}",
                f"%Member:CustomField{campo_id}%",
                resultado,
                flags=re.IGNORECASE
            )
            logger.info(f"✅ Mapeado: '{var_original}' → CustomField{campo_id}")
        else:
            logger.warning(f"⚠️ Variable sin campo personalizado: '{var_original}'")
    
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
    
    # Columnas disponibles en la primera fila (misma comparación que SMS:
    # services/variables.buscar_columna, con los alias del nombre).
    columnas_disponibles = list(rows[0].keys())

    # Verificar cada variable
    faltantes = []
    encontradas = []

    for var in todas_vars:
        if buscar_columna(var, columnas_disponibles) is not None:
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


class SinDestinatariosEmail(EmailServiceError):
    """La consulta no trajo registros o los filtros quitaron a todos: no se envía, pero no es una falla."""


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
    """
    CustomField de una columna o variable (sin importar mayúsculas, espacios o
    guiones bajos). El nombre del cliente (nombre, Nombre Cliente,
    customer_name...) va al campo de "nombre" si no tiene uno propio.
    """
    for variante in _variantes(nombre):
        if variante in mapeo:
            return mapeo[variante]
    if es_nombre(nombre):
        for alias in ("nombre", "customer_name", "nombre_cliente"):
            if alias in mapeo:
                return mapeo[alias]
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


def senuelos_email(email_config: Dict) -> List[Dict]:
    """
    Señuelos de email de config.json, sección email:
    "senuelos": [{"email": "...", "nombre": "..."}]. Reciben cada envío de email
    para comprobar que sale bien, como los señuelos de SMS. Van en config.json
    (no en git) porque son datos personales.
    """
    lista, vistos = [], set()
    for s in (email_config or {}).get("senuelos") or []:
        if not isinstance(s, dict):
            continue
        correo = str(s.get("email") or "").strip().lower()
        if "@" in correo and correo not in vistos:
            vistos.add(correo)
            lista.append({"email": correo, "nombre": str(s.get("nombre") or "").strip()})
    return lista


def filas_senuelos(base: Dict, email_column: str, senuelos: List[Dict]) -> List[Dict]:
    """
    Una fila por señuelo, copiada de la fila base (un cliente real, para que las
    variables de la plantilla tengan datos), con el email y el nombre del señuelo
    en las columnas de nombre (services/variables.es_nombre).
    """
    filas = []
    for s in senuelos:
        fila = dict(base)
        fila[email_column] = s["email"]
        if s["nombre"]:
            for columna in fila:
                if es_nombre(columna):
                    fila[columna] = s["nombre"]
        filas.append(fila)
    return filas


def filtrar_destinatarios(client_bq, rows: List[Dict], confirmar_reenvio: bool = False,
                          senuelos: Optional[List[Dict]] = None) -> Dict:
    """
    Quita correos inválidos, lista negra (Email_Tutela) y, salvo confirmar_reenvio,
    los ya enviados hoy (EmailLog, fecha de Colombia).
    Si la lista negra no carga lanza ErrorListaNegraEmail: no se envía.
    Señuelos (senuelos_email): si queda algún cliente, se agregan al final y nunca
    se excluyen (lista negra ni duplicados), como en SMS. Sin clientes no se les envía.
    """
    from google.cloud import bigquery
    if not rows:
        raise SinDestinatariosEmail("La consulta no trajo registros: no se envió nada.")
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

    # Señuelos: solo si hay clientes; si un señuelo vino en la consulta, va una sola vez.
    correos_senuelo = {s["email"] for s in senuelos or []}
    n_senuelos = 0
    clientes = [r for r in a_enviar if str(r.get(email_column) or "").strip().lower() not in correos_senuelo]
    if clientes and senuelos:
        a_enviar = clientes + filas_senuelos(clientes[0], email_column, senuelos)
        n_senuelos = len(senuelos)

    return {
        "email_column": email_column,
        "rows": a_enviar,
        "total_consulta": len(todos),
        "total_validos": len(validos),
        "invalidos": invalidos,
        "blacklist": len(blacklist),
        "duplicados": len(duplicados & set(validos)),
        "a_enviar": len(a_enviar),
        "senuelos": n_senuelos,
    }


# Contactos que se cargan a la vez en la plataforma (email.max_workers en config.json).
# De a uno, 800 contactos tomaban casi una hora; la API responde en menos de 1 s.
HILOS_CONTACTOS = 5
CONTACTOS_POR_PAGINA = 100        # máximo que devuelve contacts/getall
SUSCRIPCIONES_POR_LLAMADO = 100  # contacts/suscribe no acepta más (invalid_max_elements)


def campos_de_fila(row: Dict, mapeo: Dict[str, int], con_vacios: bool = False) -> Dict[int, str]:
    """
    CustomField -> valor de las columnas de la fila que tienen campo en la cuenta.
    con_vacios: también los vacíos (al actualizar un contacto, para borrar el
    dato de un envío anterior). Si dos columnas van al mismo campo gana la que trae dato.
    """
    campos = {}
    for columna, valor in row.items():
        campo_id = campo_de(columna, mapeo)
        if campo_id is None:
            continue
        texto = "" if _vacio(valor) else str(valor).strip()
        if texto or (con_vacios and campo_id not in campos):
            campos[campo_id] = texto
    return campos


def directorio_contactos(client, hilos: int = HILOS_CONTACTOS, max_paginas: Optional[int] = None) -> Optional[Dict[str, int]]:
    """
    email (minúsculas) -> id de todos los contactos de la cuenta, leídos de a
    100 (páginas en paralelo). Con max_paginas, si la cuenta tiene más páginas
    devuelve None: sale más barato buscar contacto por contacto.
    """
    def pagina(numero):
        return client.obtener_contactos(limit=CONTACTOS_POR_PAGINA, page=numero).get("data", {})

    primera = pagina(1)
    paginas = int(primera.get("last_page") or 1)
    if max_paginas is not None and paginas > max_paginas:
        return None
    datos = [primera]
    if paginas > 1:
        with ThreadPoolExecutor(max_workers=hilos) as ejecutor:
            datos += list(ejecutor.map(pagina, range(2, paginas + 1)))
    return {str(c["email"]).strip().lower(): c["id"]
            for d in datos for c in d.get("data", []) if c.get("email") and c.get("id")}


def suscribir_en_bloques(client, contact_ids: List[int], lista_id, tamano: int = SUSCRIPCIONES_POR_LLAMADO) -> None:
    """
    Suscribe los contactos a la lista de a `tamano` (la API acepta 100 por
    llamado). Si la API rechaza el bloque por grande (invalid_max_elements),
    lo parte en dos y lo vuelve a intentar.
    """
    from services.email_client import EmailClientError
    for i in range(0, len(contact_ids), tamano):
        bloque = contact_ids[i:i + tamano]
        try:
            client.suscribir_contactos(bloque, lista_id)
        except EmailClientError as exc:
            if "invalid_max_elements" not in str(exc) or len(bloque) == 1:
                raise
            logger.warning(f"⚠️ La API no acepta {len(bloque)} contactos por suscripción: se parten en dos.")
            suscribir_en_bloques(client, bloque, lista_id, max(1, len(bloque) // 2))


def _cargar_contacto(client, email: str, row: Dict, mapeo: Dict[str, int],
                     directorio: Optional[Dict[str, int]]) -> int:
    """
    Deja el contacto en la cuenta con los datos de esta fila y devuelve su id.
    Si ya existe se actualizan sus campos (contacts/edit): un llamado, y no se
    pierde su historial ni si se dio de baja. Si es nuevo se crea.
    """
    contact_id = (directorio or {}).get(email.lower())
    if not contact_id:
        resultado = client.crear_contacto(email, campos_de_fila(row, mapeo))
        if not resultado.get("success"):
            raise EmailServiceError(str(resultado.get("error", "Error creando contacto")))
        contact_id = resultado.get("data", {}).get("id")
        if contact_id:
            return contact_id
        if not resultado.get("ya_existe"):
            raise EmailServiceError("No se obtuvo ID del contacto")
        # Ya existía (no estaba en el directorio): se busca para actualizarlo.
        encontrados = client.obtener_contactos(email=email).get("data", {}).get("data", [])
        exacto = [c for c in encontrados if str(c.get("email") or "").strip().lower() == email.lower()]
        if not (exacto or encontrados):
            raise EmailServiceError("Contacto existente no encontrado")
        contact_id = (exacto or encontrados)[0]["id"]
    campos = campos_de_fila(row, mapeo, con_vacios=True)
    if campos:
        client.editar_contacto(contact_id, campos)
    return contact_id


def _crear_campana(client, datos: Dict, lista_id) -> int:
    """
    Crea la campaña y devuelve su ID. Si la API no responde a tiempo, la
    campaña pudo quedar creada: se busca por nombre y por la lista de este
    envío para no dejarla en borrador.
    """
    try:
        resultado = client.crear_campana(datos)
    except EmailTimeoutError as exc:
        logger.warning(f"⚠️ {exc}. Se busca si la campaña quedó creada.")
        time.sleep(5)
        encontrada = client.buscar_campana_por_nombre(datos["name"])
        if encontrada:
            detalle = client.detalle_campana(encontrada["id"]).get("data", {})
            if lista_id in [m.get("id") for m in detalle.get("maillists") or []]:
                logger.info(f"✅ La campaña sí quedó creada: ID {encontrada['id']}")
                return encontrada["id"]
        raise marcar_claro(EmailServiceError(
            "La API de email no respondió a tiempo y la campaña no quedó creada: no se envió nada. "
            "Intente de nuevo en unos minutos.")) from exc
    if not resultado.get("success") or not resultado.get("data", {}).get("id"):
        raise EmailServiceError("No se pudo crear la campaña")
    return resultado["data"]["id"]


def verificar_campana_enviada(client, campana_id, intentos: int = 6, espera: int = 5) -> Dict:
    """
    Confirma en la cuenta de email que la campaña salió: que exista, que ya no
    esté en borrador (Draft) y que tenga destinatarios. La API responde "ok" al
    enviar aunque la campaña no salga, por eso se revisa. Lanza EmailServiceError
    si no salió. Devuelve {"status", "recipients_count"}.
    """
    from services.email_client import EmailClientError
    detalle = {}
    for intento in range(intentos):
        try:
            detalle = client.detalle_campana(campana_id).get("data", {})
        except EmailClientError as exc:
            if "NotFound" in str(exc):
                raise EmailServiceError(
                    f"La campaña {campana_id} no existe en la cuenta de email configurada: revise "
                    f"email.api_key en config.json. No se envió nada.") from exc
            logger.warning(f"⚠️ No se pudo leer el estado de la campaña {campana_id}: {exc}")
        if detalle.get("status") and detalle["status"] != "Draft":
            break
        if intento < intentos - 1:
            time.sleep(espera)
    status = detalle.get("status") or "desconocido"
    destinatarios = int(detalle.get("recipients_count") or 0)
    logger.info(f"📧 Campaña {campana_id}: estado {status}, {destinatarios} destinatarios")
    if status == "Draft":
        raise EmailServiceError(
            f"La campaña {campana_id} quedó en borrador (Draft) en la plataforma de email: no se envió. "
            "Revísela allá o intente de nuevo.")
    if status == "desconocido":
        raise marcar_claro(EmailServiceError(
            f"No se pudo confirmar que la campaña {campana_id} salió (la API de email no respondió). "
            "Revise en la plataforma antes de reintentar, para no duplicar."))
    if not destinatarios and status == "Completed":
        raise EmailServiceError(f"La campaña {campana_id} terminó (Completed) sin destinatarios: no llegó a nadie.")
    return {"status": status, "recipients_count": destinatarios}


ZONA_COLOMBIA = timezone(timedelta(hours=-5))


def _revisar_horario_envio(campana: str) -> None:
    """EmailServiceError (mensaje claro) si ahora está fuera del horario de envío (Parámetros generales)."""
    from general_params import get_horario_envio
    from services.sms_horarios import MARGEN_FIN_ENVIO_MIN, fuera_de_horario
    aviso = fuera_de_horario(datetime.now(ZONA_COLOMBIA), *get_horario_envio(), margen_min=MARGEN_FIN_ENVIO_MIN)
    if aviso:
        logger.warning(f"⛔ Email de la campaña '{campana}' no enviado: {aviso}")
        raise marcar_claro(EmailServiceError(aviso))


def enviar_campana_email(client_bq, rows: List[Dict], plantilla: str, asunto: str, campana: str,
                         usuario: str, reply_email: str, confirmar_reenvio: bool,
                         email_config: Dict, email_client=None,
                         progreso: Optional[Callable[[int, int, int], None]] = None) -> Dict:
    """
    Envía una campaña de email a las filas dadas: filtros (lista negra y
    duplicados del día), validación de variables, contactos, lista, campaña,
    envío y EmailLog. Lanza EmailServiceError (o ErrorListaNegraEmail) si no se puede.
    progreso(hechos, total, con_error) se llama mientras se cargan los contactos.
    """
    from services.email_client import EmailClient

    _revisar_horario_envio(campana)
    api_key = email_config.get("api_key", "")
    from_email = email_config.get("from_email", "")
    if not api_key:
        raise EmailServiceError("Configure email.api_key en config.json")
    if not from_email:
        raise EmailServiceError("Configure email.from_email en config.json")
    if not plantilla:
        raise EmailServiceError("La plantilla (HTML) es obligatoria.")

    filtro = filtrar_destinatarios(client_bq, rows, confirmar_reenvio, senuelos_email(email_config))
    destinatarios, email_column = filtro["rows"], filtro["email_column"]
    if not destinatarios:
        raise SinDestinatariosEmail(
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

    # El nombre lleva un sufijo al azar: dos envíos en el mismo segundo (p. ej. dos
    # programaciones del mismo turno) chocaban con 'is_used'.
    lista = client.crear_lista(f"API_Email_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:6]}")
    if not lista.get("success"):
        raise EmailServiceError("No se pudo crear la lista de contactos")
    lista_id = lista["data"]["id"]

    # Contactos: se lee una vez el directorio de la cuenta (si sale más barato que
    # buscarlos de a uno) y cada contacto es un solo llamado, varios a la vez.
    hilos = max(1, min(int(email_config.get("max_workers") or HILOS_CONTACTOS), 10))
    total = len(destinatarios)
    inicio = time.time()
    try:
        directorio = directorio_contactos(client, hilos, max_paginas=max(1, total // 2))
    except Exception as exc:
        logger.warning(f"⚠️ No se pudo leer el directorio de contactos ({exc}): se buscan de a uno.")
        directorio = None

    contact_ids, enviados, errores = [], [], []
    with ThreadPoolExecutor(max_workers=hilos) as ejecutor:
        futuros = {}
        for row in destinatarios:
            email = str(row.get(email_column) or "").strip()
            futuros[ejecutor.submit(_cargar_contacto, client, email, row, mapeo, directorio)] = (email, row)
        for hechos, futuro in enumerate(as_completed(futuros), 1):
            email, row = futuros[futuro]
            try:
                contact_ids.append(futuro.result())
                enviados.append(row)
            except Exception as exc:
                errores.append({"email": enmascarar_email(email), "error": str(exc)})
                logger.warning(f"⚠️ No se pudo cargar el contacto {enmascarar_email(email)}: {exc}")
            if progreso and (hechos % 100 == 0 or hechos == total):
                progreso(hechos, total, len(errores))
    logger.info(f"📧 Contactos: {len(contact_ids)} de {total} cargados en {time.time() - inicio:.0f} s "
                f"({len(errores)} con error, {hilos} a la vez, directorio: {'sí' if directorio is not None else 'no'})")
    if not contact_ids:
        raise EmailServiceError(
            "No se pudo cargar ningún contacto en la plataforma de email"
            + (f": {errores[0]['error']}" if errores else "."))
    suscribir_en_bloques(client, contact_ids, lista_id)

    nombre_campana = campana or f"Email {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    datos_campana = {
        "name": nombre_campana,
        "subject": traducir_a_member(asunto, mapeo) if asunto else "Sin asunto",
        "fromAlias": email_config.get("from_alias") or "Valoro",
        "fromEmail": from_email,
        "replyEmail": reply_email or from_email,
        "content": traducir_a_member(plantilla, mapeo),
        "mailListsIds": [lista_id],
    }
    # Otra vez justo antes de crear y enviar la campaña: cargar contactos tarda, y
    # si el equipo se suspendió a mitad, al despertar no sale fuera de horario.
    _revisar_horario_envio(campana)
    campana_id = _crear_campana(client, datos_campana, lista_id)
    try:
        if not client.enviar_campana(campana_id, send_now=1).get("success"):
            raise EmailServiceError("No se pudo enviar la campaña")
    except EmailTimeoutError as exc:
        # Pudo salir igual: lo dice el estado de la campaña (abajo).
        logger.warning(f"⚠️ {exc}. Se revisa el estado de la campaña.")
    estado = verificar_campana_enviada(client, campana_id)

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
        "estado_campana": estado["status"],
        "destinatarios_campana": estado["recipients_count"],
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
