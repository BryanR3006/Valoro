"""
Login, perfiles y registro de acciones (igual que en Omnic, con las páginas de Valoro).

Dos perfiles por ahora (Omnic tiene también Comercial, con los mismos
permisos de Analítica: para agregarlo basta sumarlo a PERFILES y PERFILES_LECTORES):
  - admin: ve y usa todo (enviar y programar SMS y email, plantillas, editar,
    cancelar, borrar, configuración) y administra los usuarios.
  - analitica: ve todo (inicio, historial de programaciones, envíos, galería
    de plantillas, dashboard SMS) pero no envía, programa, edita plantillas ni
    configura: las páginas de BLOQUEADAS_LECTORES no se abren y de lo que no
    es GET solo pasan ESCRITURAS_LECTORES (refrescar estados de entrega del
    dashboard SMS).

Toda escritura nueva queda solo para Admin sin hacer nada. Una página nueva de
operación (enviar, programar, configurar) hay que agregarla a BLOQUEADAS_LECTORES.
Todo lo que no es GET (y las descargas) se guarda en RegistroAccion y en
logs/acciones y logs/usuarios/<usuario> (registro_logs.py): quién, cuándo,
canal, campaña, registros, resultado. Nunca se guardan cuerpos de peticiones,
teléfonos, correos ni claves.

Arriba: reglas puras (sin Flask) para probarlas con valores simples.
Abajo: la parte Flask, que app.py instala con usuarios.instalar(app).

Los usuarios son los mismos en todos los PC: se sincronizan con la tabla
Temporal.Valoro_Usuarios de BigQuery (usuarios_bq.py) al arrancar, cada minuto,
después de cada cambio y cuando alguien no logra ingresar.

Usuario inicial: al arrancar, si no existe el usuario "admin" se crea
(Admin, clave "usuario_admin_clave" de config.json o CLAVE_ADMIN_DEFECTO)
con debe_cambiar_clave: al ingresar solo puede ir a /cambiar-clave hasta
cambiarla. Si ya existe no se toca. De respaldo, por línea de comandos:
    python usuarios.py crear <usuario> "<Nombre>" <admin|analitica>
    python usuarios.py clave <usuario>
"""
import json
import re

# ══════════════════════════════════════════════════════════════════════
# Reglas puras
# ══════════════════════════════════════════════════════════════════════

PERFILES = {"admin": "Admin", "analitica": "Analítica"}
PERFIL_ADMIN = "admin"
# Perfiles que ven todo pero no envían, programan ni configuran.
PERFILES_LECTORES = ("analitica",)

# Sin sesión: login, salir y archivos estáticos.
RUTAS_LIBRES = {"/login", "/logout", "/favicon.ico"}
# Con debe_cambiar_clave solo se puede ir aquí (y a las rutas libres).
RUTA_CAMBIAR_CLAVE = "/cambiar-clave"

# Usuario que se crea al arrancar si no existe (la clave es pública: se
# obliga a cambiarla en el primer ingreso).
USUARIO_ADMIN = "admin"
NOMBRE_ADMIN = "Admin"
CLAVE_ADMIN_DEFECTO = "Admin123"
PREFIJOS_LIBRES = ("/static/",)

# Analítica: páginas (GET) que NO puede abrir. Son las de enviar,
# programar, editar plantillas, configurar y administrar usuarios (y lo que va
# debajo de cada una). Todo lo demás en GET sí lo ven.
BLOQUEADAS_LECTORES = (
    # SMS y email: enviar, programar y plantillas
    "/config-sms", "/sms/programaciones", "/api/sms/ver_programaciones", "/config-mensajes",
    "/config-email", "/email/programaciones", "/email/plantillas",
    # Configuración y seguridad
    "/config-general", "/usuarios", "/registro",
)

# Analítica: lo único que no es GET que puede usar.
ESCRITURAS_LECTORES = {
    "/api/dashboards/actualizar-estados",  # Dashboard SMS: refrescar estados de entrega
}
PREFIJOS_ESCRITURAS_LECTORES = ()
ESCRITURAS_BLOQUEADAS_LECTORES = set()

