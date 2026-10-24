
import hashlib
import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from uuid import uuid4
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from datos_personales import enmascarar_email

logger = logging.getLogger(__name__)

BASE_URL = "https://api.esmsv.com/v1"
# La API a veces tarda más de 30 s en crear o enviar una campaña: con menos
# espera la campaña quedaba creada en borrador y nunca salía.
TIMEOUT_CAMPANA = 120
# A veces la API no acepta la conexión y Windows tarda 21 s en rendirse: con 6 s
# y reintentos un corte cuesta unos segundos y el contacto no se pierde. Solo se
# reintenta al conectar (la petición no salió: no se duplica nada).
TIMEOUT_CONEXION = 6
TIMEOUT_RESPUESTA = 30
REINTENTOS_CONEXION = 4
# Consultas de solo lectura (campos, contactos, campañas): también se reintentan
# si la respuesta tarda o se corta, porque repetir una lectura no cambia nada en
# la cuenta. El 22/10/2026 una sola demora de customfields/getall (más de 30 s)
# tumbó un email programado y dejó la recurrente en error.
INTENTOS_LECTURA = 3
ESPERAS_LECTURA = (2, 5)  # segundos antes del 2.º y del 3.er intento

# Campos personalizados: casi nunca cambian. Se guardan los últimos leídos (en
# memoria y en disco, por cuenta) para seguir enviando si la API no los entrega.
ARCHIVO_CAMPOS = Path(__file__).resolve().parent.parent / "email_campos_cache.json"
_campos_memoria = {}


def _cuenta(api_key: str) -> str:
    """Identificador de la cuenta para la caché (nunca se guarda la api_key)."""
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:16]


