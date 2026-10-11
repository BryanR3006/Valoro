"""
infobip_logs.py — Descarga de los logs de SMS de Infobip (/sms/3/logs).

El periodo se parte en ventanas de 30
minutos que se piden en paralelo, y cada ventana se pagina con cursor
(useCursor + nextCursor, 1000 por página). La cuenta envía cientos de miles
de SMS al día, así que cada ventana queda en caché:
  - ventanas de las últimas 2 horas: se vuelven a pedir pasado 1 minuto
    (sus estados todavía cambian)
  - de las últimas 24 horas: cada 15 minutos
  - más viejas: cada 2 horas
Cada SMS se guarda como una tupla corta (ver CAMPOS) para no llenar la memoria.

Sin Flask ni BigQuery.
"""

import logging
import sys
import threading
import time
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import requests

logger = logging.getLogger(__name__)

TAMANO_VENTANA = timedelta(minutes=30)
CONCURRENCIA = 8          # con 20 a la vez Infobip corta conexiones desde la red de la oficina
REINTENTOS = 4
POR_PAGINA = 1000
MAX_PAGINAS_POR_VENTANA = 200
FMT = "%Y-%m-%dT%H:%M:%S.000+0000"

# precio es por parte (pricePerMessage); el costo del SMS es precio × partes
# (messageCount: un SMS de más de 160 caracteres va en 2 o más partes).
Sms = namedtuple("Sms", "message_id bulk_id sender destino sent_at done_at mcc_mnc estado nombre_estado "
                        "error descripcion_error precio texto partes moneda", defaults=(1, ""))

OPERADORES = {
    "732101": "Claro", "732102": "Movistar", "732103": "Tigo/Une",
    "732111": "Tigo", "732123": "Movistar", "732130": "Avantel",
    "732154": "Virgin", "732360": "WOM",
}

ACCIONES_ERROR = {
    "EC_DEST_ADDRESS_BLACKLISTED": "Revisar lista DNC, respetar decisión del usuario",
    "EC_TELESERVICE_NOT_PROVISIONED": "Verificar número, sacarlo de la lista",
    "EC_SM_DELIVERY_FAILURE": "Reintentar (puede ser temporal)",
    "EC_SYSTEM_FAILURE": "Reintentar (sistema)",
    "EC_UNEXPECTED_DATA_VALUE": "Revisar formato del número",
    "EC_ABSENT_SUBSCRIBER": "Número inactivo/apagado, reintentar después",
    "EC_ABSENT_SUBSCRIBER_SM": "Subscriptor no disponible, reintentar después",
    "EC_UNKNOWN_SUBSCRIBER": "Número no existe, eliminar de la lista",
    "EC_QUOTA_EXCEEDED": "Cuota excedida, revisar plan",
    "EC_INVALID_DESTINATION_ADDRESS": "Destino inválido, verificar formato",
}


class ErrorInfobip(RuntimeError):
    """Infobip no respondió: el dashboard avisa y no inventa datos."""


_cache = {}             # inicio de ventana (datetime UTC) -> (momento, [Sms])
_cache_lock = threading.Lock()


def operador(mcc_mnc):
    return OPERADORES.get(mcc_mnc or "", mcc_mnc or "Desconocido")


