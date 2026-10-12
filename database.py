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
    # SMS enviados en la última ejecución (lo muestra Inicio).
    ultimo_enviados = db.Column(db.Integer, nullable=True)
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
    
class MensajeOperacion(db.Model):
    """
    Plantilla SMS del constructor de mensajes, asignada a un momento del
    cliente (ej. "Mora corta"). La tabla vieja también tiene Operador, Tipo,
    Categoria y Operadores: ya no se usan.
    """
    __tablename__ = 'Mensajes_'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Momento = db.Column(db.String(100))
    Mensaje = db.Column(db.Text, nullable=False)
    Estado = db.Column(db.Integer, default=1)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    # Columna vieja: en las bases que ya existían es NOT NULL, por eso va vacía.
    Operador = db.Column(db.String(100), nullable=False, default='')

    def to_dict(self):
        return {
            'id': self.id,
            'momento': self.Momento or '',
            'mensaje': self.Mensaje,
            'estado': self.Estado,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
        }