# POST que solo leen y que las páginas llaman solas: no se guardan en el registro de acciones.
LECTURAS_POST = set()

# GET que se registran igual (descargas de datos).
DESCARGAS = {"/api/dashboards/exportar", "/api/envios/exportar", "/api/sms/excel/plantilla"}
PREFIJOS_DESCARGAS = ("/downloads/",)

# Canal de cada ruta (el primero que coincide). "/" va aparte.
CANALES = [
    ("usuarios", ("/login", "/logout", "/usuarios", "/registro", RUTA_CAMBIAR_CLAVE)),
    ("dashboards", ("/dashboards", "/api/dashboards", "/api/inicio", "/api/recent_logs",
                    "/envios", "/api/envios", "/programaciones", "/downloads")),
    ("plantillas", ("/plantillas",)),
    ("sms", ("/config-sms", "/config-mensajes", "/api/sms", "/sms")),
    ("email", ("/config-email", "/api/email", "/email")),
    ("config", ("/config-general",)),
]
NOMBRES_CANALES = [c for c, _ in CANALES] + ["otro"]

# Claves de la petición que traen el nombre de la campaña (la primera que tenga valor).
CLAVES_CAMPANA = ("campana", "nombre_campana", "campaign_name", "nombre", "name",
                  "campaign", "campaign_id", "grupo")
# Claves de la respuesta JSON con cuántos registros se movieron.
CLAVES_REGISTROS = ("enviados", "records_sent", "cargados", "registros", "total_enviados",
                    "a_enviar", "total_destinatarios", "total")

# El usuario es el correo completo (bcrodriguez@qnt.com.co) o un nombre corto (admin).
USUARIO_VALIDO = re.compile(r"^[a-z0-9._@+-]{3,80}$")
LARGO_MINIMO_CLAVE = 8


def _ruta(ruta):
    """Ruta sin query string ni '/' final ('/envios/' -> '/envios')."""
    ruta = (ruta or "/").split("?", 1)[0]
    return ruta.rstrip("/") or "/"


def _bajo(ruta, prefijo):
    """True si la ruta es el prefijo o algo debajo de él ('/api/sms' cubre '/api/sms/enviar')."""
    return ruta == prefijo or ruta.startswith(prefijo + "/")


def es_ruta_libre(ruta):
    ruta = (ruta or "/").split("?", 1)[0]
    return _ruta(ruta) in RUTAS_LIBRES or ruta.startswith(PREFIJOS_LIBRES)


def puede(perfil, ruta, metodo="GET"):
    """¿El perfil puede usar esa ruta con ese método? Admin todo; Analítica ve todo
    menos las páginas de operación y solo escribe en ESCRITURAS_LECTORES."""
    if perfil == PERFIL_ADMIN:
        return True
    if perfil not in PERFILES_LECTORES:
        return False
    ruta = _ruta(ruta)
    if ruta == RUTA_CAMBIAR_CLAVE:  # todos pueden cambiar su propia clave
        return True
    metodo = (metodo or "GET").upper()
    if metodo in ("GET", "HEAD"):
        return not any(_bajo(ruta, p) for p in BLOQUEADAS_LECTORES)
    if ruta in ESCRITURAS_BLOQUEADAS_LECTORES:
        return False
    return ruta in ESCRITURAS_LECTORES or any(_bajo(ruta, p) for p in PREFIJOS_ESCRITURAS_LECTORES)


def es_descarga(ruta):
    ruta = _ruta(ruta)
    return ruta in DESCARGAS or ruta.startswith(PREFIJOS_DESCARGAS)


def canal_de_ruta(ruta):
    """sms, email, plantillas, dashboards, usuarios, config u otro."""
    ruta = _ruta(ruta)
    if ruta == "/":
        return "dashboards"
    for canal, prefijos in CANALES:
        if any(_bajo(ruta, p) for p in prefijos):
            return canal
    return "otro"


