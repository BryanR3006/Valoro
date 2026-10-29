from datetime import datetime

import pytz
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

_COLOMBIA_TZ = pytz.timezone("America/Bogota")


def _ahora_colombia():
    """Hora de Colombia sin zona, como el resto de fechas de programaciones."""
    return datetime.now(_COLOMBIA_TZ).replace(tzinfo=None)


def _agregar_columnas(tabla, columnas):
    """
    Agrega a una tabla que ya existía las columnas que le falten
    ({nombre: tipo SQL}). Devuelve los nombres de las que agregó.
    """
    inspector = inspect(db.engine)
    if tabla not in inspector.get_table_names():
        return []
    existentes = {col["name"] for col in inspector.get_columns(tabla)}
    agregadas = []
    for columna, tipo in columnas.items():
        if columna not in existentes:
            db.session.execute(text(f'ALTER TABLE "{tabla}" ADD COLUMN {columna} {tipo}'))
            agregadas.append(columna)
    db.session.commit()
    return agregadas


def _ensure_programacion_sms_columns():
    """Columnas nuevas de programaciones_sms."""
    _agregar_columnas("programaciones_sms", {
        "filas_excel": "TEXT",
        "link_whatsapp": "TEXT",
        "ultimo_error": "TEXT",
        "ultimo_enviados": "INTEGER",
        "id_mensaje": "TEXT",
        "id_campana": "TEXT",
        "intervalo_minutos": "INTEGER",
        "hora_fin": "TEXT",
        "link": "TEXT",
        "ultimo_intento": "DATETIME",
    })


def _ensure_usuarios_columns():
    """Columnas nuevas de usuarios (login, usuarios.py)."""
    _agregar_columnas("usuarios", {
        "actualizado": "DATETIME",
    })


def _ensure_programacion_email_columns():
    """Columnas de horario de programaciones_email (recurrente igual que SMS)."""
    _agregar_columnas("programaciones_email", {
        "hora_inicio": "TEXT",
        "fecha_fin": "TEXT",
        "intervalo_minutos": "INTEGER",
        "hora_fin": "TEXT",
        "ultimo_intento": "DATETIME",
        "id_mensaje": "TEXT",
        "id_campana": "TEXT",
    })


def _ensure_momento_columns():
    """
    Momento del cliente en las plantillas SMS y email (reemplaza operador y
    categoría). Las plantillas SMS viejas quedan con su categoría como momento.
    """
    if "Momento" in _agregar_columnas("Mensajes_", {"Momento": "TEXT"}):
        columnas = {col["name"] for col in inspect(db.engine).get_columns("Mensajes_")}
        if "Categoria" in columnas:
            db.session.execute(text(
                'UPDATE "Mensajes_" SET Momento = Categoria WHERE Categoria IS NOT NULL AND Categoria != \'\''))
            db.session.commit()
    _agregar_columnas("plantillas_email", {"momento": "TEXT"})
    # IDs que asigna el usuario; van a los campos Id Mensaje / Id Campaña de cada contacto.
    _agregar_columnas("plantillas_email", {"id_mensaje": "TEXT", "id_campana": "TEXT"})
    # Etapa (1 a 4) de la plantilla SMS, se elige junto al momento en el constructor.
    _agregar_columnas("Mensajes_", {"Etapa": "INTEGER"})
    # IDs que asigna el usuario y viajan en el payload de Infobip.
    _agregar_columnas("Mensajes_", {"IdMensaje": "TEXT", "IdCampana": "TEXT"})
    # Link del mensaje: reemplaza {{Link}} en todos los SMS (Infobip lo acorta).
    _agregar_columnas("Mensajes_", {"Link": "TEXT"})


def _avisar_columnas_faltantes():
    """
    Compara cada modelo con su tabla y avisa de las columnas que faltan
    (create_all no las agrega a tablas existentes). Solo avisa: la columna
    se agrega en el _ensure_*_columns que corresponda.
    """
    import logging
    logger = logging.getLogger(__name__)
    inspector = inspect(db.engine)
    tablas = set(inspector.get_table_names())
    for tabla in db.metadata.sorted_tables:
        if tabla.name not in tablas:
            continue
        existentes = {col["name"] for col in inspector.get_columns(tabla.name)}
        faltantes = [col.name for col in tabla.columns if col.name not in existentes]
        if faltantes:
            logger.warning(
                f"⚠️ Tabla '{tabla.name}': faltan columnas {faltantes}. "
                f"Agréguelas en el _ensure_*_columns correspondiente."
            )

def init_db(app):
    db.init_app(app)
    with app.app_context():
        db.create_all()
        _ensure_programacion_sms_columns()
        _ensure_programacion_email_columns()
        _ensure_usuarios_columns()
        _ensure_momento_columns()
        _avisar_columnas_faltantes()


