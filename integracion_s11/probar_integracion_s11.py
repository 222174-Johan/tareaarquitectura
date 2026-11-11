"""Prueba E2E local S09→S11: crea un pedido público sintético y una factura DEMO.

Ejecutar en Windows desde tareaarquitectura: python integracion_s11\probar_integracion_s11.py
"""
from __future__ import annotations

import json
import os
import time
import uuid
from urllib.request import Request, urlopen
from urllib.error import HTTPError

BASE = os.getenv("FLASH_BASE_URL", "http://127.0.0.1:8019").rstrip("/")


def call(method, path, body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    request = Request(BASE + path, data=data, headers={"Content-Type": "application/json", **(headers or {})}, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as exc:
        with exc:
            return exc.code, json.load(exc)


status, catalog = call("GET", "/api/v1/products")
if status != 200:
    raise SystemExit(f"Falla catálogo: HTTP {status} {catalog}")
product = next((p for p in catalog["items"] if p["stock"] > 0), None)
if product is None:
    raise SystemExit("No hay stock disponible para la prueba de integración")

key = str(uuid.uuid4())
request_body = {"items": [{"product_id": product["id"], "quantity": 1}]}
status, order = call("POST", "/api/v1/orders", request_body, {"Idempotency-Key": key})
assert status == 201 and order["status"] == "confirmed", (status, order)
print("PASA | Pedido confirmado:", order["id"])
status, repeated = call("POST", "/api/v1/orders", request_body, {"Idempotency-Key": key})
assert status == 201 and repeated == order, (status, repeated)
print("PASA | Repetir pedido no crea otra orden")

order_path = f"/api/v1/orders/{order['id']}"
last = None
for _ in range(30):
    status, last = call("GET", order_path + "/billing")
    if status != 200:
        raise SystemExit(f"No se pudo consultar facturación: HTTP {status} {last}")
    if last["status"] in ("issued", "failed"):
        break
    time.sleep(1)
if last["status"] != "issued":
    raise SystemExit(f"Facturación no emitida: {last}. Si está failed, reintenta manualmente.")
assert last["invoice_id"] and last["invoice_number"].startswith("DEMO-")
print("PASA | Factura emitida:", last["invoice_number"])
status, confirmed = call("GET", order_path)
assert status == 200 and confirmed["status"] == "confirmed"
print("PASA | Pedido sigue confirmed y su contrato no cambió")
print("RESULTADO: Integración S09 → S11 funcionando en escenario público de prueba")