def destino_seguro(siguiente):
    """Solo rutas internas ('/algo'); cualquier otra cosa (https://..., //sitio) va al inicio."""
    siguiente = (siguiente or "").strip()
    if not siguiente.startswith("/") or siguiente.startswith("//") or "\\" in siguiente:
        return "/"
    if _ruta(siguiente) in ("/login", "/logout"):
        return "/"
    return siguiente


def extraer_campana(*fuentes):
    """Nombre de campaña de los datos de la petición (dicts), o None."""
    for datos in fuentes:
        if not isinstance(datos, dict):
            continue
        for clave in CLAVES_CAMPANA:
            valor = datos.get(clave)
            if isinstance(valor, (str, int)) and not isinstance(valor, bool) and str(valor).strip():
                return " ".join(str(valor).split())[:200]
    return None


def extraer_registros(respuesta):
    """Cuántos registros reporta la respuesta JSON (enviados, total...), o None."""
    if not isinstance(respuesta, dict):
        return None
    for clave in CLAVES_REGISTROS:
        valor = respuesta.get(clave)
        if isinstance(valor, bool):
            continue
        if isinstance(valor, (int, float)) or (isinstance(valor, str) and valor.strip().isdigit()):
            return int(valor)
    return None


def resumir_resultado(estado_http, respuesta=None, archivo=None):
    """'ok' o 'error', más el message de la respuesta (o el archivo descargado), máximo ~300 caracteres."""
    ok = (estado_http or 0) < 400
    mensaje = ""
    if isinstance(respuesta, dict):
        if respuesta.get("success") is False or respuesta.get("error"):
            ok = False
        mensaje = str(respuesta.get("message") or respuesta.get("error") or "")
    if archivo and not mensaje:
        mensaje = archivo
    texto = "ok" if ok else "error"
    mensaje = " ".join(mensaje.split())[:300]
    return f"{texto}: {mensaje}" if mensaje else texto


def motivo_bloqueo_usuario(es_uno_mismo, perfil_actual, activo_actual,
                           perfil_nuevo, activo_nuevo, admins_activos):
    """Por qué no se puede hacer el cambio (texto) o None si se puede."""
    if es_uno_mismo and not activo_nuevo:
        return "No puedes desactivarte a ti mismo."
    if es_uno_mismo and perfil_nuevo != perfil_actual:
        return "No puedes cambiar tu propio perfil."
    deja_de_ser = (perfil_actual == PERFIL_ADMIN and activo_actual
                   and (perfil_nuevo != PERFIL_ADMIN or not activo_nuevo))
    if deja_de_ser and admins_activos <= 1:
        return "Debe quedar al menos un usuario Admin activo."
    return None


def motivo_no_eliminar(es_uno_mismo, perfil, activo, admins_activos):
    """Por qué no se puede eliminar el usuario (texto) o None si se puede."""
    if es_uno_mismo:
        return "No puedes eliminarte a ti mismo."
    if perfil == PERFIL_ADMIN and activo and admins_activos <= 1:
        return "Debe quedar al menos un usuario Admin activo."
    return None


def puede_con_clave_pendiente(ruta):
    """Con debe_cambiar_clave solo se permite la página de cambiar clave."""
    return _ruta(ruta) == RUTA_CAMBIAR_CLAVE


def errores_usuario_nuevo(usuario, nombre, perfil, clave):
    errores = []
    if not USUARIO_VALIDO.match(usuario or ""):
        errores.append("El usuario debe ser el correo o tener de 3 a 80 caracteres: minúsculas, números, punto, guion, @.")
    if not (nombre or "").strip():
        errores.append("Escribe el nombre.")
    if perfil not in PERFILES:
        errores.append("Perfil no válido.")
    errores += errores_clave(clave)
    return errores


def errores_clave(clave):
    if len(clave or "") < LARGO_MINIMO_CLAVE:
        return [f"La clave debe tener al menos {LARGO_MINIMO_CLAVE} caracteres."]
    return []


# ══════════════════════════════════════════════════════════════════════
# Parte Flask (se instala desde app.py con usuarios.instalar(app))
# ══════════════════════════════════════════════════════════════════════