class PlantillaEmail(db.Model):
    """
    Plantilla de email guardada (asunto + HTML con {{variables}}). Se elige en
    /config-email. Si ya se usó, editarla crea otra ("<nombre> v2") para que lo
    enviado antes no cambie.
    """
    __tablename__ = 'plantillas_email'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    nombre = db.Column(db.String(120), nullable=False, unique=True)
    # Momento del cliente para el que es la plantilla (ej. "Mora corta").
    momento = db.Column(db.String(100), nullable=True)
    # IDs que asigna el usuario; al elegir la plantilla se copian al envío
    # (campos Id Mensaje / Id Campaña de cada contacto en la plataforma de email).
    id_mensaje = db.Column(db.String(100), nullable=True)
    id_campana = db.Column(db.String(100), nullable=True)
    asunto = db.Column(db.String(255), nullable=False, default='')
    html = db.Column(db.Text, nullable=False)
    activa = db.Column(db.Boolean, default=True, nullable=False)
    usos = db.Column(db.Integer, default=0, nullable=False)
    fecha_ultimo_uso = db.Column(db.DateTime, nullable=True)
    usuario = db.Column(db.String(100), nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=_ahora_colombia)
    fecha_actualizacion = db.Column(db.DateTime, default=_ahora_colombia, onupdate=_ahora_colombia)

    def a_dict(self):
        from services.email_service import variables_de_plantilla
        fecha = lambda d: d.strftime("%Y-%m-%d %H:%M") if d else ""
        return {
            "id": self.id,
            "nombre": self.nombre,
            "momento": self.momento or "",
            "id_mensaje": self.id_mensaje or "",
            "id_campana": self.id_campana or "",
            "asunto": self.asunto or "",
            "html": self.html,
            "activa": bool(self.activa),
            "usos": self.usos or 0,
            "variables": variables_de_plantilla(self.html, self.asunto),
            "fecha_ultimo_uso": fecha(self.fecha_ultimo_uso),
            "usuario": self.usuario or "",
            "fecha_creacion": fecha(self.fecha_creacion),
            "fecha_actualizacion": fecha(self.fecha_actualizacion),
        }


class ProgramacionSms(db.Model):
    __tablename__ = 'programaciones_sms'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tipo_programacion = db.Column(db.String(255), nullable=False)
    consulta_sql = db.Column(db.Text, nullable=True)
    plantilla = db.Column(db.Text, nullable=False)
    campana = db.Column(db.String(100), nullable=False)
    usuario = db.Column(db.String(100), nullable=False)
    estado = db.Column(db.String(20), default='pendiente')
    total_destinatarios = db.Column(db.Integer, default=0)
    confirmar_reenvio = db.Column(db.Boolean, default=False)
    fecha_programada = db.Column(db.DateTime)
    hora_inicio = db.Column(db.String(5), default='08:00')
    fecha_fin = db.Column(db.String(10))
    # Filas del Excel (JSON) cuando la base no sale de una consulta (issue #39).
    filas_excel = db.Column(db.Text, nullable=True)
    # Link de WhatsApp del día (Id Mensaje WS) en programaciones simples con consulta.
    link_whatsapp = db.Column(db.Text, nullable=True)
    # Mensaje del último error de ejecución (estado 'error'), visible en la lista.
    ultimo_error = db.Column(db.Text, nullable=True)
    # Recurrente: último turno que se intentó (hora de Colombia), salga bien o mal.
    # Un turno que falla no se repite (podría duplicar lo que alcanzó a salir) y
    # la recurrente sigue con el siguiente turno. Reintentar lo borra.
    ultimo_intento = db.Column(db.DateTime, nullable=True)
    # SMS enviados en la última ejecución (lo muestra Inicio).
    ultimo_enviados = db.Column(db.Integer, nullable=True)
    # ID del mensaje y de la campaña (del constructor): van en el payload de Infobip.
    id_mensaje = db.Column(db.String(100), nullable=True)
    id_campana = db.Column(db.String(100), nullable=True)
    # Recurrente repetida en el día: cada intervalo_minutos desde hora_inicio
    # hasta hora_fin (HH:MM). Vacíos = una vez al día (services/sms_horarios.py).
    intervalo_minutos = db.Column(db.Integer, nullable=True)
    hora_fin = db.Column(db.String(5), nullable=True)
    # Link del constructor: reemplaza {{Link}} de todas las filas al ejecutar.
    link = db.Column(db.Text, nullable=True)
    fecha_ejecucion = db.Column(
        db.DateTime,
        nullable=True
    )
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    fecha_actualizacion = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow
    )
    
