"""抄本核对 API:对三份独立抄录做三维全局对齐并生成共识文本。"""
from __future__ import annotations

import os
import unicodedata

import grapheme
from fastapi import FastAPI, HTTPException

from .reconcile import AdjudicationConflict, adjudicate, reconcile, reconcile_anchored
from .schemas import (
    AdjudicateRequest,
    AdjudicateResponse,
    AnchoredReconcileRequest,
    AnchoredReconcileResponse,
    AppendAdjudicationsRequest,
    CaseRecord,
    Decision,
    ReconcileRequest,
    ReconcileResponse,
)
from .store import CaseNotFound, CaseStore, RevisionConflict

MAX_GRAPHEMES = 120

app = FastAPI(
    title="抄本核对 API",
    version="1.3.0",
    description=(
        "接收三段抄录文本,NFC 规范化后做三维全局动态规划对齐,输出共识文本、对齐列与总成本;"
        "支持锚点强制对齐、人工裁决 uncertain 列,并可将案件持久化到 SQLite 后增量追加裁决。"
    ),
)

#: 持久化案件存储;路径可用环境变量 CASE_DB_PATH 覆盖,默认 ./data/cases.db
store = CaseStore(os.environ.get("CASE_DB_PATH", "data/cases.db"))


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


def _to_choices(decisions: list[Decision]) -> list[tuple[tuple, str, object]]:
    """把裁决模型转换为 adjudicate 的 (signature, kind, value) 序列。

    替换值与三文本同样做 NFC 规范化,且必须恰好是一个字素簇(422)。
    """
    choices: list[tuple[tuple, str, object]] = []
    for idx, decision in enumerate(decisions):
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
    return choices


_CONFLICT_REASON = {
    "duplicate": "裁决重复:同一坐标签名被多次裁决",
    "column_not_found": "坐标签名在重算后的对齐中不存在",
    "not_uncertain": "目标列已达成共识,并非 uncertain 列",
    "empty_source": "来源选择指向该列的空位",
}


def _conflict_detail(exc: AdjudicationConflict) -> list[dict]:
    """把裁决冲突异常格式化为 409 响应 detail(列出每项失效裁决的全部原因)。"""
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
    choices = _to_choices(req.decisions)

    # 锚点为空时也走带锚点路径,使响应列统一携带 anchored 标记
    reconciled = reconcile_anchored(*seqs, anchors)
    try:
        return adjudicate(reconciled, choices)
    except AdjudicationConflict as exc:
        # 任一裁决失效即整体拒绝(409),不产出部分结果
        raise HTTPException(status_code=409, detail=_conflict_detail(exc))


# ---------------------------------------------------------------- 持久化案件


def _recompute_case(texts: list[str], anchors: list, decision_dicts: list[dict]) -> dict:
    """案件重算:由库存文本/锚点重建对齐,再对合并后的裁决列表做原子校验。

    文本与锚点在创建时已通过校验,此处直接复用;裁决经 Decision 模型还原后
    走与无状态接口相同的转换与 adjudicate 原子校验。
    """
    seqs = _to_clusters(texts)
    anchor_tuples = [tuple(a) for a in anchors]
    decisions = [Decision(**d) for d in decision_dicts]
    reconciled = reconcile_anchored(*seqs, anchor_tuples)
    return adjudicate(reconciled, _to_choices(decisions))


def _case_response(record: dict) -> dict:
    """把存储记录整理为 CaseRecord 响应形状。"""
    return {
        "case_id": record["case_id"],
        "revision": record["revision"],
        "texts": record["texts"],
        "anchors": [{"positions": p} for p in record["anchors"]],
        "decisions": record["decisions"],
        "result": record["result"],
    }


@app.post("/cases", status_code=201, response_model=CaseRecord)
def post_case(req: AnchoredReconcileRequest) -> dict:
    """创建案件:保存三份文本与锚点,初始裁决列表为空、revision 为 0。"""
    seqs = _to_clusters(req.texts)
    anchors = _validate_anchors(req.anchors, seqs)
    # 空裁决重算一次,得到与追加接口一致的初始结果结构(含 audits)
    result = adjudicate(reconcile_anchored(*seqs, anchors), [])
    record = store.create_case(
        texts=req.texts,
        anchors=[list(a) for a in anchors],
        result=result,
    )
    return _case_response(record)


@app.get("/cases/{case_id}", response_model=CaseRecord)
def get_case(case_id: str) -> dict:
    """查询案件完整状态;案件落盘于 SQLite,服务重启后仍可查询。"""
    record = store.get_case(case_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"案件不存在:{case_id}")
    return _case_response(record)


@app.post("/cases/{case_id}/adjudications", response_model=CaseRecord)
def post_case_adjudications(case_id: str, req: AppendAdjudicationsRequest) -> dict:
    """追加裁决:事务内校验 expected_revision、合并既有与新增裁决并重算。

    版本不符或合并后任一裁决失效均返回 409 且不写入;成功后 revision 加一。
    """
    # 事务外先做请求级校验(replacement 规范化与字素簇数),问题请求直接 422
    _to_choices(req.decisions)
    new_decisions = [d.model_dump(exclude_none=True) for d in req.decisions]
    try:
        record = store.append_adjudications(
            case_id, req.expected_revision, new_decisions, _recompute_case
        )
    except CaseNotFound:
        raise HTTPException(status_code=404, detail=f"案件不存在:{case_id}")
    except RevisionConflict as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "revision_conflict",
                "message": (
                    f"expected_revision={req.expected_revision} 与当前版本不符"
                    f"(当前 revision={exc.current}),未写入任何修改"
                ),
                "current_revision": exc.current,
            },
        )
    except AdjudicationConflict as exc:
        # 合并后任一裁决失效即整体回滚(409),detail 下标按"既有在前、追加在后"的合并顺序
        raise HTTPException(status_code=409, detail=_conflict_detail(exc))
    return _case_response(record)
