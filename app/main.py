"""抄本核对 API:对三份独立抄录做三维全局对齐并生成共识文本。"""
from __future__ import annotations

import unicodedata

import grapheme
from fastapi import FastAPI, HTTPException

from .reconcile import AdjudicationConflict, adjudicate, reconcile, reconcile_anchored
from .schemas import (
    AdjudicateRequest,
    AdjudicateResponse,
    AnchoredReconcileRequest,
    AnchoredReconcileResponse,
    ReconcileRequest,
    ReconcileResponse,
)

MAX_GRAPHEMES = 120

app = FastAPI(
    title="抄本核对 API",
    version="1.2.0",
    description=(
        "接收三段抄录文本,NFC 规范化后做三维全局动态规划对齐,输出共识文本、对齐列与总成本;"
        "支持锚点强制对齐与人工裁决 uncertain 列。"
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

    # 替换值与三文本同样做 NFC 规范化,且必须恰好是一个字素簇(422)
    choices: list[tuple[tuple, str, object]] = []
    for idx, decision in enumerate(req.decisions):
        signature = tuple(decision.signature)
        if decision.source is not None:
            choices.append((signature, "source", decision.source))
        else:
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
            choices.append((signature, "replacement", clusters[0]))

    # 锚点为空时也走带锚点路径,使响应列统一携带 anchored 标记
    reconciled = reconcile_anchored(*seqs, anchors)
    try:
        return adjudicate(reconciled, choices)
    except AdjudicationConflict as exc:
        # 任一裁决失效即整体拒绝(409),不产出部分结果
        details = [
            {
                "decision": item["decision"],
                "signature": item["signature"],
                "reasons": [_CONFLICT_REASON[r] for r in item["reasons"]],
            }
            for item in exc.conflicts
        ]
        raise HTTPException(status_code=409, detail=details)
