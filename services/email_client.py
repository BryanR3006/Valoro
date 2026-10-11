
import json
import logging
import time
from typing import Any, Dict, List, Optional

import requests

from datos_personales import enmascarar_email

logger = logging.getLogger(__name__)

BASE_URL = "https://api.esmsv.com/v1"


class EmailClientError(Exception):
    """Error personalizado para el cliente de Email."""
    pass


class EmailClient:
    """Cliente para interactuar con la API de Email Marketing."""

    def __init__(self, api_key: str):
        if not api_key:
            raise EmailClientError("API Key de Email Marketing es requerida.")
        
        self.api_key = api_key
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": self.api_key,
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json"
        })

    # ==================================================
    # 📋 CONTACTOS
    # ==================================================

    def crear_contacto(self, email: str, campos_personalizados: Optional[Dict[int, str]] = None) -> Dict:
       
        url = f"{BASE_URL}/contacts/create"
        data = {"email": email}
        logger.info(f"📧 Creando contacto en: {url}")
        logger.info(f"📧 Datos: {data}")
        

        if campos_personalizados:
            for field_id, value in campos_personalizados.items():
                data[f"customFields[{field_id}]"] = value
        
        try:
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                logger.info(f"✅ Contacto creado: {enmascarar_email(email)}")
                return {"success": True, "data": result.get("data", {})}
            elif result.get("status") == "error":
                if result.get("code") == "errorMsg_contactAlreadyExist":
                    logger.warning(f"⚠️ Contacto ya existe: {enmascarar_email(email)}")
                    return {"success": True, "data": {"email": email}, "ya_existe": True}
                raise EmailClientError(result.get("code", "Error desconocido"))
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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(result.get("code", "Error desconocido"))
        
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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result}
            raise EmailClientError(result.get("code", "Error desconocido"))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    # ==================================================
    # 📋 LISTAS DE CONTACTOS
    # ==================================================

    def crear_lista(self, nombre: str) -> Dict:
        """
        Crea una lista de contactos.
        POST /v1/listscontacts/create
        """
        url = f"{BASE_URL}/listscontacts/create"
        data = {"name": nombre}
        
        try:
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            elif result.get("status") == "error":
                if result.get("code", {}).get("name", [""])[0] == "is_used":
                    logger.warning(f"⚠️ Lista ya existe: {nombre}")
                    return {"success": True, "data": {"name": nombre}, "ya_existe": True}
                raise EmailClientError(str(result.get("code", "Error")))
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
            resp = self.session.post(url, data=data, timeout=60)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result}
            raise EmailClientError(result.get("code", "Error"))
        
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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                logger.info(f"✅ Campaña creada: ID {result.get('data', {}).get('id')}")
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(result.get("code", "Error"))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def enviar_campana(self, campana_id: int, send_now: int = 1, send_date: Optional[str] = None) -> Dict:
    
        url = f"{BASE_URL}/campaign/send"
        data = {"id": campana_id, "sendNow": send_now}
        
        if send_now == 0 and send_date:
            data["sendDate"] = send_date
        
        try:
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                logger.info(f"✅ Campaña {campana_id} enviada")
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(str(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(result.get("code", "Error"))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def detalle_campana(self, campana_id: int) -> Dict:
        """
        Obtiene detalle de una campaña.
        GET /v1/campaign/:campaign_id
        """
        url = f"{BASE_URL}/campaign/{campana_id}"
        
        try:
            resp = self.session.get(url, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {})}
            raise EmailClientError(result.get("code", "Error"))
        
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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result}
            raise EmailClientError(result.get("code", "Error"))
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")

    def preview_html(self, campana_id: int, email: Optional[str] = None) -> Dict:
    
        url = f"{BASE_URL}/campaign/preview/html"
        data = {"id": campana_id}
        
        if email:
            data["email"] = email
        
        try:
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "html": result.get("html", "")}
            raise EmailClientError(result.get("code", "Error"))
        
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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                return {"success": True, "data": result.get("data", {}).get("data", [])}
            raise EmailClientError(str(result.get("code", "Error")))
        
        except requests.exceptions.RequestException as e:
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
            resp = self.session.post(url, data=data, timeout=30)
            result = resp.json()
            
            if result.get("status") == "ok":
                logger.info(f"✅ Campo creado: {nombre}")
                return {"success": True, "data": result.get("data", {})}
            elif result.get("status") == "error":
                if "is_used" in str(result.get("code", "")):
                    logger.warning(f"⚠️ El campo '{nombre}' ya existe")
                    return {"success": True, "ya_existe": True}
                raise EmailClientError(str(result.get("code", "Error")))
            return {"success": False, "error": result}
        
        except requests.exceptions.RequestException as e:
            raise EmailClientError(f"Error de red: {e}")
    