from datetime import datetime, timedelta

from flask import (current_app, flash, g, get_flashed_messages, has_request_context,
                   jsonify, redirect, render_template, request, session)
from werkzeug.security import check_password_hash, generate_password_hash

from sqlalchemy import func

from database import db, Usuario, RegistroAccion, _ahora_colombia
import registro_logs

FILAS_POR_PAGINA = 100


def _quiere_json():
    """APIs y llamadas fetch reciben JSON; las páginas, HTML."""
    if request.path.startswith("/api/") or request.is_json:
        return True
    if request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return True
    return request.method != "GET" and "text/html" not in (request.headers.get("Accept") or "")


def guardia():
    """before_request: exige sesión y revisa el perfil."""
    for clave in ("usuario", "registro_accion", "registro_campana", "registro_resultado"):
        g.pop(clave, None)  # por si el contexto de la app se reutiliza
    if es_ruta_libre(request.path):
        return None
    uid = session.get("usuario_id")
    usuario = db.session.get(Usuario, uid) if uid else None
    if usuario is None or not usuario.activo:
        session.clear()
        if _quiere_json():
            return jsonify({"success": False, "message": "Inicia sesión"}), 401
        siguiente = request.full_path if request.query_string else request.path
        return redirect("/login?" + _query({"next": siguiente}))
    g.usuario = usuario
    if usuario.debe_cambiar_clave and not puede_con_clave_pendiente(request.path):
        if _quiere_json():
            return jsonify({"success": False, "message": "Cambia tu clave antes de seguir."}), 403
        return redirect(RUTA_CAMBIAR_CLAVE)
    if not puede(usuario.perfil, request.path, request.method):
        if _quiere_json():
            return jsonify({"success": False, "message": "No tienes permiso para esta acción."}), 403
        return render_template("sin_permiso.html"), 403
    return None


def _query(datos):
    from urllib.parse import urlencode
    return urlencode(datos)


def _guardar_registro(**campos):
    """Inserta un RegistroAccion en su propia conexión, sin tocar la sesión de la vista."""
    with db.engine.begin() as conexion:
        conexion.execute(RegistroAccion.__table__.insert().values(**campos))


def _archivo_descargado(response):
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', response.headers.get("Content-Disposition") or "")
    return m.group(1) if m else None


def registrar(response):
    """after_request: guarda lo que no es GET y las descargas. Nunca rompe la petición."""
    try:
        usuario = g.get("usuario")
        metodo = request.method
        if usuario is None or metodo in ("HEAD", "OPTIONS") or es_ruta_libre(request.path):
            return response
        if metodo == "GET" and not es_descarga(request.path):
            return response
        if _ruta(request.path) in LECTURAS_POST:  # solo lee (la página lo llama sola)
            return response
        datos_json = request.get_json(silent=True) if request.is_json else None
        respuesta = None
        if response.mimetype == "application/json" and not response.direct_passthrough:
            respuesta = response.get_json(silent=True)
        campana = g.get("registro_campana") or extraer_campana(
            datos_json, request.form.to_dict(), request.args.to_dict(), request.view_args or {})
        if not campana and isinstance(respuesta, dict):
            campana = extraer_campana({"campana": respuesta.get("campana_id") or respuesta.get("campana")})
        if not campana and request.view_args:  # p. ej. /api/sms/programaciones/<prog_id>
            campana = ", ".join(f"{k} {v}" for k, v in request.view_args.items())[:200]
        campos = dict(
            usuario=usuario.usuario,
            perfil=usuario.perfil,
            metodo=metodo,
            ruta=request.path[:255],
            canal=canal_de_ruta(request.path),
            accion=(g.get("registro_accion") or f"{metodo} {request.path}")[:300],
            campana=campana,
            registros=extraer_registros(respuesta),
            estado_http=response.status_code,
            resultado=g.get("registro_resultado") or resumir_resultado(
                response.status_code, respuesta, _archivo_descargado(response)),
        )
        registro_logs.registrar_accion(**campos)  # logs/acciones y logs/usuarios/<usuario>
        _guardar_registro(fecha=_ahora_colombia(), **campos)
    except Exception as e:
        current_app.logger.error(f"❌ Registro de acciones: no se pudo guardar ({request.method} {request.path}): {e}")
    return response


