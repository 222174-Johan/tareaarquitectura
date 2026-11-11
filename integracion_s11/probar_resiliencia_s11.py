"""Prueba guiada de resiliencia Flash Sale S09 -> Supabase S11.

Ejecuta desde CMD en tareaarquitectura:
    python integracion_s11/probar_resiliencia_s11.py

Simula una caída del proveedor SOLO en el contenedor worker, conservando
los archivos .env y los servicios API/BD. Restaura el worker siempre.
Crea UN pedido público sintético y descuenta una unidad de stock.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent.parent
BASE = os.getenv("FLASH_BASE_URL", "http://127.0.0.1:8019").rstrip("/")
BROKEN_URL = "https://127.0.0.1/functions/v1/billing"


def run_compose(*args: str, env: dict[str, str]) -> str:
    result = subprocess.run(
        ["docker", "compose", *args], cwd=ROOT, env=env,
        capture_output=True, text=True, check=False,
    )
    if result.returncode:
        # Los comandos usados no contienen tokens ni credenciales.
        raise RuntimeError(
            "Falló docker compose " + " ".join(args)
            + "\n" + (result.stderr[-1500:] or result.stdout[-1500:])
        )
    return result.stdout.strip()


def call(method: str, path: str, body=None, headers=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = Request(
        BASE + path, data=data,
        headers={"Content-Type": "application/json", **(headers or {})},
        method=method,
    )
    try:
        with urlopen(request, timeout=20) as response:
            return response.status, json.load(response)
    except HTTPError as error:
        with error:
            return error.code, json.load(error)


def check(description: str, condition: bool, details=None):
    if not condition:
        raise AssertionError(f"FALLÓ | {description}; detalle={details!r}")
    print("PASA |", description, flush=True)


def wait_status(order_id: str, desired: str, duration=60):
    last = None
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        status, last = call("GET", f"/api/v1/orders/{order_id}/billing")
        if status != 200:
            raise RuntimeError(f"Consulta billing HTTP {status}: {last}")
        if last["status"] == desired:
            return last
        # Una nueva falla tras restaurar debe investigarse, no reintentarse en bucle.
        if desired == "issued" and last["status"] == "failed":
            raise RuntimeError(f"Facturación volvió a fallar: {last['last_error_code']}")
        time.sleep(1)
    raise RuntimeError(f"Tiempo de espera agotado para {desired}: {last}")


def main():
    if not (ROOT / "compose.yaml").exists() or not (ROOT / ".env").exists():
        raise SystemExit("Guarda este script dentro de tareaarquitectura/integracion_s11/")
    # Restauración: la variable ausente hace que Compose la tome del .env local.
    real_env = os.environ.copy()
    real_env.pop("BILLING_BASE_URL", None)
    failure_env = real_env.copy()
    failure_env["BILLING_BASE_URL"] = BROKEN_URL

    order_id = None
    failure_checked = False
    print("PRUEBA: proveedor de facturación inaccesible y recuperación manual")
    print("Aviso: se creará un pedido de DEMO que descuenta 1 unidad de stock local.")
    try:
        print("1) Simulando fallo únicamente en el worker...")
        run_compose("up", "-d", "--no-deps", "--force-recreate", "worker", env=failure_env)
        # Comprobar que el worker realmente está ejecutándose con el destino de prueba.
        configured_url = run_compose(
            "exec", "-T", "worker", "python", "-c",
            "from app.config import get_settings; print(get_settings().billing_base_url)",
            env=failure_env,
        )
        check("Worker usa el destino de fallo simulado", configured_url.strip() == BROKEN_URL)

        status, catalog = call("GET", "/api/v1/products")
        check("Catálogo disponible durante fallo de proveedor", status == 200, status)
        product = next((item for item in catalog["items"] if item["stock"] > 0), None)
        if product is None:
            raise RuntimeError("No hay stock para crear un pedido sintético")
        key = str(uuid.uuid4())
        body = {"items": [{"product_id": product["id"], "quantity": 1}]}
        status, order = call("POST", "/api/v1/orders", body, {"Idempotency-Key": key})
        check("Pedido se confirma aunque facturación esté caída", status == 201 and order.get("status") == "confirmed", (status, order))
        order_id = order["id"]
        print("ID del pedido de prueba:", order_id)

        failed = wait_status(order_id, "failed", duration=60)
        check("Tarea de facturación queda en failed", failed["status"] == "failed", failed)
        check("Fallo queda registrado sin factura inventada", failed["invoice_id"] is None and bool(failed["last_error_code"]), failed)
        status, still_confirmed = call("GET", f"/api/v1/orders/{order_id}")
        check("Pedido permanece confirmed", status == 200 and still_confirmed["status"] == "confirmed", (status, still_confirmed))
        failure_checked = True
    finally:
        print("2) Restaurando SIEMPRE el worker con la configuración original...")
        try:
            run_compose("up", "-d", "--no-deps", "--force-recreate", "worker", env=real_env)
            print("PASA | Worker restaurado (URL cargada desde .env)")
        except Exception as restore_error:
            print("ATENCIÓN: no se pudo restaurar el worker:", restore_error, file=sys.stderr)
            print("Ejecuta: docker compose up -d --no-deps --force-recreate worker", file=sys.stderr)
            raise

    if not failure_checked or not order_id:
        raise RuntimeError("La fase de fallo no se completó; no se ejecuta el reintento")
    print("3) Solicitando un reintento MANUAL del mismo pedido...")
    retry_output = run_compose(
        "exec", "-T", "api", "python", "-m", "app.billing_retry", order_id,
        env=real_env,
    )
    print(retry_output)
    issued = wait_status(order_id, "issued", duration=90)
    check("Factura emitida tras recuperar Supabase", issued["status"] == "issued" and bool(issued["invoice_id"]), issued)
    check("Se reutilizó el mismo pedido original", issued["order_id"] == order_id, issued)
    check("Se registraron al menos dos intentos", issued["attempts"] >= 2, issued)
    status, last_order = call("GET", f"/api/v1/orders/{order_id}")
    check("Pedido original continúa confirmed", status == 200 and last_order["status"] == "confirmed", last_order)
    print("COMPROBANTE:", issued["invoice_number"])
    print("RESULTADO: caída simulada y recuperación manual verificadas. Worker restaurado.")


if __name__ == "__main__":
    try:
        main()
    except (AssertionError, RuntimeError, URLError, OSError, KeyError) as exc:
        print("DIAGNÓSTICO:", exc, file=sys.stderr)
        sys.exit(1)
