from datetime import datetime

import pytz
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

_COLOMBIA_TZ = pytz.timezone("America/Bogota")


def _ahora_colombia():
    """Hora de Colombia sin zona, como el resto de fechas de programaciones."""
    return datetime.now(_COLOMBIA_TZ).replace(tzinfo=None)


def _ensure_programacion_sms_columns():
    """Agrega columnas nuevas a programaciones_sms si la tabla ya existía."""
    inspector = inspect(db.engine)
    if "programaciones_sms" not in inspector.get_table_names():
        return

    existing = {col["name"] for col in inspector.get_columns("programaciones_sms")}
    migrations = {
        "filas_excel": "ALTER TABLE programaciones_sms ADD COLUMN filas_excel TEXT",
        "link_whatsapp": "ALTER TABLE programaciones_sms ADD COLUMN link_whatsapp TEXT",
        "ultimo_error": "ALTER TABLE programaciones_sms ADD COLUMN ultimo_error TEXT",
    }
    for column, ddl in migrations.items():
        if column not in existing:
            db.session.execute(text(ddl))
    db.session.commit()

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
    __tablename__ = 'Mensajes_'
    
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    Operador = db.Column(db.String(100), nullable=False)
    Mensaje = db.Column(db.Text, nullable=False)
    Tipo = db.Column(db.String(50))
    Estado = db.Column(db.Integer, default=1)
    fecha_creacion = db.Column(db.DateTime, default=datetime.utcnow)
    Categoria = db.Column(db.String(100)) 
    Operadores = db.Column(db.Text)
    def to_dict(self):
        return {
            'id': self.id,
            'operador': self.Operador,
            'mensaje': self.Mensaje,
            'tipo': self.Tipo,
            'estado': self.Estado,
            'fecha_creacion': self.fecha_creacion.isoformat() if self.fecha_creacion else None,
            'operadores': self.Operadores,
            'categoria': self.Categoria,

        }

