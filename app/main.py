"""抄本核对 API:对三份独立抄录做三维全局对齐并生成共识文本。"""
from __future__ import annotations

import unicodedata

import grapheme
from fastapi import FastAPI, HTTPException

from . import cases
from .cases import RevisionConflict
from .reconcile import AdjudicationConflict, adjudicate, reconcile, reconcile_anchored
from .schemas import (
    AdjudicateRequest,
    AdjudicateResponse,
    AnchoredReconcileRequest,
    AnchoredReconcileResponse,
    CaseAdjudicationsRequest,
    CaseResponse,
    Decision,
    ReconcileRequest,
    ReconcileResponse,
)

MAX_GRAPHEMES = 120

app = FastAPI(
    title="抄本核对 API",
    version="1.3.0",
    description=(
        "接收三段抄录文本,NFC 规范化后做三维全局动态规划对齐,输出共识文本、对齐列与总成本;"
        "支持锚点强制对齐与人工裁决 uncertain 列;核对案件可持久化保存并增量追加裁决。"
    ),
)


def _to_clusters(texts: list[str]) -> list[list[str]]:
    """NFC 规范化并切分为字素簇序列;超过上限时抛 422。"""
    seqs: list[list[str]] = []
    for idx, raw in enumerate(texts):
        normalized = unicodedata.normalize("NFC", raw)
        clusters = list(grapheme.graphemes(normalized))
        if len(clusters) > MAX_GRAPHEMES:
            raise HTTPException(
                status_code=422,
                detail=f"texts[{idx}] 超过 {MAX_GRAPHEMES} 个字素簇(当前 {len(clusters)} 个)",
            )
        seqs.append(clusters)
    return seqs


def _validate_anchors(
    anchors: list, seqs: list[list[str]]
) -> list[tuple[int, int, int]]:
    """校验锚点:位置不越界且各来源严格递增;任一不合法抛 422。"""
    result: list[tuple[int, int, int]] = []
    prev = (-1, -1, -1)
    for idx, anchor in enumerate(anchors):
        pos = tuple(anchor.positions)
        for s in range(3):
            if not 0 <= pos[s] < len(seqs[s]):
                raise HTTPException(
                    status_code=422,
                    detail=f"anchors[{idx}].positions[{s}]={pos[s]} 越界:texts[{s}] 共 {len(seqs[s])} 个字素簇",
                )
            if pos[s] <= prev[s]:
                raise HTTPException(
                    status_code=422,
                    detail=f"anchors[{idx}].positions[{s}]={pos[s]} 未严格递增(上一锚点为 {prev[s]}),锚点不得重复或回退",
                )
        result.append(pos)
        prev = pos
    return result


_CONFLICT_REASON = {
    "duplicate": "裁决重复:同一坐标签名被多次裁决",
    "column_not_found": "坐标签名在重算后的对齐中不存在",
    "not_uncertain": "目标列已达成共识,并非 uncertain 列",
    "empty_source": "来源选择指向该列的空位",
}


def _normalize_decisions(decisions: list[Decision]) -> list[dict]:
    """校验并规范化裁决为可存储结构;replacement 做 NFC 且须恰好一个字素簇(422)。"""
    stored: list[dict] = []
    for idx, decision in enumerate(decisions):
        if decision.source is not None:
            stored.append(
                {
                    "signature": list(decision.signature),
                    "source": decision.source,
                    "replacement": None,
                }
            )
            continue
        replacement = unicodedata.normalize("NFC", decision.replacement)
        clusters = list(grapheme.graphemes(replacement))
        if len(clusters) != 1:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"decisions[{idx}].replacement 须恰好包含 1 个 NFC 字素簇"
                    f"(当前 {len(clusters)} 个)"
                ),
            )
        stored.append(
            {
                "signature": list(decision.signature),
                "source": None,
                "replacement": clusters[0],
            }
        )
    return stored


def _to_choices(stored: list[dict]) -> list[tuple[tuple, str, object]]:
    """把规范化后的裁决结构还原为 adjudicate 所需的 (signature, kind, value)。"""
    return [
        (tuple(d["signature"]), "source", d["source"])
        if d["source"] is not None
        else (tuple(d["signature"]), "replacement", d["replacement"])
        for d in stored
    ]


