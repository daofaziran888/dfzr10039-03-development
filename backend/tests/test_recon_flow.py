"""采集计费月底复核账差场景测试：J7 迟到补采与对账签收全链路。

场景数据：
- 2026-03-31 作业 J7 原单计 10 次请求（单价 0.5 元/次，原账 5.00 元）；
- 3 月清单已发审核人，待签收；
- 2026-04-02 迟到补采回执 RCPT-J7-0402 再计 2 次请求（补采 1.00 元）。
"""
from __future__ import annotations

import copy

import pytest

from app import recon
from app.store import store

TABLES = ["crawl_cost", "recon_list", "supplement_receipt", "audit_log"]


@pytest.fixture(autouse=True)
def isolate_tables():
    """每个用例前后快照/还原相关台账，避免污染共享内存仓库。"""
    snapshot = {name: copy.deepcopy(store.rows(name)) for name in TABLES}
    yield
    for name, rows in snapshot.items():
        store._tables[name] = rows


def run_j7_scenario() -> dict:
    """按时间线走完 J7 场景，返回各环节结果供断言。"""
    # 3月31日：J7 原单登记 10 次请求（按核算时间入账期 2026-03）
    recon.register_usage_estimate("J7", "news.example.com", "2026-03", 10,
                                  registered_at="2026-03-31")
    # 3月月底开账：原账清单发审核人，等待签收
    bill, bill_msg = recon.issue_bill_list(
        "2026-03", issued_by="运营-李某", sent_to="审核人-王某",
        issued_at="2026-03-31")
    # 4月2日：迟到补采回执（+2 次，原采集账期 2026-03）
    receipt, created, receipt_msg = recon.receive_supplement(
        "RCPT-J7-0402", "J7", 2, "2026-03", received_at="2026-04-02")
    # 审核人拿着原清单回来签收
    signed_bill, sign_msg = recon.sign_list(
        bill["清单序号"], signer="审核人-王某", signed_at="2026-04-05")
    # 运营从视图查到 J7 差额 → 生成复核清单 → 财务签收
    view = recon.difference_view("2026-03")
    review, review_msg = recon.generate_review_list(
        "2026-03", operator="运营-李某", created_at="2026-04-06")
    signed_review, review_sign_msg = recon.sign_list(
        review["清单序号"], signer="财务-赵某", signed_at="2026-04-07")
    # 复核清单签发后再看视图：差额已关联到复核清单
    view_after = recon.difference_view("2026-03")
    return {
        "bill": bill, "bill_msg": bill_msg,
        "receipt": receipt, "receipt_created": created, "receipt_msg": receipt_msg,
        "signed_bill": signed_bill, "sign_msg": sign_msg,
        "view": view, "view_after": view_after,
        "review": review, "review_msg": review_msg,
        "signed_review": signed_review, "review_sign_msg": review_sign_msg,
    }


# ---------------------------------------------------------------------------
# 问题一：审核人拿着原清单回来签收时，实际收到哪份结账依据
# ---------------------------------------------------------------------------

def test_reviewer_signs_original_list_unchanged() -> None:
    """迟到补采不回溯已发清单：审核人签收的是 3 月原账清单，金额不变。"""
    ctx = run_j7_scenario()

    bill = ctx["bill"]
    assert bill["清单序号"] == "BILL202603-01", "3 月原账清单序号"
    assert bill["status"] == "已签收"

    # 实际结账依据：原账清单 BILL202603-01，10 次请求、5.00 元，不含补采
    assert ctx["signed_bill"]["请求总数"] == 10
    assert ctx["signed_bill"]["金额合计"] == 5.00
    assert len(ctx["signed_bill"]["明细"]) == 1
    line = ctx["signed_bill"]["明细"][0]
    assert line["来源作业"] == "J7" and line["费用来源"] == "原账"
    assert line["请求次数"] == 10 and line["金额"] == 5.00

    # 补采回执没有进入 3 月清单，而是变动到 4 月账期
    assert ctx["receipt_created"] is True
    assert ctx["receipt"]["原采集账期"] == "2026-03"
    assert ctx["receipt"]["入账账期"] == "2026-04"


def test_difference_view_shows_shift_and_trace() -> None:
    """复核账差视图：按来源作业列出原账、补采费用、账期变动和对账清单。"""
    ctx = run_j7_scenario()

    # 运营开复核清单前看到的视图：差额待复核，复核清单列为空
    assert len(ctx["view"]) == 1
    assert ctx["view"][0]["复核清单"] == ""

    row = ctx["view_after"][0]
    assert row["来源作业"] == "J7"
    assert row["原账请求数"] == 10 and row["原账金额"] == 5.00
    assert row["补采请求数"] == 2 and row["补采金额"] == 1.00
    assert row["账期变动"] == "2026-03 → 2026-04"
    assert row["原账清单"] == "BILL202603-01"
    assert row["复核清单"] == "RVW202604-01"
    assert row["补采回执"] == "RCPT-J7-0402"


