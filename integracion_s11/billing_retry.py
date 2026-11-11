"""Recuperación manual: python -m app.billing_retry UUID_PEDIDO"""
from __future__ import annotations

import asyncio
import sys
import uuid

from .config import get_settings
from .db import Database, PUBLIC_DATASET, utcnow
from .models import BillingTask


async def retry(order_id: uuid.UUID) -> None:
    database = Database(get_settings())
    try:
        async with database.session_factory() as session:
            async with session.begin():
                task = await session.get(BillingTask, order_id, with_for_update=True)
                if task is None or task.dataset_id != PUBLIC_DATASET:
                    raise SystemExit("No se encontró una facturación pública para ese pedido")
                if task.status == "issued":
                    print("Comprobante ya emitido:", task.invoice_number)
                    return
                if task.status == "processing":
                    print("La tarea está procesándose; consulta de nuevo antes de reintentar")
                    return
                task.status = "pending"
                task.last_error_code = None
                task.updated_at = utcnow()
        print("Facturación en pending. El worker reintentará con el mismo order_id y cuerpo.")
    finally:
        await database.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Uso: python -m app.billing_retry UUID_PEDIDO")
    try:
        identifier = uuid.UUID(sys.argv[1])
    except ValueError:
        raise SystemExit("Se requiere UUID válido") from None
    asyncio.run(retry(identifier))
