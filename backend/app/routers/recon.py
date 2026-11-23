"""采集计费月底复核账差接口：视图查询与对账动作，业务规则都在 app.recon。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app import recon

router = APIRouter(prefix="/api/recon", tags=["采集计费复核账差"])


class UsagePayload(BaseModel):
    job_id: str
    site: str = ""
    period: str
    requests: int = Field(ge=0)
    registered_at: str = ""


class IssuePayload(BaseModel):
    period: str
    issued_by: str = "运营"
    sent_to: str = "审核人"
    issued_at: str = ""


class ReceiptPayload(BaseModel):
    receipt_id: str
    job_id: str
    requests: int = Field(ge=0)
    original_period: str
    received_at: str = ""


class ReviewPayload(BaseModel):
    original_period: str
    operator: str = "运营"
    created_at: str = ""


class SignPayload(BaseModel):
    signer: str
    signed_at: str = ""


@router.post("/usage")
def register_usage(payload: UsagePayload) -> dict[str, Any]:
    """按核算时间登记采集用量估算（既有能力的登记入口）。"""
    row = recon.register_usage_estimate(
        payload.job_id, payload.site, payload.period,
        payload.requests, payload.registered_at)
    return {"ok": True, "message": "用量估算已登记", "entry": row}


@router.post("/issue")
def issue_bill(payload: IssuePayload) -> dict[str, Any]:
    """月底开账：生成原账对账清单并发审核人签收。"""
    row, message = recon.issue_bill_list(
        payload.period, payload.issued_by, payload.sent_to, payload.issued_at)
    return {"ok": row is not None, "message": message, "entry": row}


@router.post("/receipts")
def receive_receipt(payload: ReceiptPayload) -> dict[str, Any]:
    """迟到补采回执入账：同一回执编号重复提交只计一次。"""
    row, created, message = recon.receive_supplement(
        payload.receipt_id, payload.job_id, payload.requests,
        payload.original_period, payload.received_at)
    return {"ok": True, "created": created, "message": message, "entry": row}


@router.get("/view/{period}")
def view_differences(period: str) -> dict[str, Any]:
    """按来源作业复核账差视图：原账、补采费用、账期变动、对账清单。"""
    return {"period": period, "items": recon.difference_view(period)}


@router.post("/review-list")
def create_review_list(payload: ReviewPayload) -> dict[str, Any]:
    """运营据视图差额生成复核清单，供财务签收。"""
    row, message = recon.generate_review_list(
        payload.original_period, payload.operator, payload.created_at)
    return {"ok": row is not None, "message": message, "entry": row}


@router.post("/sign/{serial_no}")
def sign_list(serial_no: str, payload: SignPayload) -> dict[str, Any]:
    """签收对账清单：已签金额依旧有效，结果同步操作审计台账。"""
    row, message = recon.sign_list(serial_no, payload.signer, payload.signed_at)
    return {"ok": row is not None, "message": message, "entry": row}


@router.get("/billing/{period}")
def billing_statement(period: str) -> dict[str, Any]:
    """账期开账可见内容：本期原账明细与入账到账期的补差明细。"""
    return recon.billing_statement(period)
