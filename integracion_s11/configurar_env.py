"""Carga el token local de tienda-a de S11 hacia .env de S09, sin imprimirlo."""
from __future__ import annotations

import re
from pathlib import Path

project = Path.cwd()
if not (project / "compose.yaml").is_file():
    raise SystemExit("Ejecuta desde la carpeta raíz tareaarquitectura")
source = project.parent / "billing-service-s11" / "cliente-propio.env"
if not source.is_file():
    raise SystemExit("No existe ../billing-service-s11/cliente-propio.env. Configura .env manualmente.")
values = dict(
    line.strip().split("=", 1)
    for line in source.read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.startswith("#") and "=" in line
)
token = values.get("BILLING_TOKEN", "").strip()
if not re.fullmatch(r"[0-9a-f]{64}", token):
    raise SystemExit("El token del archivo cliente-propio.env no tiene el formato correcto")
url = "https://pilblpxqueksfjdqpkqf.supabase.co/functions/v1/billing"
env = project / ".env"
if not env.is_file():
    raise SystemExit("No existe el .env de S09; no se sobrescribirá su configuración")
lines = [
    line for line in env.read_text(encoding="utf-8").splitlines()
    if not line.startswith("BILLING_BASE_URL=") and not line.startswith("BILLING_TOKEN=")
]
lines.extend(["BILLING_BASE_URL=" + url, "BILLING_TOKEN=" + token])
env.write_text("\n".join(lines) + "\n", encoding="utf-8")
print("Configuración S11 guardada en el .env privado de Semana 9, sin mostrar token.")
