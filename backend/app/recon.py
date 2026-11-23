"""采集计费月底复核账差流程：迟到补采、对账清单、签收与审计同步。

既有能力（crawl_cost）只按核算时间登记采集用量估算；本模块在其上新增：
- 月底开账：把账期内的用量估算冻结成对账清单（原账），发审核人签收；
- 迟到补采回执：按回执编号幂等入账，已开账账期的补采计入下一未开账账期（账期变动）；
- 复核账差视图：按来源作业列出原账、补采费用、账期变动和签收所用的对账清单；
- 复核清单：运营据视图差额生成，财务据此签收；
- 签收结果同步操作审计台账（audit_log）：登记清单实际序号、金额和原始账期。

不变量：
- 已发出 / 已签收的清单及其金额依旧有效，补采不回溯修改；
- 同一补采回执重复提交只计一次；
- 每笔补差都能查回原采集账期和来源作业。
"""
from __future__ import annotations

from typing import Any

from app.models import ReconciliationList, ReconLine, SupplementReceipt
from app.store import store

PRICE_PER_REQUEST = 0.5  # 每次请求单价（元）

COST_TABLE = "crawl_cost"           # 用量估算台账（既有）
RECON_TABLE = "recon_list"          # 对账清单
RECEIPT_TABLE = "supplement_receipt"  # 补采回执
AUDIT_TABLE = "audit_log"           # 操作审计台账（既有）


# ---------------------------------------------------------------------------
# 账期与序号工具
# ---------------------------------------------------------------------------

def _next_period(period: str) -> str:
    year, month = int(period[:4]), int(period[5:7])
    year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return f"{year:04d}-{month:02d}"


def _next_serial(prefix: str) -> str:
    """按前缀递增生成清单实际序号，如 BILL202603-01 / RVW202604-01。"""
    seq = sum(
        1 for row in store.rows(RECON_TABLE)
        if str(row.get("清单序号", "")).startswith(prefix)
    )
    return f"{prefix}-{seq + 1:02d}"


def _next_audit_id() -> str:
    seq = 0
    for row in store.rows(AUDIT_TABLE):
        suffix = str(row.get("记录编号", "")).replace("AUDI-", "")
        if suffix.isdigit():
            seq = max(seq, int(suffix))
    return f"AUDI-{seq + 1:04d}"


def _bill_list_row(period: str) -> dict[str, Any] | None:
    """账期的原账清单（开账即冻结，一个账期只开一次）。"""
    for row in store.rows(RECON_TABLE):
        if row.get("清单类型") == "原账清单" and row.get("开账账期") == period:
            return row
    return None


def _next_open_period(period: str) -> str:
    """原账期已开账时，补采落到之后第一个尚未开账的账期。"""
    candidate = _next_period(period)
    while _bill_list_row(candidate) is not None:
        candidate = _next_period(candidate)
    return candidate


def _usage_by_job(period: str) -> dict[str, dict[str, Any]]:
    """按来源作业聚合账期内的用量估算（crawl_cost 台账）。"""
    usage: dict[str, dict[str, Any]] = {}
    for row in store.rows(COST_TABLE):
        if str(row.get("计费周期", "")) != period:
            continue
        job_id = str(row.get("来源作业", ""))
        if not job_id:
            continue
        bucket = usage.setdefault(job_id, {"requests": 0, "amount": 0.0})
        bucket["requests"] += int(row.get("请求次数") or 0)
        bucket["amount"] += float(row.get("费用预估") or 0)
    for bucket in usage.values():
        bucket["amount"] = round(bucket["amount"], 2)
    return usage


# ---------------------------------------------------------------------------
# 用量登记与月底开账
# ---------------------------------------------------------------------------