def _guardar_campos(api_key: str, campos: List[Dict]) -> None:
    _campos_memoria[_cuenta(api_key)] = (campos, datetime.now().isoformat(timespec="seconds"))
    try:
        ARCHIVO_CAMPOS.write_text(json.dumps({
            "cuenta": _cuenta(api_key), "fecha": datetime.now().isoformat(timespec="seconds"), "campos": campos,
        }, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        logger.warning(f"⚠️ No se pudieron guardar los campos personalizados en {ARCHIVO_CAMPOS.name}: {e}")


def _campos_guardados(api_key: str):
    """(campos, fecha) de los últimos campos leídos de esa cuenta, o (None, None)."""
    cuenta = _cuenta(api_key)
    if cuenta in _campos_memoria:
        return _campos_memoria[cuenta]
    try:
        datos = json.loads(ARCHIVO_CAMPOS.read_text(encoding="utf-8"))
        if datos.get("cuenta") == cuenta and datos.get("campos"):
            return datos["campos"], datos.get("fecha")
    except (OSError, ValueError):
        pass
    return None, None


class EmailClientError(Exception):
    """Error personalizado para el cliente de Email."""
    pass


# Códigos de error de la API que se ven en pantalla: qué pasó y qué hacer.
ERRORES_API = {
    "errorMsg_senderDomainNotVerified": (
        "El dominio del remitente (email.from_email en config.json) no está verificado en esta cuenta de "
        "email. Use un remitente de un dominio verificado en la cuenta (por ejemplo comercial@valoro.com.co) "
        "o verifique el dominio en la plataforma."),
    "errorMsg_campaignNotFound": "La campaña no existe en la cuenta de email configurada (revise email.api_key).",
}


def _error_api(codigo) -> str:
    """Texto del error de la API: la explicación si se conoce el código, si no el código tal cual."""
    for clave, texto in ERRORES_API.items():
        if clave in str(codigo):
            return f"{texto} ({clave})"
    return str(codigo)


class EmailTimeoutError(EmailClientError):
    """La API no respondió a tiempo: la operación pudo quedar hecha o no."""


class EmailClient:
    """Cliente para interactuar con la API de Email Marketing."""

    def __init__(self, api_key: str):
        if not api_key:
            raise EmailClientError("API Key de Email Marketing es requerida.")
        
        self.api_key = api_key
        self._hilo = threading.local()

    @property
    def session(self) -> requests.Session:
        """
        Sesión HTTP de este hilo (los contactos se cargan en paralelo y una
        sesión no se comparte entre hilos), con reintentos de conexión.
        """
        sesion = getattr(self._hilo, "sesion", None)
        if sesion is None:
            sesion = requests.Session()
            sesion.headers.update({
                "Authorization": self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json"
            })
            reintentos = Retry(total=REINTENTOS_CONEXION, connect=REINTENTOS_CONEXION, read=0, status=0,
                               other=0, backoff_factor=1, allowed_methods=None, raise_on_status=False)
            sesion.mount("https://", HTTPAdapter(max_retries=reintentos))
            self._hilo.sesion = sesion
        return sesion

    def _leer(self, metodo: str, url: str, **kwargs) -> requests.Response:
        """
        Petición de solo lectura con reintentos también si la respuesta tarda o
        se corta (INTENTOS_LECTURA). Las escrituras (crear, editar, enviar) no
        pasan por aquí: repetirlas podría duplicar.
        """
        kwargs.setdefault("timeout", (TIMEOUT_CONEXION, TIMEOUT_RESPUESTA))
        for intento in range(INTENTOS_LECTURA):
            try:
                return self.session.request(metodo, url, **kwargs)
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
                if intento == INTENTOS_LECTURA - 1:
                    raise
                espera = ESPERAS_LECTURA[min(intento, len(ESPERAS_LECTURA) - 1)]
                logger.warning(f"⚠️ La API de email no respondió ({url.split('/v1/', 1)[-1]}, {type(e).__name__}): "
                               f"intento {intento + 2} de {INTENTOS_LECTURA} en {espera} s")
                time.sleep(espera)

    # ==================================================
    # 📋 CONTACTOS
    # ==================================================

    def crear_contacto(self, email: str, campos_personalizados: Optional[Dict[int, str]] = None) -> Dict:
       
        url = f"{BASE_URL}/contacts/create"
        data = {"email": email}

        if campos_personalizados:
            for field_id, value in campos_personalizados.items():
                data[f"customFields[{field_id}]"] = value
        
        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_RESPUESTA))
            result = resp.json()
            
            if result.get("status") == "ok":
                logger.debug(f"✅ Contacto creado: {enmascarar_email(email)}")
                return {"success": True, "data": result.get("data", {})}
            elif result.get("status") == "error":
                if result.get("code") == "errorMsg_contactAlreadyExist":
                    logger.debug(f"Contacto ya existe: {enmascarar_email(email)}")
                    return {"success": True, "data": {"email": email}, "ya_existe": True}
                raise EmailClientError(_error_api(result.get("code", "Error desconocido")))
            return {"success": False, "error": result}
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")
        except EmailClientError:
            raise
        except Exception as e:
            raise EmailClientError(f"Error inesperado: {e}")

    def obtener_contactos(self, email: Optional[str] = None, list_id: Optional[int] = None,
                          limit: int = 100, page: int = 1) -> Dict:
        """
        Obtiene colección de contactos.
        POST /v1/contacts/getall
        """
        url = f"{BASE_URL}/contacts/getall"
        data = {"limit": limit, "page": page}
        
        if email:
            data["email"] = email
        if list_id:
            data["listId"] = list_id

        try:
            resp = self._leer("POST", url, data=data)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(_error_api(result.get("code", "Error desconocido")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def editar_contacto(self, contact_id: int, campos_personalizados: Dict[int, str]) -> Dict:
        """
        Actualiza los campos personalizados de un contacto que ya existe, en un
        solo llamado. POST /v1/contacts/edit (id + customFields[N]).
        """
        url = f"{BASE_URL}/contacts/edit"
        data = {"id": contact_id}
        for field_id, value in (campos_personalizados or {}).items():
            data[f"customFields[{field_id}]"] = value

        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_RESPUESTA))
            result = resp.json()

            if result.get("status") == "ok":
                return {"success": True}
            raise EmailClientError(_error_api(result.get("code", "Error desconocido")))

        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def eliminar_contactos(self, contact_ids: List[int]) -> Dict:
        """
        Elimina contactos.
        POST /v1/contacts/delete
        """
        url = f"{BASE_URL}/contacts/delete"
        data = {}
        for i, cid in enumerate(contact_ids):
            data[f"contactsIds[{i}]"] = cid
        
        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_RESPUESTA))
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result}
            raise EmailClientError(_error_api(result.get("code", "Error desconocido")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    # ==================================================
    # 📋 LISTAS DE CONTACTOS
    # ==================================================

    def crear_lista(self, nombre: str, _reintento: bool = False) -> Dict:
        """
        Crea una lista de contactos.
        POST /v1/listscontacts/create
        """
        url = f"{BASE_URL}/listscontacts/create"
        data = {"name": nombre}
        
        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_RESPUESTA))
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            elif result.get("status") == "error":
                codigo = result.get("code") or {}
                nombre_err = codigo.get("name") if isinstance(codigo, dict) else None
                if isinstance(nombre_err, list):
                    nombre_err = nombre_err[0] if nombre_err else None
                if nombre_err == "is_used" and not _reintento:
                    # El nombre ya existe en la cuenta: otro intento con sufijo (la
                    # lista existente no sirve, el envío necesita su id).
                    logger.warning(f"⚠️ Lista ya existe: {nombre}; se crea con otro nombre")
                    return self.crear_lista(f"{nombre}_{uuid4().hex[:6]}", _reintento=True)
                raise EmailClientError(_error_api(result.get("code", "Error")))
            return {"success": False, "error": result}
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def suscribir_contactos(self, contact_ids: List[int], list_id: int) -> Dict:
        """
        Suscribe contactos a una lista.
        POST /v1/contacts/suscribe
        """
        url = f"{BASE_URL}/contacts/suscribe"
        data = {"listId": list_id}
        
        for i, cid in enumerate(contact_ids):
            data[f"contactsIds[{i}]"] = cid
        
        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, 60))
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result}
            raise EmailClientError(_error_api(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    # ==================================================
    # 📧 CAMPAÑAS
    # ==================================================

    def crear_campana(self, datos: Dict) -> Dict:
       
        
        url = f"{BASE_URL}/campaign/create"
        data = {}
        
        for key, value in datos.items():
            if isinstance(value, list):
                for i, v in enumerate(value):
                    data[f"{key}[{i}]"] = v
            else:
                data[key] = value
        
        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_CAMPANA))
            result = resp.json()

            if result.get("status") == "ok":
                logger.info(f"✅ Campaña creada: ID {result.get('data', {}).get('id')}")
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(_error_api(result.get("code", "Error")))

        except requests.exceptions.Timeout as e:
            raise EmailTimeoutError(f"La API de email no respondió a tiempo creando la campaña: {e}")
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def enviar_campana(self, campana_id: int, send_now: int = 1, send_date: Optional[str] = None) -> Dict:

        url = f"{BASE_URL}/campaign/send"
        data = {"id": campana_id, "sendNow": send_now}

        if send_now == 0 and send_date:
            data["sendDate"] = send_date

        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_CAMPANA))
            result = resp.json()

            if result.get("status") == "ok":
                logger.info(f"✅ Campaña {campana_id}: la API aceptó el envío")
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(_error_api(result.get("code", "Error")))

        except requests.exceptions.Timeout as e:
            raise EmailTimeoutError(f"La API de email no respondió a tiempo enviando la campaña {campana_id}: {e}")
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def buscar_campana_por_nombre(self, nombre: str) -> Optional[Dict]:
        """La campaña más reciente con ese nombre exacto (o None)."""
        resultado = self.obtener_campanas(filtro=nombre, limit=20)
        campanas = [c for c in resultado.get("data", {}).get("data", []) if c.get("name") == nombre]
        return max(campanas, key=lambda c: c.get("id") or 0) if campanas else None

    def obtener_campanas(self, filtro: Optional[str] = None, status: Optional[str] = None,
                         limit: int = 100, page: int = 1) -> Dict:
        """
        Obtiene colección de campañas.
        POST /v1/campaign/getAll
        """
        url = f"{BASE_URL}/campaign/getAll"
        data = {"limit": limit, "page": page}
        
        if filtro:
            data["filter"] = filtro
        if status:
            data["status"] = status

        try:
            resp = self._leer("POST", url, data=data)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(_error_api(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def detalle_campana(self, campana_id: int) -> Dict:
        """
        Obtiene detalle de una campaña.
        GET /v1/campaign/:campaign_id
        """
        url = f"{BASE_URL}/campaign/{campana_id}"

        try:
            resp = self._leer("GET", url, timeout=(TIMEOUT_CONEXION, 60))
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(_error_api(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def check_status(self, campana_id: int) -> Dict:
        """
        Chequea si la campaña está lista para enviar.
        POST /v1/campaign/checkstatus
        """
        url = f"{BASE_URL}/campaign/checkstatus"
        data = {"id": campana_id}

        try:
            resp = self._leer("POST", url, data=data)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result}
            raise EmailClientError(_error_api(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def preview_html(self, campana_id: int, email: Optional[str] = None) -> Dict:
    
        url = f"{BASE_URL}/campaign/preview/html"
        data = {"id": campana_id}
        
        if email:
            data["email"] = email

        try:
            resp = self._leer("POST", url, data=data)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "html": result.get("html", "")}
            raise EmailClientError(_error_api(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def obtener_campos_personalizados(self) -> Dict:
        """
        Obtiene todos los campos personalizados.
        POST /v1/customfields/getall
        """
        url = f"{BASE_URL}/customfields/getall"
        data = {"limit": "100"}

        try:
            resp = self._leer("POST", url, data=data)
            result = resp.json()

            if result.get("status") == "ok":
                campos = result.get("data", {}).get("data", [])
                _guardar_campos(self.api_key, campos)
                return {"success": True, "data": campos}
            raise EmailClientError(_error_api(result.get("code", "Error")))

        except requests.exceptions.RequestException as e:
            # La API no respondió: los campos casi nunca cambian, se usan los últimos leídos.
            campos, fecha = _campos_guardados(self.api_key)
            if campos:
                logger.warning(f"⚠️ La API de email no entregó los campos personalizados ({type(e).__name__}): "
                               f"se usan los últimos leídos ({fecha}).")
                return {"success": True, "data": campos, "guardados": fecha}
            raise EmailClientError(f"Error de red: {e}")
    def crear_campo_personalizado(self, nombre: str, tipo: str = "Text field") -> Dict:
        """
        Crea un campo personalizado en la plataforma.
        POST /v1/customfields/create
        """
        url = f"{BASE_URL}/customfields/create"
        data = {
            "name": nombre,
            "type": tipo,
            "validation_type": "Do not Apply",
            "value_default": ""
        }
        
        try:
            resp = self.session.post(url, data=data, timeout=(TIMEOUT_CONEXION, TIMEOUT_RESPUESTA))
            result = resp.json()
            
            if result.get("status") == "ok":
                logger.info(f"✅ Campo creado: {nombre}")
                return {"success": True, "data": result.get("data", {})}
            elif result.get("status") == "error":
                if "is_used" in str(result.get("code", "")):
                    logger.warning(f"⚠️ El campo '{nombre}' ya existe")
                    return {"success": True, "ya_existe": True}
                raise EmailClientError(_error_api(result.get("code", "Error")))
            return {"success": False, "error": result}
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")
    