def _conflict_detail(exc: AdjudicationConflict) -> list[dict]:
    """把裁决冲突异常转成 409 响应体:逐项列出全部失效原因。"""
    return [
        {
            "decision": item["decision"],
            "signature": item["signature"],
            "reasons": [_CONFLICT_REASON[r] for r in item["reasons"]],
        }
        for item in exc.conflicts
    ]


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/reconcile", response_model=ReconcileResponse)
def post_reconcile(req: ReconcileRequest) -> dict:
    return reconcile(*_to_clusters(req.texts))


@app.post("/reconcile/anchored", response_model=AnchoredReconcileResponse)
def post_reconcile_anchored(req: AnchoredReconcileRequest) -> dict:
    seqs = _to_clusters(req.texts)
    anchors = _validate_anchors(req.anchors, seqs)
    return reconcile_anchored(*seqs, anchors)


@app.post("/reconcile/adjudicate", response_model=AdjudicateResponse)
def post_reconcile_adjudicate(req: AdjudicateRequest) -> dict:
    seqs = _to_clusters(req.texts)
    anchors = _validate_anchors(req.anchors, seqs)
    choices = _to_choices(_normalize_decisions(req.decisions))

    # 锚点为空时也走带锚点路径,使响应列统一携带 anchored 标记
    reconciled = reconcile_anchored(*seqs, anchors)
    try:
        return adjudicate(reconciled, choices)
    except AdjudicationConflict as exc:
        # 任一裁决失效即整体拒绝(409),不产出部分结果
        raise HTTPException(status_code=409, detail=_conflict_detail(exc))


# ---------------------------------------------------------------- 持久化案件


def _case_response(case: dict) -> dict:
    return {
        "case_id": case["case_id"],
        "revision": case["revision"],
        "texts": case["texts"],
        "anchors": [{"positions": list(a)} for a in case["anchors"]],
        "decisions": case["decisions"],
        "result": case["result"],
    }


@app.post("/cases", response_model=CaseResponse, status_code=201)
def post_case(req: AnchoredReconcileRequest) -> dict:
    """创建核对案件:保存三份文本与锚点,初始结果为无裁决的重算结果,revision 为 1。"""
    seqs = _to_clusters(req.texts)
    anchors = _validate_anchors(req.anchors, seqs)
    result = adjudicate(reconcile_anchored(*seqs, anchors), [])
    case = cases.create_case(
        texts=req.texts,
        anchors=[list(a) for a in anchors],
        decisions=[],
        result=result,
    )
    return _case_response(case)


@app.get("/cases/{case_id}", response_model=CaseResponse)
def get_case(case_id: str) -> dict:
    """查询案件当前状态;重启后仍可查询。"""
    case = cases.get_case(case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"案件不存在:{case_id}")
    return _case_response(case)


@app.post("/cases/{case_id}/adjudications", response_model=CaseResponse)
def post_case_adjudications(case_id: str, req: CaseAdjudicationsRequest) -> dict:
    """追加裁决:校验 expected_revision,合并既有与新增裁决后重算并原子校验。

    版本不符或任一裁决失效均返回 409 且不写入;全部有效则在同一事务中
    落库,revision 加一。
    """
    new_decisions = _normalize_decisions(req.decisions)  # 结构校验(422)在事务外完成

    def apply(case: dict) -> tuple[list, dict]:
        merged = case["decisions"] + new_decisions
        seqs = _to_clusters(case["texts"])
        anchors = [tuple(a) for a in case["anchors"]]
        # 复用现有重算/原子校验逻辑:对合并后的全部裁决一次性校验
        result = adjudicate(reconcile_anchored(*seqs, anchors), _to_choices(merged))
        return merged, result

    try:
        case = cases.append_decisions(case_id, req.expected_revision, apply)
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "expected_revision 与当前 revision 不符,未写入任何裁决",
                "expected_revision": req.expected_revision,
                "current_revision": exc.current_revision,
            },
        )
    except AdjudicationConflict as exc:
        raise HTTPException(status_code=409, detail=_conflict_detail(exc))
    if case is None:
        raise HTTPException(status_code=404, detail=f"案件不存在:{case_id}")
    return _case_response(case)
