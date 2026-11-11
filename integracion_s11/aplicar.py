"""Aplica integración S11 preservando cambios existentes de Semana 9.

Ejecutar SOLO desde la raíz de tareaarquitectura:
    python integracion_s11\aplicar.py
"""
from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path.cwd()
SRC = Path(__file__).resolve().parent
BACKUP = ROOT / ".s11-backup"


def edit(path: str, old: str, new: str) -> None:
    file = ROOT / path
    original = planned.get(path, file.read_text(encoding="utf-8"))
    count = original.count(old)
    if count != 1:
        raise RuntimeError(f"No se aplica {path}: se esperaba exactamente 1 anclaje; se encontró {count}.")
    planned[path] = original.replace(old, new, 1)


if not (ROOT / "compose.yaml").is_file() or not (ROOT / "backend/app/orders.py").is_file():
    raise SystemExit("ERROR: Abre CMD en la carpeta raíz tareaarquitectura antes de ejecutar.")
if (ROOT / "backend/app/billing.py").exists():
    raise SystemExit("Integración ya existente; no se sobrescribieron archivos.")

planned: dict[str, str] = {}

edit(
    "backend/app/config.py",
    '    frontend_dist: str = "/app/frontend/dist"\n',
    '    frontend_dist: str = "/app/frontend/dist"\n'
    '    # S11: solo variables en backend / worker.\n'
    '    billing_base_url: str = ""\n'
    '    billing_token: SecretStr = SecretStr("")\n',
)

edit(
    "backend/app/models.py",
    'class Notification(Base):\n',
    'class BillingTask(Base):\n'
    '    """Estado de facturación distinto de notification_status."""\n'
    '    __tablename__ = "billing_tasks"\n\n'
    '    order_id: Mapped[uuid.UUID] = mapped_column(\n'
    '        Uuid(as_uuid=True), ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True\n'
    '    )\n'
    '    dataset_id: Mapped[str] = mapped_column(String(64), nullable=False)\n'
    '    payload: Mapped[dict] = mapped_column(JSON_VALUE, nullable=False)\n'
    '    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")\n'
    '    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)\n'
    '    invoice_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)\n'
    '    invoice_number: Mapped[str | None] = mapped_column(String(100), nullable=True)\n'
    '    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)\n'
    '    updated_at: Mapped[datetime] = mapped_column(\n'
    '        DateTime(timezone=True), nullable=False, default=utcnow\n'
    '    )\n'
    '    __table_args__ = (Index("ix_billing_tasks_status_updated", "status", "updated_at"),)\n\n\n'
    'class Notification(Base):\n',
)

edit(
    "backend/app/orders.py",
    'from .db import CAMPAIGN, utcnow\n',
    'from .billing import billing_payload\n'
    'from .db import CAMPAIGN, PUBLIC_DATASET, utcnow\n',
)
edit(
    "backend/app/orders.py",
    'from .models import Order, OrderItem, Outbox, Product\n',
    'from .models import BillingTask, Order, OrderItem, Outbox, Product\n',
)
edit(
    "backend/app/orders.py",
    '        await session.flush()\n        return snapshot\n',
    '        # S11: tarea y snapshot persistidos atómicamente con el pedido público.\n'
    '        # El POST HTTPS ocurre posteriormente en worker, nunca en esta transacción.\n'
    '        if dataset_id == PUBLIC_DATASET:\n'
    '            session.add(BillingTask(\n'
    '                order_id=order_id, dataset_id=dataset_id,\n'
    '                payload=billing_payload(snapshot), status="pending", created_at=created_at\n'
    '            ))\n\n'
    '        await session.flush()\n        return snapshot\n'.replace('status="pending", created_at=created_at','status="pending", updated_at=created_at'),
)

edit(
    "backend/app/main.py",
    'from .events import create_redis\n',
    'from .billing_routes import router as billing_router\nfrom .events import create_redis\n',
)
edit(
    "backend/app/main.py",
    'app.include_router(orders_router)\n',
    'app.include_router(orders_router)\napp.include_router(billing_router)\n',
)

edit(
    "backend/app/worker.py",
    'from .config import Settings, get_settings\n',
    'from .billing import billing_loop\nfrom .config import Settings, get_settings\n',
)
edit(
    "backend/app/worker.py",
    '            cleanup_loop(database, settings),\n',
    '            cleanup_loop(database, settings),\n'
    '            billing_loop(database, settings),\n',
)

edit(
    "compose.yaml",
    '    LAB_KEY: ${LAB_KEY:-}\n',
    '    LAB_KEY: ${LAB_KEY:-}\n'
    '    BILLING_BASE_URL: ${BILLING_BASE_URL:-}\n'
    '    BILLING_TOKEN: ${BILLING_TOKEN:-}\n',
)

original_ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
planned[".gitignore"] = original_ignore + ("\n.s11-backup/\n" if ".s11-backup/" not in original_ignore else "")

new_files = {
    "backend/app/billing.py": SRC / "billing.py",
    "backend/app/billing_routes.py": SRC / "billing_routes.py",
    "backend/app/billing_retry.py": SRC / "billing_retry.py",
    "backend/tests/test_billing_unit.py": SRC / "test_billing_unit.py",
}
for rel in new_files:
    if (ROOT / rel).exists():
        raise SystemExit(f"ERROR: ya existe {rel}. No se modificó ningún archivo.")

for rel, data in planned.items():
    backup = BACKUP / rel
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / rel, backup)
    (ROOT / rel).write_text(data, encoding="utf-8")

for rel, src in new_files.items():
    dst = ROOT / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)

print("Integración S11 aplicada a S09. Respaldos en .s11-backup (ignorado por Git).")
print("No se modificó .env; ejecuta configurar_env.py para cargar el token local.")