def register_usage_estimate(job_id: str, site: str, period: str,
                            requests: int, registered_at: str = "") -> dict[str, Any]:
    """既有能力：按核算时间（账期）登记采集用量估算，写入 crawl_cost 台账。"""
    amount = round(requests * PRICE_PER_REQUEST, 2)
    rows = store.rows(COST_TABLE)
    row = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "记录编号": f"COST-{job_id}-{period.replace('-', '')}",
        "采集站点": site,
        "计费周期": period,
        "请求次数": str(requests),
        "数据流量": "0",
        "代理用量": "0",
        "费用预估": amount,
        "账单状态": "已核算",
        "来源作业": job_id,
        "核算时间": registered_at,
        "status": "已核算", "pending": True, "abnormal": False,
    }
    rows.append(row)
    return row


def issue_bill_list(period: str, issued_by: str, sent_to: str,
                    issued_at: str = "") -> tuple[dict[str, Any] | None, str]:
    """月底开账：把账期用量冻结成原账对账清单并发签收人；同账期只开一次。"""
    if _bill_list_row(period) is not None:
        return None, f"账期 {period} 已开账，已发出的清单与金额依旧有效"
    usage = _usage_by_job(period)
    if not usage:
        return None, f"账期 {period} 无用量记录，无法开账"
    lines = [
        ReconLine(job_id=job_id, original_period=period,
                  requests=bucket["requests"], amount=bucket["amount"], source="原账")
        for job_id, bucket in sorted(usage.items())
    ]
    bill = ReconciliationList(
        serial_no=_next_serial(f"BILL{period.replace('-', '')}"),
        kind="原账清单", billing_period=period, original_period=period,
        lines=lines, issued_by=issued_by, issued_at=issued_at, sent_to=sent_to,
    )
    return _persist_list(bill), f"对账清单 {bill.serial_no} 已发{sent_to}，待签收"


# ---------------------------------------------------------------------------
# 迟到补采回执（幂等）
# ---------------------------------------------------------------------------

def receive_supplement(receipt_id: str, job_id: str, requests: int,
                       original_period: str, received_at: str = "",
                       ) -> tuple[dict[str, Any], bool, str]:
    """补采回执入账：同一回执编号重复提交只计一次。

    原采集账期已开账 → 补采计入下一未开账账期（账期变动）；
    原采集账期未开账 → 直接并入该账期用量估算。
    返回 (回执行, 是否新入账, 说明)。
    """
    for row in store.rows(RECEIPT_TABLE):
        if row.get("回执编号") == receipt_id:
            return row, False, f"回执 {receipt_id} 已入账，重复提交忽略"

    amount = round(requests * PRICE_PER_REQUEST, 2)
    if _bill_list_row(original_period) is None:
        _merge_into_usage(job_id, original_period, requests, amount)
        target_period = original_period
        note = f"原账期 {original_period} 未开账，补采已并入该账期用量"
    else:
        target_period = _next_open_period(original_period)
        note = f"原账期 {original_period} 已开账，补采计入账期 {target_period}"

    receipt = SupplementReceipt(
        receipt_id=receipt_id, job_id=job_id, requests=requests,
        original_period=original_period, received_at=received_at,
        amount=amount, target_period=target_period,
    )
    return _persist_receipt(receipt), True, note


def _merge_into_usage(job_id: str, period: str, requests: int, amount: float) -> None:
    """原账期未开账时，把补采直接并入该账期的用量估算行。"""
    for row in store.rows(COST_TABLE):
        if (str(row.get("来源作业", "")) == job_id
                and str(row.get("计费周期", "")) == period):
            row["请求次数"] = str(int(row.get("请求次数") or 0) + requests)
            row["费用预估"] = round(float(row.get("费用预估") or 0) + amount, 2)
            return
    register_usage_estimate(job_id, "", period, requests)


# ---------------------------------------------------------------------------
# 复核账差视图（按来源作业）
# ---------------------------------------------------------------------------