def contexto():
    """Variables para las plantillas: usuario_actual y puede_ver(url)."""
    usuario = g.get("usuario") if has_request_context() else None
    perfil = usuario.perfil if usuario else None
    return {
        "usuario_actual": usuario,
        "perfil_actual": PERFILES.get(perfil, ""),
        "puede_ver": lambda url: puede(perfil, url, "GET"),
    }


# ---------------------------------------------------------------- páginas

def login():
    siguiente = destino_seguro(request.values.get("next"))
    sin_usuarios = Usuario.query.count() == 0
    error = ""
    nombre_usuario = ""
    if request.method == "POST":
        nombre_usuario = (request.form.get("usuario") or "").strip().lower()[:80]
        clave = request.form.get("clave") or ""

        def buscar():
            u = (Usuario.query.filter(func.lower(Usuario.usuario) == nombre_usuario).first()
                 if nombre_usuario else None)
            return u, bool(u and check_password_hash(u.clave_hash, clave))
        usuario, clave_ok = buscar()
        # Puede ser un usuario creado (o una clave cambiada) en otro PC: se traen
        # los usuarios de BigQuery y se intenta otra vez.
        if not clave_ok and nombre_usuario:
            import usuarios_bq
            if usuarios_bq.sincronizar_por_ingreso(current_app._get_current_object()):
                usuario, clave_ok = buscar()
        valido = clave_ok and usuario.activo
        if valido:
            resultado = "ok"
        elif clave_ok:
            resultado = "error: usuario inactivo"
        else:
            resultado = "error: usuario o clave incorrectos"
        try:
            _guardar_registro(
                fecha=_ahora_colombia(), usuario=nombre_usuario, perfil=usuario.perfil if usuario else "",
                metodo="POST", ruta="/login", canal="usuarios", accion="Ingreso",
                estado_http=302 if valido else 401, resultado=resultado,
            )
        except Exception as e:
            current_app.logger.error(f"❌ Registro de acciones: no se pudo guardar el ingreso: {e}")
        if valido:
            session.clear()
            session.permanent = True
            session["usuario_id"] = usuario.id
            usuario.ultimo_ingreso = _ahora_colombia()
            db.session.commit()
            return redirect(RUTA_CAMBIAR_CLAVE if usuario.debe_cambiar_clave else siguiente)
        error = "Usuario o clave incorrectos."
    return render_template("login.html", error=error, siguiente=siguiente,
                           sin_usuarios=sin_usuarios, nombre_usuario=nombre_usuario), (401 if error else 200)


def cambiar_clave():
    """Cambiar la propia clave (obligatorio con debe_cambiar_clave)."""
    usuario = g.usuario
    error = ""
    if request.method == "POST":
        g.registro_accion, g.registro_campana = "Cambiar mi clave", f"usuario {usuario.usuario}"
        clave = request.form.get("clave") or ""
        errores = errores_clave(clave)
        if clave != (request.form.get("clave2") or ""):
            errores.append("Las dos claves no coinciden.")
        if not errores and check_password_hash(usuario.clave_hash, clave):
            errores.append("La clave nueva debe ser distinta de la actual.")
        if not errores:
            usuario.clave_hash = generate_password_hash(clave)
            usuario.debe_cambiar_clave = False
            _cambio(usuario)
            db.session.commit()
            _compartir()
            g.registro_resultado = "ok: clave cambiada"
            return redirect("/")
        error = " ".join(errores)
        g.registro_resultado = f"error: {error}"[:320]
    return render_template("cambiar_clave.html", error=error, obligatoria=usuario.debe_cambiar_clave,
                           largo_minimo=LARGO_MINIMO_CLAVE), (400 if error else 200)


def logout():
    session.clear()
    return redirect("/login")


def _solo_admin():
    usuario = g.get("usuario")
    return usuario is not None and usuario.perfil == PERFIL_ADMIN


