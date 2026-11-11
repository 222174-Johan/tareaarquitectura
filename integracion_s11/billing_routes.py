"""Estado S11 separado del contrato JSON público original de S09."""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from .models import BillingTask, Order
from .security import ApiError, DatasetContext, dataset_context, get_session

router = APIRouter()


@router.get("/api/v1/orders/{order_id}/billing")
async def get_order_billing(
    order_id: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
    context: DatasetContext = Depends(dataset_context),
) -> JSONResponse:
    try:
        identifier = uuid.UUID(order_id)
    except (ValueError, TypeError, AttributeError):
        raise ApiError(404, "NOT_FOUND", "Billing status not found") from None
    order = await session.get(Order, identifier)
    if order is None or order.dataset_id != context.dataset_id:
        raise ApiError(404, "NOT_FOUND", "Billing status not found")
    task = await session.get(BillingTask, identifier)
    if task is None:
        raise ApiError(404, "NOT_FOUND", "Billing status not found")
    return JSONResponse(content={
        "order_id": str(identifier),
        "status": task.status,
        "invoice_id": str(task.invoice_id) if task.invoice_id else None,
        "invoice_number": task.invoice_number,
        "attempts": task.attempts,
        "last_error_code": task.last_error_code,
    })