class ProgramacionEmail(db.Model):
    """
    Programación de email (antes en BigQuery, Temporal.ProgramacionEmail_Valoro).
    Estados: pendiente / enviando / enviado / completado / error / cancelado.
    tipo: simple (una vez, en fecha_programada) o recurrente, igual que SMS:
    todos los días a hora_inicio, o cada intervalo_minutos hasta hora_fin,
    hasta fecha_fin (services/sms_horarios.py). En la recurrente,
    fecha_programada es desde cuándo corre (los turnos de antes no salen).
    Fechas en hora de Colombia sin zona.
    """
    __tablename__ = 'programaciones_email'

    id = db.Column(db.String(36), primary_key=True)
    tipo = db.Column(db.String(20), nullable=False, default='simple')
    fecha_programada = db.Column(db.DateTime, nullable=False)
    hora_inicio = db.Column(db.String(5), nullable=True)
    fecha_fin = db.Column(db.String(10), nullable=True)
    intervalo_minutos = db.Column(db.Integer, nullable=True)
    hora_fin = db.Column(db.String(5), nullable=True)
    consulta_sql = db.Column(db.Text, nullable=False)
    plantilla = db.Column(db.Text, nullable=False)
    asunto = db.Column(db.String(255), nullable=False, default='')
    campana = db.Column(db.String(255), nullable=False, default='')
    usuario = db.Column(db.String(100), nullable=True)
    reply_email = db.Column(db.String(255), nullable=True)
    # ID del mensaje y de la campaña (de la plantilla o la pantalla): van a los
    # campos Id Mensaje / Id Campaña de cada contacto y a EmailLog.
    id_mensaje = db.Column(db.String(100), nullable=True)
    id_campana = db.Column(db.String(100), nullable=True)
    estado = db.Column(db.String(20), nullable=False, default='pendiente')
    # Mensaje del último error (estado 'error'), visible en la lista.
    ultimo_error = db.Column(db.Text, nullable=True)
    # Recurrente: último turno que se intentó (hora de Colombia), salga bien o mal.
    # Un turno que falla no se repite (podría duplicar lo que alcanzó a salir) y
    # la recurrente sigue con el siguiente turno. Reintentar lo borra.
    ultimo_intento = db.Column(db.DateTime, nullable=True)
    # Emails enviados en la última ejecución y cuándo (lo muestra Inicio).
    ultimo_enviados = db.Column(db.Integer, nullable=True)
    fecha_ejecucion = db.Column(db.DateTime, nullable=True)
    fecha_creacion = db.Column(db.DateTime, default=_ahora_colombia)
    fecha_actualizacion = db.Column(db.DateTime, default=_ahora_colombia, onupdate=_ahora_colombia)

    def hora_recurrente(self):
        """Hora de envío de la recurrente. Las viejas no la tienen: era la hora de fecha_programada."""
        if self.hora_inicio:
            return self.hora_inicio
        return self.fecha_programada.strftime("%H:%M") if self.fecha_programada else None

    def a_dict(self):
        fecha = lambda d: d.strftime("%Y-%m-%d %H:%M:%S") if d else None
        return {
            "id": self.id,
            "tipo": self.tipo or "simple",
            "fecha_programada": fecha(self.fecha_programada),
            "hora_inicio": self.hora_recurrente(),
            "fecha_fin": self.fecha_fin,
            "intervalo_minutos": self.intervalo_minutos,
            "hora_fin": self.hora_fin,
            "consulta_sql": self.consulta_sql,
            "plantilla": self.plantilla,
            "asunto": self.asunto or "",
            "campana": self.campana or "",
            "usuario": self.usuario or "",
            "reply_email": self.reply_email or "",
            "id_mensaje": self.id_mensaje or "",
            "id_campana": self.id_campana or "",
            "estado": self.estado,
            "ultimo_error": self.ultimo_error,
            "ultimo_enviados": self.ultimo_enviados,
            "fecha_ejecucion": fecha(self.fecha_ejecucion),
            "fecha_creacion": fecha(self.fecha_creacion),
            "fecha_actualizacion": fecha(self.fecha_actualizacion),
        }