def usuarios_pagina():
    if not _solo_admin():  # la guardia ya lo impide; esto es por si acaso
        return render_template("sin_permiso.html"), 403
    if request.method == "POST":
        _accion_usuarios()
        return redirect("/usuarios")
    usuarios = Usuario.query.order_by(Usuario.activo.desc(), Usuario.usuario).all()
    return render_template("usuarios.html", usuarios=usuarios, perfiles=PERFILES,
                           largo_minimo=LARGO_MINIMO_CLAVE,
                           mensajes=get_flashed_messages(with_categories=True))


def _accion_usuarios():
    """Crear, cambiar perfil, cambiar clave, activar/desactivar y eliminar (formularios de /usuarios)."""
    f = request.form
    accion = f.get("accion") or ""
    yo = g.usuario
    if accion == "crear":
        usuario = (f.get("usuario") or "").strip().lower()
        nombre = " ".join((f.get("nombre") or "").split())[:100]
        perfil = f.get("perfil") or ""
        g.registro_accion, g.registro_campana = "Crear usuario", f"usuario {usuario}"[:200]
        errores = errores_usuario_nuevo(usuario, nombre, perfil, f.get("clave") or "")
        if f.get("clave") != f.get("clave2"):
            errores.append("Las dos claves no coinciden.")
        if not errores and Usuario.query.filter_by(usuario=usuario).first():
            errores.append(f"Ya existe el usuario {usuario}.")
        if errores:
            return _aviso("error", " ".join(errores))
        nuevo = Usuario(usuario=usuario, nombre=nombre, perfil=perfil, activo=True,
                        clave_hash=generate_password_hash(f.get("clave")))
        _cambio(nuevo)
        db.session.add(nuevo)
        db.session.commit()
        _compartir()
        return _aviso("ok", f"Usuario {usuario} creado ({PERFILES[perfil]}).")

    objetivo = db.session.get(Usuario, f.get("id", type=int) or 0)
    if objetivo is None:
        return _aviso("error", "El usuario no existe.")
    g.registro_campana = f"usuario {objetivo.usuario}"
    admins = Usuario.query.filter_by(perfil=PERFIL_ADMIN, activo=True).count()

    if accion in ("perfil", "activo"):
        perfil_nuevo = f.get("perfil") if accion == "perfil" else objetivo.perfil
        activo_nuevo = (not objetivo.activo) if accion == "activo" else objetivo.activo
        g.registro_accion = ("Cambiar perfil" if accion == "perfil"
                             else ("Activar usuario" if activo_nuevo else "Desactivar usuario"))
        if perfil_nuevo not in PERFILES:
            return _aviso("error", "Perfil no válido.")
        motivo = motivo_bloqueo_usuario(objetivo.id == yo.id, objetivo.perfil, objetivo.activo,
                                        perfil_nuevo, activo_nuevo, admins)
        if motivo:
            return _aviso("error", motivo)
        objetivo.perfil, objetivo.activo = perfil_nuevo, activo_nuevo
        _cambio(objetivo)
        db.session.commit()
        _compartir()
        return _aviso("ok", f"{objetivo.usuario}: {PERFILES[objetivo.perfil]}, "
                            f"{'activo' if objetivo.activo else 'inactivo'}.")

    if accion == "clave":
        g.registro_accion = "Cambiar clave"
        errores = errores_clave(f.get("clave") or "")
        if f.get("clave") != f.get("clave2"):
            errores.append("Las dos claves no coinciden.")
        if errores:
            return _aviso("error", " ".join(errores))
        objetivo.clave_hash = generate_password_hash(f.get("clave"))
        # Clave puesta por otra persona: el dueño la cambia al ingresar.
        objetivo.debe_cambiar_clave = objetivo.id != yo.id
        _cambio(objetivo)
        db.session.commit()
        _compartir()
        return _aviso("ok", f"Clave de {objetivo.usuario} cambiada"
                            + (" (deberá cambiarla al ingresar)." if objetivo.debe_cambiar_clave else "."))

    if accion == "eliminar":
        g.registro_accion = "Eliminar usuario"
        motivo = motivo_no_eliminar(objetivo.id == yo.id, objetivo.perfil, objetivo.activo, admins)
        if motivo:
            return _aviso("error", motivo)
        # Primero se marca eliminado en BigQuery (si no, volvería en la próxima
        # sincronización); RegistroAccion guarda el usuario como texto: su historial se conserva.
        try:
            import usuarios_bq
            usuarios_bq.marcar_eliminado(objetivo)
        except Exception as e:
            return _aviso("error", f"No se pudo eliminar en BigQuery (los demás PC lo seguirían viendo): {e}")
        db.session.delete(objetivo)
        db.session.commit()
        return _aviso("ok", f"Usuario {objetivo.usuario} eliminado.")
    return _aviso("error", "Acción no válida.")