def _fecha(valor):
    if not valor:
        return None
    try:
        return datetime.strptime(str(valor)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _a_sms(log):
    st, er, pr, ct = (log.get("status") or {}), (log.get("error") or {}), (log.get("price") or {}), (log.get("content") or {})
    i = sys.intern
    return Sms(
        message_id=log.get("messageId") or "",
        bulk_id=i(log.get("bulkId") or ""),
        sender=i(log.get("sender") or ""),
        destino=log.get("destination") or log.get("to") or "",
        sent_at=_fecha(log.get("sentAt")),
        done_at=_fecha(log.get("doneAt")),
        mcc_mnc=i(log.get("mccMnc") or ""),
        estado=i(st.get("groupName") or ""),
        nombre_estado=i(st.get("name") or ""),
        error=i(er.get("name") or ""),
        descripcion_error=i(er.get("description") or ""),
        precio=float(pr.get("pricePerMessage") or 0),
        texto=i((ct.get("text") or "").replace("\n", " ")[:200]),
        partes=int(log.get("messageCount") or 1),
        moneda=i(pr.get("currency") or ""),
    )


def _ttl(fin_ventana, ahora):
    edad = ahora - fin_ventana
    if edad < timedelta(hours=2):
        return 60
    if edad < timedelta(hours=24):
        return 15 * 60
    return 2 * 3600


def _traer_ventana(session, url, desde, hasta):
    """Todos los SMS de una ventana, paginando con cursor."""
    salida, cursor = [], None
    for _ in range(MAX_PAGINAS_POR_VENTANA):
        params = {"sentSince": desde.strftime(FMT), "sentUntil": hasta.strftime(FMT),
                  "limit": POR_PAGINA, "useCursor": "true"}
        if cursor:
            params["cursor"] = cursor
        r = None
        for intento in range(REINTENTOS):
            try:
                r = session.get(url, params=params, timeout=120)
                if r.status_code == 429 or r.status_code >= 500:  # límite de peticiones o error de Infobip
                    raise requests.RequestException(f"HTTP {r.status_code}")
                break
            except requests.RequestException as e:
                if intento == REINTENTOS - 1:
                    raise ErrorInfobip(f"No se pudo conectar con Infobip: {e}") from e
                time.sleep(1.5 * (2 ** intento))
        if r.status_code != 200:
            raise ErrorInfobip(f"Infobip respondió {r.status_code}: {r.text[:200]}")
        data = r.json()
        lote = data.get("results") or []
        salida.extend(_a_sms(log) for log in lote)
        cursor = (data.get("cursor") or {}).get("nextCursor")
        # Infobip a veces manda una página vacía con nextCursor: se sigue hasta que no haya cursor.
        if not cursor:
            break
    return salida


def _filtrar_y_ordenar(mensajes, desde_utc, hasta_utc, ahora):
    vistos, salida = set(), []
    for sms in mensajes:
        if sms.sent_at and not (desde_utc <= sms.sent_at < hasta_utc):
            continue
        if sms.message_id and sms.message_id in vistos:
            continue
        vistos.add(sms.message_id)
        salida.append(sms)
    salida.sort(key=lambda s: s.sent_at or ahora)
    return salida


def descargar(api_key, base_url, desde, hasta, usar_cache=True):
    """
    (sms, ventanas_con_error): SMS enviados entre desde y hasta (datetime con
    zona), sin repetir y en orden de envío, usando el caché por ventana. Una
    ventana que falla después de los reintentos no entra al caché y se
    devuelve en ventanas_con_error (hora Colombia "HH:MM"). Lanza ErrorInfobip
    si fallan todas. usar_cache=False: pide todo a Infobip y no guarda nada
    (la sincronización con BigQuery).
    """
    if not api_key or not base_url:
        raise ErrorInfobip("Falta la configuración de Infobip (api_key / base_url) en config.json.")
    base_url = base_url.rstrip("/")
    if not base_url.startswith("http"):
        base_url = "https://" + base_url
    url = f"{base_url}/sms/3/logs"

    ahora = datetime.now(timezone.utc)
    desde_utc, hasta_utc = desde.astimezone(timezone.utc), min(hasta.astimezone(timezone.utc), ahora)
    # Ventanas alineadas a :00 y :30 para que el caché sirva entre consultas.
    inicio = desde_utc.replace(minute=0 if desde_utc.minute < 30 else 30, second=0, microsecond=0)
    ventanas = []
    while inicio < hasta_utc:
        ventanas.append((inicio, inicio + TAMANO_VENTANA))
        inicio += TAMANO_VENTANA

    pendientes = []
    with _cache_lock:
        for ini, fin in ventanas:
            guardado = _cache.get(ini)
            if not usar_cache or not guardado or time.time() - guardado[0] > _ttl(min(fin, ahora), ahora):
                pendientes.append((ini, fin))

    resultados = {}
    if pendientes:
        session = requests.Session()
        session.headers.update({"Authorization": f"App {api_key}", "Accept": "application/json"})
        adapter = requests.adapters.HTTPAdapter(pool_connections=CONCURRENCIA, pool_maxsize=CONCURRENCIA)
        session.mount("https://", adapter)
        t0 = time.time()
        resultados, fallidas, ultimo_error = {}, [], None
        with ThreadPoolExecutor(max_workers=CONCURRENCIA) as pool:
            futuros = {(ini, fin): pool.submit(_traer_ventana, session, url, ini, min(fin, ahora))
                       for ini, fin in pendientes}
            for k, f in futuros.items():
                try:
                    resultados[k] = f.result()
                except ErrorInfobip as e:
                    fallidas.append(k[0])
                    ultimo_error = e
        # Segunda pasada: las ventanas que fallaron, de una en una (sin concurrencia,
        # que es cuando Infobip corta menos las conexiones).
        for ini in list(fallidas):
            fin = ini + TAMANO_VENTANA
            try:
                time.sleep(2)
                resultados[(ini, fin)] = _traer_ventana(session, url, ini, min(fin, ahora))
                fallidas.remove(ini)
            except ErrorInfobip as e:
                ultimo_error = e
        if fallidas and not resultados and len(fallidas) == len(ventanas):
            raise ultimo_error
        with _cache_lock:
            for (ini, _), sms in resultados.items():
                _cache[ini] = (time.time(), sms)
        logger.info(f"📨 Infobip: {len(pendientes)} ventanas, {sum(len(v) for v in resultados.values()):,} SMS "
                    f"en {time.time() - t0:.1f}s" + (f", {len(fallidas)} con error" if fallidas else ""))
    else:
        fallidas = []

    if not usar_cache:
        return _filtrar_y_ordenar([s for ventana in resultados.values() for s in ventana], desde_utc, hasta_utc, ahora),             sorted(f.astimezone(timezone(timedelta(hours=-5))).strftime("%Y-%m-%d %H:%M") for f in fallidas)

    vistos, salida = set(), []
    with _cache_lock:
        for ini, _ in ventanas:
            for sms in _cache.get(ini, (0, []))[1]:
                if sms.sent_at and not (desde_utc <= sms.sent_at < hasta_utc):
                    continue
                if sms.message_id and sms.message_id in vistos:
                    continue
                vistos.add(sms.message_id)
                salida.append(sms)
        # Sacar del caché ventanas de más de 3 días (no se vuelven a usar).
        for ini in [k for k in _cache if ahora - k > timedelta(days=3)]:
            del _cache[ini]
    salida.sort(key=lambda s: s.sent_at or ahora)
    col = timezone(timedelta(hours=-5))
    return salida, sorted(f.astimezone(col).strftime("%Y-%m-%d %H:%M") for f in fallidas)