def test_signoff_syncs_to_audit_ledger() -> None:
    """签收结果同步操作审计台账：登记清单实际序号、金额和原始账期。"""
    ctx = run_j7_scenario()

    audits = [
        row for row in store.rows("audit_log")
        if row.get("来源节点") == "recon_flow" and row.get("操作类型") == "清单签收"
    ]
    assert len(audits) == 2, "原账清单与复核清单各产生一条签收审计"

    bill_audit = next(a for a in audits if a["清单序号"] == "BILL202603-01")
    assert bill_audit["签收金额"] == 5.00
    assert bill_audit["原始账期"] == "2026-03"
    assert bill_audit["操作人"] == "审核人-王某"
    assert bill_audit["审计结果"] == "已同步"

    review_audit = next(a for a in audits if a["清单序号"] == "RVW202604-01")
    assert review_audit["签收金额"] == 1.00
    assert review_audit["原始账期"] == "2026-03", "复核清单登记的是差额的原采集账期"
    assert review_audit["操作人"] == "财务-赵某"


# ---------------------------------------------------------------------------
# 问题二：同一补采回执重发两次，核对已签金额与下一次开账差额明细
# ---------------------------------------------------------------------------

def test_duplicate_receipt_counted_once() -> None:
    """同一回执重发两次：已签金额不变，下一开账账期差额明细仍只有一条。"""
    ctx = run_j7_scenario()

    # 同一个补采回执重发两次
    dup1, created1, msg1 = recon.receive_supplement(
        "RCPT-J7-0402", "J7", 2, "2026-03", received_at="2026-04-02")
    dup2, created2, msg2 = recon.receive_supplement(
        "RCPT-J7-0402", "J7", 2, "2026-03", received_at="2026-04-02")
    assert created1 is False and created2 is False
    assert "重复" in msg1 and "重复" in msg2

    # 回执台账仍只有一条入账记录
    receipts = [r for r in store.rows("supplement_receipt")
                if r.get("回执编号") == "RCPT-J7-0402"]
    assert len(receipts) == 1

    # 已签清单与金额依旧有效
    assert recon.find_list("BILL202603-01")["金额合计"] == 5.00
    assert recon.find_list("BILL202603-01")["status"] == "已签收"
    assert recon.find_list("RVW202604-01")["金额合计"] == 1.00
    assert recon.find_list("RVW202604-01")["status"] == "已签收"

    # 下一次开账（2026-04）可见的差额明细：仅一条 J7 补采 2 次 / 1.00 元
    statement = recon.billing_statement("2026-04")
    assert len(statement["差额明细"]) == 1
    detail = statement["差额明细"][0]
    assert detail["来源作业"] == "J7"
    assert detail["补采请求数"] == 2 and detail["补采金额"] == 1.00
    assert detail["原采集账期"] == "2026-03", "补差可查回原采集账期"
    assert detail["补采回执"] == "RCPT-J7-0402"
    assert detail["关联复核清单"] == "RVW202604-01"
    assert statement["差额合计"] == 1.00

    # 视图差额不因重发而翻倍
    row = recon.difference_view("2026-03")[0]
    assert row["补采请求数"] == 2 and row["补采金额"] == 1.00


# ---------------------------------------------------------------------------
# 规则单测
# ---------------------------------------------------------------------------

def test_signed_list_cannot_be_signed_twice() -> None:
    """已签清单和金额依旧有效：重复签收被拒绝且金额不变。"""
    ctx = run_j7_scenario()
    again, msg = recon.sign_list("BILL202603-01", signer="审核人-王某",
                                 signed_at="2026-04-08")
    assert again is None and "已签收" in msg
    assert recon.find_list("BILL202603-01")["金额合计"] == 5.00
    audits = [r for r in store.rows("audit_log") if r.get("来源节点") == "recon_flow"]
    assert len(audits) == 2, "重复签收不产生新的审计记录"


def test_issue_same_period_twice_rejected() -> None:
    """同一账期只开一次账，保护已发出的清单。"""
    recon.register_usage_estimate("J7", "news.example.com", "2026-03", 10)
    bill, _ = recon.issue_bill_list("2026-03", "运营", "审核人")
    again, msg = recon.issue_bill_list("2026-03", "运营", "审核人")
    assert bill is not None and again is None
    assert "已开账" in msg


def test_review_list_not_duplicated() -> None:
    """同一笔补差只进入一份复核清单。"""
    ctx = run_j7_scenario()
    again, msg = recon.generate_review_list("2026-03", operator="运营-李某")
    assert again is None and "无待复核" in msg
    reviews = [r for r in store.rows("recon_list") if r.get("清单类型") == "复核清单"]
    assert len(reviews) == 1


def test_supplement_before_issue_folds_into_period() -> None:
    """原账期未开账时，补采直接并入该账期用量，不产生账期变动。"""
    recon.register_usage_estimate("J8", "shop.example.com", "2026-03", 5)
    receipt, created, msg = recon.receive_supplement(
        "RCPT-J8-0401", "J8", 1, "2026-03", received_at="2026-03-30")
    assert created is True and receipt["入账账期"] == "2026-03"
    usage = recon._usage_by_job("2026-03")
    assert usage["J8"]["requests"] == 6
    assert usage["J8"]["amount"] == 3.00


def test_empty_period_cannot_issue() -> None:
    """无用量记录的账期不能开账。"""
    bill, msg = recon.issue_bill_list("2026-09", "运营", "审核人")
    assert bill is None and "无用量记录" in msg
