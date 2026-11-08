import base64
import binascii
import logging
import os
import random
import time
from collections import Counter
from datetime import datetime, timezone
from functools import cache

from flask import Flask, request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("cocina")

BASE_DATOS = os.environ.get("BASE_DATOS", "(default)")
TIEMPO_PREPARACION = float(os.environ.get("TIEMPO_PREPARACION", "6"))
PROBABILIDAD_FALLO = float(os.environ.get("PROBABILIDAD_FALLO", "0"))
PLATO_MISTERIOSO = "plato-misterioso"
ESTADOS_FINALES = ("listo", "rechazado")

app = Flask(__name__)


@cache
def db():
    return firestore.Client(database=BASE_DATOS)


def ahora():
    return datetime.now(timezone.utc)


@firestore.transactional
def terminar_pedido(transaction, pedido_ref, cantidades):
    """Revisa el inventario, lo descuenta y cierra el pedido en un solo paso.

    Todo ocurre en una transacción: si dos entregas del mismo mensaje llegan a
    la vez, Firestore reintenta a la que perdió y esta vez ya ve el pedido
    terminado, así que el descuento se hace una sola vez.

    Devuelve ("listo" | "rechazado" | "repetido", productos que faltan).
    """
    # Firestore exige hacer todas las lecturas antes de la primera escritura.
    estado = pedido_ref.get(transaction=transaction).get("estado")
    if estado in ESTADOS_FINALES:
        return "repetido", []

    inventario = db().collection("inventario")
    existencias = {
        producto: inventario.document(producto).get(transaction=transaction).get("existencias") or 0
        for producto in cantidades
    }
    faltan = [producto for producto, cantidad in cantidades.items() if existencias[producto] < cantidad]

    if faltan:
        transaction.update(pedido_ref, {
            "estado": "rechazado",
            "motivo": "Se acabó: " + ", ".join(faltan),
            "actualizado": ahora(),
        })
        return "rechazado", faltan

    for producto, cantidad in cantidades.items():
        transaction.update(inventario.document(producto), {"existencias": firestore.Increment(-cantidad)})
    transaction.update(pedido_ref, {"estado": "listo", "actualizado": ahora()})
    return "listo", []


@app.post("/")
def recibir():
    # Pub/Sub entrega un sobre JSON: {"message": {"data": <base64>, ...},
    # "subscription": ..., "deliveryAttempt": n}. El último campo solo viene
    # cuando la suscripción tiene política de mensajes muertos.
    sobre = request.get_json(silent=True)
    try:
        pedido_id = base64.b64decode(sobre["message"]["data"], validate=True).decode()
    except (TypeError, KeyError, binascii.Error, UnicodeDecodeError):
        log.error("Llegó un sobre que no es de Pub/Sub o no trae data: %r", sobre)
        return "sobre inválido", 400

    intento = sobre.get("deliveryAttempt")
    log.info("Pedido %s, intento de entrega %s", pedido_id, intento or "desconocido")

    pedido_ref = db().collection("pedidos").document(pedido_id)
    pedido = pedido_ref.get()
    if not pedido.exists:
        # Se confirma igual: reintentar un pedido que no existe no lo va a crear.
        log.warning("El pedido %s no existe; se descarta el mensaje", pedido_id)
        return "", 204

    # Pub/Sub entrega cada mensaje al menos una vez, así que este pedido pudo
    # terminar en una entrega anterior. Se confirma sin tocar nada.
    if pedido.get("estado") in ESTADOS_FINALES:
        log.info("El pedido %s ya estaba %s; entrega repetida, se confirma sin hacer nada",
                 pedido_id, pedido.get("estado"))
        return "", 204

    pedido_ref.update({"estado": "preparando", "actualizado": ahora()})

    cantidades = Counter()
    for item in pedido.get("items"):
        cantidades[item["producto"]] += item["cantidad"]

    if PLATO_MISTERIOSO in cantidades:
        raise RuntimeError(f"La cocina no tiene receta para el plato misterioso (pedido {pedido_id})")

    time.sleep(TIEMPO_PREPARACION)

    resultado, faltan = terminar_pedido(db().transaction(), pedido_ref, cantidades)
    if resultado == "repetido":
        log.info("El pedido %s lo terminó otra entrega mientras se preparaba; no se descuenta de nuevo", pedido_id)
    elif resultado == "rechazado":
        log.info("Pedido %s rechazado, no alcanza: %s", pedido_id, faltan)
    else:
        log.info("Pedido %s listo", pedido_id)

    # Simula que la cocina se cae justo después de terminar y antes de confirmar.
    # Pub/Sub no recibe el 2xx y vuelve a entregar el mismo pedido.
    if random.random() < PROBABILIDAD_FALLO:
        log.warning("Falla simulada en el pedido %s: el trabajo quedó hecho pero no se confirma", pedido_id)
        return "falla simulada", 500

    return "", 204
