"""
listas_negras.py — Tablas de lista negra (tutelas) que usan SMS y email.

Cambie aquí la tabla y todo el código la toma de este archivo:
  - LISTA_NEGRA_SMS: teléfonos que no reciben SMS (columna Telefono, 10 dígitos).
    La usa services/sms_service.verificar_lista_negra antes de enviar o programar.
  - LISTA_NEGRA_EMAIL: correos que no reciben email (columna EMAIL).
    La usa services/email_service.filtrar_destinatarios antes de enviar.
Si la tabla no se puede leer, el envío se cancela (no se envía sin revisar la lista).
"""

LISTA_NEGRA_SMS = "capable-arbor-209819.Tablas_Reporteria.Telefonos_Tutela"
LISTA_NEGRA_EMAIL = "capable-arbor-209819.Tablas_Reporteria.Email_Tutela"