def difference_view(period: str) -> list[dict[str, Any]]:
    """按来源作业复核账差：原账、补采费用、账期变动、签收所用的对账清单。

    原账以已发出的对账清单为准（结账依据）；未开账时取用量估算台账。
    """
    bill = _bill_list_row(period)
    original: dict[str, dict[str, Any]] = {}
    if bill is not None:
        for line in bill.get("明细", []):
            original[str(line["来源作业"])] = {
                "requests": int(line["请求次数"]), "amount": float(line["金额"]),
            }
    else:
        original = _usage_by_job(period)

    receipts = [
        row for row in store.rows(RECEIPT_TABLE)
        if row.get("原采集账期") == period and row.get("status") == "已入账"
    ]
    supplements: dict[str, dict[str, Any]] = {}
    for row in receipts:
        job_id = str(row["来源作业"])
        bucket = supplements.setdefault(job_id, {
            "requests": 0, "amount": 0.0, "targets": set(),
            "receipts": [], "review_lists": [],
        })
        bucket["requests"] += int(row["补采请求数"])
        bucket["amount"] = round(bucket["amount"] + float(row["补采金额"]), 2)
        bucket["targets"].add(str(row["入账账期"]))
        bucket["receipts"].append(str(row["回执编号"]))
        if row.get("关联复核清单"):
            bucket["review_lists"].append(str(row["关联复核清单"]))

    view: list[dict[str, Any]] = []
    for job_id in sorted(set(original) | set(supplements)):
        orig = original.get(job_id, {"requests": 0, "amount": 0.0})
        supp = supplements.get(job_id)
        targets = sorted(supp["targets"]) if supp else []
        view.append({
            "来源作业": job_id,
            "原采集账期": period,
            "原账请求数": orig["requests"],
            "原账金额": orig["amount"],
            "补采请求数": supp["requests"] if supp else 0,
            "补采金额": supp["amount"] if supp else 0.0,
            "账期变动": f"{period} → {'、'.join(targets)}" if targets else "—",
            "原账清单": str(bill["清单序号"]) if bill is not None else "",
            "复核清单": "、".join(supp["review_lists"]) if supp else "",
            "补采回执": "、".join(supp["receipts"]) if supp else "",
        })
    return view


# ---------------------------------------------------------------------------
# 复核清单与签收
# ---------------------------------------------------------------------------

def generate_review_list(original_period: str, operator: str,
                         created_at: str = "") -> tuple[dict[str, Any] | None, str]:
    """运营据视图差额生成复核清单；每笔补差只进入一份复核清单。"""
    pending = [
        row for row in store.rows(RECEIPT_TABLE)
        if row.get("原采集账期") == original_period
        and row.get("status") == "已入账"
        and not row.get("关联复核清单")
    ]
    if not pending:
        return None, f"账期 {original_period} 无待复核的补采差额"
    targets = {str(row["入账账期"]) for row in pending}
    if len(targets) > 1:
        return None, "差额入账账期不一致，请分批复核"
    billing_period = targets.pop()

    lines = [
        ReconLine(job_id=str(row["来源作业"]), original_period=original_period,
                  requests=int(row["补采请求数"]), amount=float(row["补采金额"]),
                  source="补采", receipt_id=str(row["回执编号"]))
        for row in sorted(pending, key=lambda r: str(r["回执编号"]))
    ]
    review = ReconciliationList(
        serial_no=_next_serial(f"RVW{billing_period.replace('-', '')}"),
        kind="复核清单", billing_period=billing_period,
        original_period=original_period, lines=lines,
        issued_by=operator, issued_at=created_at, sent_to="财务",
    )
    row = _persist_list(review)
    for receipt_row in pending:
        receipt_row["关联复核清单"] = review.serial_no
    return row, f"复核清单 {review.serial_no} 已生成，待财务签收"


def sign_list(serial_no: str, signer: str, signed_at: str = "",
              ) -> tuple[dict[str, Any] | None, str]:
    """签收对账清单：已签清单和金额依旧有效，签收结果同步操作审计台账。"""
    row = find_list(serial_no)
    if row is None:
        return None, f"对账清单 {serial_no} 不存在"
    if row.get("status") == "已签收":
        return None, f"清单 {serial_no} 已签收，金额 {row['金额合计']} 元保持不变"
    row["status"] = "已签收"
    row["pending"] = False
    row["签收人"] = signer
    row["签收时间"] = signed_at
    _persist_audit(serial_no, float(row["金额合计"]), str(row["原始账期"]),
                   signer, signed_at)
    return row, f"清单 {serial_no} 已签收，结果已同步操作审计台账"