def _cambio(usuario):
    """Marca un cambio real del usuario para que viaje a los otros PC."""
    import usuarios_bq
    usuarios_bq.marcar_cambio(usuario)


def _compartir():
    """Sube el cambio a BigQuery en segundo plano (no demora la página)."""
    import threading
    import usuarios_bq
    app = current_app._get_current_object()
    threading.Thread(target=usuarios_bq.sincronizar, args=(app, "cambio"), daemon=True).start()


def _aviso(tipo, texto):
    flash(texto, tipo)
    g.registro_resultado = f"{tipo}: {texto}"[:320]


def registro_pagina():
    if not _solo_admin():
        return render_template("sin_permiso.html"), 403
    a = request.args
    filtros = {"usuario": (a.get("usuario") or "").strip().lower(), "canal": a.get("canal") or "",
               "desde": a.get("desde") or "", "hasta": a.get("hasta") or ""}
    pagina = max(a.get("pagina", 1, type=int) or 1, 1)
    q = RegistroAccion.query
    if filtros["usuario"]:
        q = q.filter(RegistroAccion.usuario == filtros["usuario"])
    if filtros["canal"]:
        q = q.filter(RegistroAccion.canal == filtros["canal"])
    try:
        if filtros["desde"]:
            q = q.filter(RegistroAccion.fecha >= datetime.strptime(filtros["desde"], "%Y-%m-%d"))
        if filtros["hasta"]:
            q = q.filter(RegistroAccion.fecha < datetime.strptime(filtros["hasta"], "%Y-%m-%d") + timedelta(days=1))
    except ValueError:
        flash("Fechas con formato inválido (AAAA-MM-DD).", "error")
    total = q.count()
    filas = (q.order_by(RegistroAccion.id.desc())
             .offset((pagina - 1) * FILAS_POR_PAGINA).limit(FILAS_POR_PAGINA).all())
    paginas = max((total + FILAS_POR_PAGINA - 1) // FILAS_POR_PAGINA, 1)
    usuarios = [u.usuario for u in Usuario.query.order_by(Usuario.usuario).all()]
    return render_template("registro.html", filas=filas, filtros=filtros, pagina=pagina,
                           paginas=paginas, total=total, usuarios=usuarios, canales=NOMBRES_CANALES,
                           query_pagina=lambda p: _query({**{k: v for k, v in filtros.items() if v}, "pagina": p}),
                           mensajes=get_flashed_messages(with_categories=True))


def _clave_admin_config():
    """'usuario_admin_clave' de config.json, o la clave por defecto."""
    from pathlib import Path
    try:
        with (Path(__file__).resolve().parent / "config.json").open("r", encoding="utf-8") as f:
            clave = str(json.load(f).get("usuario_admin_clave") or "").strip()
    except Exception:
        clave = ""
    return clave or CLAVE_ADMIN_DEFECTO


def crear_admin_si_falta(clave):
    """Crea el usuario admin (Admin, debe cambiar la clave) solo si no existe. True si lo creó."""
    if Usuario.query.filter(func.lower(Usuario.usuario) == USUARIO_ADMIN).first():
        return False
    db.session.add(Usuario(usuario=USUARIO_ADMIN, nombre=NOMBRE_ADMIN, perfil=PERFIL_ADMIN, activo=True,
                           debe_cambiar_clave=True, clave_hash=generate_password_hash(clave)))
    db.session.commit()
    return True


def instalar(app):
    """Registra la guardia, el registro de acciones, el contexto y las páginas; crea admin si falta."""
    with app.app_context():
        try:
            if crear_admin_si_falta(_clave_admin_config()):
                app.logger.warning(f"⚠️ Usuario '{USUARIO_ADMIN}' creado (Admin). "
                                   f"Debe cambiar la clave en el primer ingreso.")
        except Exception as e:
            db.session.rollback()
            app.logger.error(f"❌ No se pudo crear el usuario '{USUARIO_ADMIN}': {e}")
    app.before_request(guardia)
    app.after_request(registrar)
    app.context_processor(contexto)
    app.add_url_rule("/login", "login", login, methods=["GET", "POST"])
    app.add_url_rule("/logout", "logout", logout, methods=["GET", "POST"])
    app.add_url_rule("/usuarios", "usuarios_pagina", usuarios_pagina, methods=["GET", "POST"])
    app.add_url_rule("/registro", "registro_pagina", registro_pagina, methods=["GET"])
    app.add_url_rule(RUTA_CAMBIAR_CLAVE, "cambiar_clave", cambiar_clave, methods=["GET", "POST"])


# ══════════════════════════════════════════════════════════════════════
# Línea de comandos (sin backend: no arranca el scheduler ni los jobs)
# ══════════════════════════════════════════════════════════════════════

def _app_cli():
    """Flask mínimo apuntando a la misma app.db, solo para las tablas de usuarios."""
    from pathlib import Path
    from flask import Flask
    app = Flask(__name__)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{Path(__file__).resolve().parent / 'app.db'}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(app)
    return app


def _pedir_clave():
    import getpass
    clave = getpass.getpass("Clave: ")
    if getpass.getpass("Repite la clave: ") != clave:
        raise SystemExit("Las dos claves no coinciden.")
    errores = errores_clave(clave)
    if errores:
        raise SystemExit(errores[0])
    return clave


def main(argumentos):
    uso = ('Uso:\n  python usuarios.py crear <usuario> "<Nombre>" <admin|analitica>\n'
           '  python usuarios.py clave <usuario>')
    if len(argumentos) < 2 or argumentos[0] not in ("crear", "clave"):
        raise SystemExit(uso)
    app = _app_cli()
    with app.app_context():
        Usuario.__table__.create(db.engine, checkfirst=True)
        RegistroAccion.__table__.create(db.engine, checkfirst=True)
        nombre_usuario = argumentos[1].strip().lower()
        if argumentos[0] == "crear":
            if len(argumentos) != 4:
                raise SystemExit(uso)
            nombre, perfil = argumentos[2], argumentos[3].strip().lower()
            errores = errores_usuario_nuevo(nombre_usuario, nombre, perfil, "x" * LARGO_MINIMO_CLAVE)
            if errores:
                raise SystemExit(" ".join(errores))
            if Usuario.query.filter_by(usuario=nombre_usuario).first():
                raise SystemExit(f"Ya existe el usuario {nombre_usuario}.")
            clave = _pedir_clave()
            db.session.add(Usuario(usuario=nombre_usuario, nombre=" ".join(nombre.split()), perfil=perfil,
                                   activo=True, clave_hash=generate_password_hash(clave),
                                   actualizado=_ahora_colombia()))
            db.session.commit()
            print(f"Usuario {nombre_usuario} creado ({PERFILES[perfil]}).")
        else:
            usuario = Usuario.query.filter_by(usuario=nombre_usuario).first()
            if not usuario:
                raise SystemExit(f"No existe el usuario {nombre_usuario}.")
            usuario.clave_hash = generate_password_hash(_pedir_clave())
            usuario.debe_cambiar_clave = False
            usuario.actualizado = _ahora_colombia()
            db.session.commit()
            print(f"Clave de {nombre_usuario} cambiada.")


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
