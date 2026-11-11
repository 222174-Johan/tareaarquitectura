"""Verificacion HTTP de Semana 9. Ejecutar dentro del contenedor api por stdin.

Windows CMD (en la raiz del proyecto):
    type verificar_semana9.py | docker compose exec -T api python -

Usa la LAB_KEY ya configurada en el contenedor; nunca la imprime.
Crea solo datos sinteticos en un run aislado y los elimina al finalizar.
"""

import json
import os
import sys
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "http://127.0.0.1:8000"
TOKEN = os.environ.get("LAB_KEY", "")
RUN = None
OK = 0


def http(method, path, body=None, run=None, idem=None, auth=True):
    headers = {"Content-Type": "application/json"}
    if auth:
        headers["X-Lab-Key"] = TOKEN
    if run:
        headers["X-Lab-Run"] = run
    if idem:
        headers["Idempotency-Key"] = idem
    raw = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(BASE + path, data=raw, headers=headers, method=method)
    try:
        response = urlopen(request, timeout=20)
    except HTTPError as err:
        response = err
    with response:
        content = response.read(1024 * 1024)
        return response.status, (json.loads(content) if content else {})


def check(label, condition, detail=""):
    global OK
    if not condition:
        raise AssertionError("NO PASA: " + label + (" | " + detail if detail else ""))
    OK += 1
    print("PASA | " + label, flush=True)


def main():
    global RUN
    if not TOKEN:
        raise RuntimeError("LAB_KEY no esta disponible dentro del contenedor api")

    code, _ = http("POST", "/_lab/runs", {"stock": 8}, auth=False)
    check("Ruta privada rechaza solicitudes sin LAB_KEY", code == 401, f"HTTP {code}")

    code, created = http("POST", "/_lab/runs", {"stock": 8, "notification_delay_ms": 2000})
    check("Crea escenario aislado (demora 2000 ms)", code == 201 and bool(created.get("run_id")), f"HTTP {code}: {created}")
    RUN = created["run_id"]

    code, catalog = http("GET", "/api/v1/products", run=RUN)
    check("Obtiene catalogo", code == 200 and len(catalog.get("items", [])) >= 1, f"HTTP {code}")
    product = catalog["items"][0]
    product_id = product["id"]
    product_price = product["price_cents"]
    good_body = {"items": [{"product_id": product_id, "quantity": 2}]}
    key = uuid.uuid4().hex

    for value in (True, "2", 2.5, 0, 6):
        body = {"items": [{"product_id": product_id, "quantity": value}]}
        code, response = http("POST", "/api/v1/orders", body, RUN, uuid.uuid4().hex)
        error_code = (response.get("error") or {}).get("code")
        check(
            f"Rechaza quantity={value!r}",
            code == 422 and error_code == "VALIDATION_ERROR",
            f"HTTP {code}; error={error_code}",
        )

    started = time.monotonic()
    code, order = http("POST", "/api/v1/orders", good_body, RUN, key)
    elapsed = time.monotonic() - started
    check("Crea pedido correcto", code == 201 and order.get("total_cents") == 2 * product_price, f"HTTP {code}: {order}")
    print(f"INFO | Respuesta HTTP de pedido: {elapsed:.3f} s (demora de notificacion configurada: 2.000 s)", flush=True)
    check("La respuesta no espera 2 segundos artificialmente", elapsed < 1.9, f"Tiempo {elapsed:.3f} s")
    order_id = order["id"]

    code, repeated = http("POST", "/api/v1/orders", good_body, RUN, key)
    check("Mismo pedido + misma clave: 201 y mismo ID", code == 201 and repeated.get("id") == order_id, f"HTTP {code}")

    altered = {"items": [{"product_id": product_id, "quantity": 1}]}
    code, _ = http("POST", "/api/v1/orders", altered, RUN, key)
    check("Misma clave + otro pedido: 409", code == 409, f"HTTP {code}")

    code, state = http("GET", "/_lab/runs/" + RUN)
    check("Una sola orden en el escenario", code == 200 and len(state.get("orders", [])) == 1, f"HTTP {code}")
    stock = next((p["stock"] for p in state.get("products", []) if p["id"] == product_id), None)
    check("Inventario se descuenta solo una vez", stock == 6, f"stock={stock}")

    deadline = time.monotonic() + 15
    latest = state
    while time.monotonic() < deadline:
        code, latest = http("GET", "/_lab/runs/" + RUN)
        if code == 200:
            statuses = [p.get("notification_status") for p in latest.get("orders", []) if p.get("id") == order_id]
            processed = [e for e in latest.get("events", []) if e.get("status") == "processed"]
            if statuses == ["sent"] and processed and len(latest.get("notifications", [])) == 1:
                break
        time.sleep(0.35)

    statuses = [p.get("notification_status") for p in latest.get("orders", []) if p.get("id") == order_id]
    check("Notificacion llega al estado sent", statuses == ["sent"], f"estados={statuses}")
    processed = [e for e in latest.get("events", []) if e.get("status") == "processed"]
    check("Evento CloudEvents procesado por worker", bool(processed), f"eventos={latest.get('events', [])}")
    event = next((e.get("payload", {}) for e in processed if e.get("payload", {}).get("data", {}).get("order_id") == order_id), {})
    check(
        "Evento contiene identidad y total_cents",
        event.get("type") == "pe.unamba.flash.order.created.v1"
        and event.get("source") == "/services/orders"
        and event.get("specversion") == "1.0"
        and event.get("data", {}).get("total_cents") == 2 * product_price,
        f"evento={event}",
    )
    check("Una sola notificacion persistida", len(latest.get("notifications", [])) == 1)
    print(f"\nRESULTADO: {OK}/{OK} comprobaciones superadas. Semana 9 verificada localmente en este escenario.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, URLError, ValueError, KeyError) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    finally:
        if RUN:
            try:
                code, _ = http("DELETE", "/_lab/runs/" + RUN)
                print(f"INFO | Limpieza de escenario: HTTP {code}")
            except Exception:
                print("AVISO | No se pudo limpiar el escenario; expirara automaticamente.", file=sys.stderr)