def find_list(serial_no: str) -> dict[str, Any] | None:
    for row in store.rows(RECON_TABLE):
        if row.get("清单序号") == serial_no:
            return row
    return None


def billing_statement(period: str) -> dict[str, Any]:
    """账期开账可见内容：本期原账明细 + 入账到账期的补差明细（均可溯源）。"""
    bill = _bill_list_row(period)
    own_lines = bill.get("明细", []) if bill is not None else [
        {"来源作业": job_id, "原始账期": period, "请求次数": bucket["requests"],
         "金额": bucket["amount"], "费用来源": "原账", "补采回执": ""}
        for job_id, bucket in sorted(_usage_by_job(period).items())
    ]
    difference_lines = [
        {
            "来源作业": row["来源作业"], "补采请求数": row["补采请求数"],
            "补采金额": row["补采金额"], "原采集账期": row["原采集账期"],
            "补采回执": row["回执编号"], "关联复核清单": row.get("关联复核清单", ""),
        }
        for row in store.rows(RECEIPT_TABLE)
        if row.get("入账账期") == period and row.get("status") == "已入账"
    ]
    lists = [
        row["清单序号"] for row in store.rows(RECON_TABLE)
        if row.get("开账账期") == period
    ]
    return {
        "开账账期": period,
        "原账明细": own_lines,
        "差额明细": difference_lines,
        "差额合计": round(sum(float(l["补采金额"]) for l in difference_lines), 2),
        "对账清单": lists,
    }


# ---------------------------------------------------------------------------
# 持久化（内存仓库；真实项目替换为数据库访问层）
# ---------------------------------------------------------------------------

def _persist_list(bill: ReconciliationList) -> dict[str, Any]:
    rows = store.rows(RECON_TABLE)
    row = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "清单序号": bill.serial_no,
        "清单类型": bill.kind,
        "开账账期": bill.billing_period,
        "原始账期": bill.original_period,
        "来源作业": "、".join(line.job_id for line in bill.lines),
        "请求总数": bill.total_requests,
        "金额合计": bill.total_amount,
        "明细": [line.to_row() for line in bill.lines],
        "发送人": bill.issued_by,
        "发送时间": bill.issued_at,
        "签收人": bill.signed_by,
        "签收时间": bill.signed_at,
        "status": bill.status,
        "pending": True,
        "abnormal": False,
    }
    rows.append(row)
    return row


def _persist_receipt(receipt: SupplementReceipt) -> dict[str, Any]:
    rows = store.rows(RECEIPT_TABLE)
    row = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "回执编号": receipt.receipt_id,
        "来源作业": receipt.job_id,
        "补采请求数": receipt.requests,
        "补采金额": receipt.amount,
        "原采集账期": receipt.original_period,
        "入账账期": receipt.target_period,
        "回执时间": receipt.received_at,
        "关联复核清单": receipt.review_list_serial,
        "status": receipt.status,
        "pending": True,
        "abnormal": False,
    }
    rows.append(row)
    return row


def _persist_audit(serial_no: str, amount: float, original_period: str,
                   signer: str, signed_at: str) -> dict[str, Any]:
    """签收结果同步操作审计台账：登记清单实际序号、金额和原始账期。"""
    rows = store.rows(AUDIT_TABLE)
    row = {
        "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
        "记录编号": _next_audit_id(),
        "操作模块": "采集计费",
        "操作人": signer,
        "操作类型": "清单签收",
        "变更摘要": f"对账清单{serial_no}签收：金额{amount}元，原始账期{original_period}",
        "操作时间": signed_at,
        "来源节点": "recon_flow",
        "审计结果": "已同步",
        "清单序号": serial_no,
        "签收金额": amount,
        "原始账期": original_period,
        "status": "已记录", "pending": True, "abnormal": False,
    }
    rows.append(row)
    return row