class Usuario(db.Model):
    """
    Usuario de la aplicación (login). Perfiles en usuarios.py:
    'admin' usa todo; 'analitica' ve todo sin enviar, programar, editar
    plantillas ni configurar. La clave se guarda con
    generate_password_hash, nunca en texto plano.
    """
    __tablename__ = 'usuarios'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    usuario = db.Column(db.String(80), unique=True, nullable=False)  # en minúsculas (correo o nombre corto)
    nombre = db.Column(db.String(100), nullable=False, default='')
    clave_hash = db.Column(db.String(255), nullable=False)
    perfil = db.Column(db.String(20), nullable=False, default='analitica')
    activo = db.Column(db.Boolean, nullable=False, default=True)
    # True: al ingresar solo puede ir a /cambiar-clave (usuario admin sembrado,
    # o clave puesta por otra persona desde /usuarios).
    debe_cambiar_clave = db.Column(db.Boolean, nullable=False, default=False)
    fecha_creacion = db.Column(db.DateTime, default=_ahora_colombia)
    ultimo_ingreso = db.Column(db.DateTime, nullable=True)
    # Último cambio real (crear, perfil, activo, clave): decide qué copia gana al
    # sincronizar con BigQuery (usuarios_bq.py). None = nunca cambió en este PC.
    actualizado = db.Column(db.DateTime, nullable=True)


class RegistroAccion(db.Model):
    """
    Lo que hace cada usuario (todo lo que no es GET, descargas e ingresos).
    Nunca guarda el cuerpo de la petición, teléfonos, correos ni claves.
    """
    __tablename__ = 'registro_acciones'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    fecha = db.Column(db.DateTime, default=_ahora_colombia, nullable=False, index=True)
    usuario = db.Column(db.String(80), nullable=False, default='', index=True)
    perfil = db.Column(db.String(20), nullable=False, default='')
    metodo = db.Column(db.String(10), nullable=False, default='')
    ruta = db.Column(db.String(255), nullable=False, default='')
    canal = db.Column(db.String(20), nullable=False, default='otro', index=True)
    accion = db.Column(db.String(300), nullable=False, default='')
    campana = db.Column(db.String(200), nullable=True)
    registros = db.Column(db.Integer, nullable=True)
    estado_http = db.Column(db.Integer, nullable=True)
    resultado = db.Column(db.String(320), nullable=True)


class EjecucionProgramacion(db.Model):
    """
    Una ejecución (turno) de una programación SMS o email: si envió, si la
    consulta no trajo datos o si falló. Inicio la usa para mostrar cada turno
    del día (ej. recurrente cada 2 h). Fechas en hora de Colombia sin zona.
    resultado: enviado / sin_datos / error.
    """
    __tablename__ = 'ejecuciones_programacion'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    canal = db.Column(db.String(10), nullable=False)  # sms / email
    programacion_id = db.Column(db.String(36), nullable=False, index=True)
    campana = db.Column(db.String(255), nullable=True)
    # Hora que le tocaba (turno de la recurrente o fecha programada).
    turno = db.Column(db.DateTime, nullable=True)
    fecha = db.Column(db.DateTime, nullable=False, default=_ahora_colombia, index=True)
    resultado = db.Column(db.String(20), nullable=False)
    filas_consulta = db.Column(db.Integer, nullable=True)
    enviados = db.Column(db.Integer, nullable=True)
    excluidos = db.Column(db.Integer, nullable=True)
    mensaje = db.Column(db.Text, nullable=True)

    def a_dict(self):
        hora = lambda d: d.strftime("%H:%M") if d else ""
        return {
            "turno": hora(self.turno), "a_las": hora(self.fecha), "resultado": self.resultado,
            "filas_consulta": self.filas_consulta, "enviados": self.enviados,
            "excluidos": self.excluidos, "mensaje": self.mensaje or "",
        }


class MensajeOperacion(db.Model):
    """
    Plantilla SMS del constructor de mensajes, asignada a un momento del
    cliente (ej. "Mora corta") y a una etapa (1 a 4). La tabla vieja también tiene Operador, Tipo,
    Categoria y Operadores: ya no se usan.
    """
    __tablename__ = 'Mensajes_'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Momento = db.Column(db.String(100))
    Etapa = db.Column(db.Integer)  # 1 a 4; vacía en las plantillas viejas
    # IDs que asigna el usuario; al elegir la plantilla se copian al envío (payload de Infobip).
    IdMensaje = db.Column(db.String(100))
    IdCampana = db.Column(db.String(100))
    # Link que va en {{Link}} (en vez del de la consulta); Infobip lo acorta al enviar.
    Link = db.Column(db.Text)
    Mensaje = db.Column(db.Text, nullable=False)
    Estado = db.Column(db.Integer, default=1)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    # Columna vieja: en las bases que ya existían es NOT NULL, por eso va vacía.
    Operador = db.Column(db.String(100), nullable=False, default='')

    def to_dict(self):
        return {
            'id': self.id,
            'momento': self.Momento or '',
            'etapa': self.Etapa,
            'id_mensaje': self.IdMensaje or '',
            'id_campana': self.IdCampana or '',
            'link': self.Link or '',
            'mensaje': self.Mensaje,
            'estado': self.Estado,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
        }
