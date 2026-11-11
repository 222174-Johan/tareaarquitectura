"""Adaptador S09 → S11. Solo backend/worker; nunca expone X-Billing-Token al navegador."""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import timedelta
from typing import Any

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

from .config import Settings
from .db import utcnow
from .models import BillingTask

logger = logging.getLogger(__name__)


class BillingFailure(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def billing_payload(order_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Snapshot original confirmado; nunca consulta precios actuales del catálogo."""
    order_id = str(uuid.UUID(order_snapshot["id"]))
    items = [
        {
            "product_id": item["product_id"],
            "name": item["name"],
            "quantity": item["quantity"],
            "unit_price_cents": item["unit_price_cents"],
        }
        for item in order_snapshot["items"]
    ]
    items.sort(key=lambda item: item["product_id"])
    total = sum(item["quantity"] * item["unit_price_cents"] for item in items)
    if total != order_snapshot["total_cents"]:
        raise ValueError("Persisted order has an inconsistent total")
    return {
        "order_id": order_id,
        "currency": "PEN",
        "items": items,
        "total_cents": total,
    }


def configured(settings: Settings) -> bool:
    return bool(settings.billing_base_url.strip() and settings.billing_token.get_secret_value())


async def send_invoice(
    settings: Settings,
    payload: dict[str, Any],
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str]:
    """Retorna (invoice_id, number). Acepta 201 nuevo y 200 replay."""
    base = settings.billing_base_url.strip().rstrip("/")
    token = settings.billing_token.get_secret_value()
    if not base or not token:
        raise BillingFailure("BILLING_NOT_CONFIGURED")
    if not base.startswith("https://") or not base.endswith("/functions/v1/billing"):
        raise BillingFailure("BILLING_URL_INVALID")

    headers = {
        "X-Billing-Token": token,
        "Idempotency-Key": payload["order_id"],
        "Content-Type": "application/json",
    }
    try:
        if client is None:
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=False) as owned_client:
                response = await owned_client.post(base + "/invoices", json=payload, headers=headers)
        else:
            response = await client.post(base + "/invoices", json=payload, headers=headers)
    except (httpx.RequestError, asyncio.TimeoutError):
        raise BillingFailure("BILLING_UNAVAILABLE") from None

    if response.status_code not in (200, 201):
        # No incluir texto del proveedor ni tokens en logs o en datos persistidos.
        if response.status_code == 409:
            raise BillingFailure("IDEMPOTENCY_CONFLICT")
        if response.status_code in (401, 403):
            raise BillingFailure("BILLING_UNAUTHORIZED")
        if response.status_code == 422:
            raise BillingFailure("BILLING_VALIDATION_ERROR")
        raise BillingFailure("BILLING_PROVIDER_ERROR")

    try:
        invoice = response.json()
        invoice_id = str(uuid.UUID(invoice["id"]))
        assert invoice["order_id"] == payload["order_id"]
        assert invoice["total_cents"] == payload["total_cents"]
        assert invoice["currency"] == "PEN"
        assert invoice["status"] == "issued"
        assert invoice["number"] == "DEMO-" + invoice_id
    except (ValueError, KeyError, TypeError, AssertionError):
        raise BillingFailure("BILLING_INVALID_RESPONSE") from None
    return invoice_id, invoice["number"]


async def process_one_billing_task(
    session_factory: async_sessionmaker[AsyncSession], settings: Settings,
) -> bool:
    if not configured(settings):
        return False
    # Reclamo breve en transacción exclusiva de facturación. No hay HTTP dentro de
    # una transacción de inventario ni dentro de este reclamo.
    now = utcnow()
    stale = now - timedelta(seconds=90)
    async with session_factory() as session:
        async with session.begin():
            task = (
                await session.execute(
                    select(BillingTask)
                    .where(or_(
                        BillingTask.status == "pending",
                        and_(BillingTask.status == "processing", BillingTask.updated_at < stale),
                    ))
                    .order_by(BillingTask.updated_at, BillingTask.order_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if task is None:
                return False
            order_id = task.order_id
            payload = dict(task.payload)
            task.status = "processing"
            task.updated_at = now
            task.attempts += 1

    try:
        invoice_id, number = await send_invoice(settings, payload)
        error_code = None
    except BillingFailure as error:
        invoice_id, number, error_code = None, None, error.code
    except Exception:
        logger.exception("Unexpected billing adapter failure; no payload or token logged")
        invoice_id, number, error_code = None, None, "BILLING_INTERNAL_ERROR"

    async with session_factory() as session:
        async with session.begin():
            row = await session.get(BillingTask, order_id, with_for_update=True)
            if row is None or row.status != "processing":
                return True
            row.status = "issued" if error_code is None else "failed"
            row.invoice_id = uuid.UUID(invoice_id) if invoice_id else None
            row.invoice_number = number
            row.last_error_code = error_code
            row.updated_at = utcnow()
    return True


async def billing_loop(database: Any, settings: Settings) -> None:
    while True:
        try:
            processed = await process_one_billing_task(database.session_factory, settings)
            await asyncio.sleep(0.15 if processed else 1.0)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.warning("billing worker issue type=%s", type(error).__name__)
            await asyncio.sleep(2.0)
