"""Pruebas independientes de Supabase; no requieren credenciales."""
from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest
from pydantic import SecretStr

from app.billing import BillingFailure, billing_payload, send_invoice
from app.config import Settings


ORDER_ID = "11111111-1111-4111-8111-111111111111"


def snapshot():
    return {
        "id": ORDER_ID,
        "total_cents": 6000,
        "items": [
            {"product_id": "p2", "name": "Cable", "quantity": 1, "unit_price_cents": 1000},
            {"product_id": "p1", "name": "Producto", "quantity": 2, "unit_price_cents": 2500},
        ],
    }


def settings():
    return Settings(
        database_url="postgresql+psycopg://user:password@localhost/testdb",
        redis_url="redis://localhost:6379/0",
        lab_key="x" * 64,
        billing_base_url="https://example.supabase.co/functions/v1/billing",
        billing_token=SecretStr("a" * 64),
    )


def test_exact_snapshot_and_sorted_items():
    p = billing_payload(snapshot())
    assert list(p) == ["order_id", "currency", "items", "total_cents"]
    assert [i["product_id"] for i in p["items"]] == ["p1", "p2"]
    assert p["total_cents"] == 6000


def test_reject_inconsistent_snapshot():
    o = snapshot()
    o["total_cents"] = 99
    with pytest.raises(ValueError):
        billing_payload(o)


def test_send_invoice_success_and_replay():
    p = billing_payload(snapshot())
    seen = []
    invoice_id = str(uuid.uuid4())

    def handler(request):
        seen.append(request)
        assert request.headers["Idempotency-Key"] == ORDER_ID
        assert request.headers["X-Billing-Token"] == "a" * 64
        assert request.url.path.endswith("/billing/invoices")
        return httpx.Response(201 if len(seen) == 1 else 200, json={
            "id": invoice_id, "order_id": ORDER_ID, "currency": "PEN",
            "total_cents": 6000, "status": "issued", "number": "DEMO-" + invoice_id,
        })

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            first = await send_invoice(settings(), p, client)
            second = await send_invoice(settings(), p, client)
        return first, second

    first, second = asyncio.run(run())
    assert first == second == (invoice_id, "DEMO-" + invoice_id)


def test_timeout_returns_recoverable_failure():
    p = billing_payload(snapshot())

    def handler(request):
        raise httpx.ReadTimeout("test timeout", request=request)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await send_invoice(settings(), p, client)

    with pytest.raises(BillingFailure) as exc:
        asyncio.run(run())
    assert exc.value.code == "BILLING_UNAVAILABLE"


def test_conflict_is_not_silently_accepted():
    p = billing_payload(snapshot())

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(409))) as client:
            await send_invoice(settings(), p, client)

    with pytest.raises(BillingFailure) as exc:
        asyncio.run(run())
    assert exc.value.code == "IDEMPOTENCY_CONFLICT"


def test_mismatched_order_is_rejected():
    p = billing_payload(snapshot())
    invoice_id = str(uuid.uuid4())
    payload = {"id": invoice_id, "number": "DEMO-" + invoice_id, "status": "issued",
               "order_id": str(uuid.uuid4()), "total_cents": 6000, "currency": "PEN"}

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(201, json=payload))) as client:
            await send_invoice(settings(), p, client)

    with pytest.raises(BillingFailure) as exc:
        asyncio.run(run())
    assert exc.value.code == "BILLING_INVALID_RESPONSE"
