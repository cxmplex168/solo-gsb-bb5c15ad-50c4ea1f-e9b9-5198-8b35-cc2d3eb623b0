"""三维全局动态规划对齐与共识生成。

三份抄录分别视为序列 A、B、C(均为 NFC 规范化后的字素簇列表)。
每列可消费任意非空来源组合(位掩码 1..7,bit0=A、bit1=B、bit2=C):
  - 字符不等、字符与空位配对:各计 1
  - 空位与空位配对:计 0
目标为总成本最小;平局时依次偏好消费来源数更多、位掩码更小,
即掩码优先级 7 > 3 > 5 > 6 > 1 > 2 > 4。
"""
from __future__ import annotations

import numpy as np

#: 平局时的掩码优先级(消费来源数降序,其次掩码值升序)
MASK_PRIORITY = (7, 3, 5, 6, 1, 2, 4)

#: 共识无法达成时写入的替换字符
REPLACEMENT = "�"

_INF = 1 << 28


def _encode(seq: list[str], vocab: dict[str, int]) -> np.ndarray:
    """把字素簇序列编码为整数数组,便于用 numpy 做相等性比较。"""
    codes = np.empty(len(seq), dtype=np.int32)
    for i, g in enumerate(seq):
        code = vocab.get(g)
        if code is None:
            code = len(vocab)
            vocab[g] = code
        codes[i] = code
    return codes


def align_three(a: list[str], b: list[str], c: list[str]) -> tuple[int, list[tuple[int, list, list]]]:
    """对三段字素簇序列做三维全局对齐。

    返回 (总成本, 列列表);每列为 (mask, [g0, g1, g2], [p0, p1, p2]),
    空位以 None 表示,p* 为字素在对应来源中的原位置(0 起始)。
    """
    n1, n2, n3 = len(a), len(b), len(c)
    vocab: dict[str, int] = {}
    A = _encode(a, vocab)
    B = _encode(b, vocab)
    C = _encode(c, vocab)

    dp = np.full((n1 + 1, n2 + 1, n3 + 1), _INF, dtype=np.int32)
    choice = np.zeros((n1 + 1, n2 + 1, n3 + 1), dtype=np.uint8)
    dp[0, 0, 0] = 0

    # neq_ac[i, k] = 0/1,表示 A[i] 与 C[k] 是否不等
    neq_ac = (A[:, None] != C[None, :]).astype(np.int32)
    neq_bc = (B[:, None] != C[None, :]).astype(np.int32)
    ks = np.arange(n3 + 1, dtype=np.int32)
    two_k = 2 * ks

    priority_rank = {m: r for r, m in enumerate(MASK_PRIORITY)}

    for i in range(n1 + 1):
        for j in range(n2 + 1):
            if i == 0 and j == 0:
                # 只能逐列消费来源 C(掩码 4),每列成本 2
                dp[0, 0, :] = two_k
                choice[0, 0, 1:] = 4
                continue

            # 先处理不依赖本行 k-1 状态的六个掩码(沿 k 向量化)
            best = np.full(n3 + 1, _INF, dtype=np.int32)
            pick = np.zeros(n3 + 1, dtype=np.uint8)
            neq_ab = int(A[i - 1] != B[j - 1]) if i and j else 0

            candidates: list[tuple[int, np.ndarray]] = []
            if i and j:
                if n3:
                    v7 = np.full(n3 + 1, _INF, dtype=np.int32)
                    v7[1:] = dp[i - 1, j - 1, :n3] + neq_ab + neq_ac[i - 1] + neq_bc[j - 1]
                    candidates.append((7, v7))
                candidates.append((3, dp[i - 1, j - 1, :] + 2 + neq_ab))
            if i:
                if n3:
                    v5 = np.full(n3 + 1, _INF, dtype=np.int32)
                    v5[1:] = dp[i - 1, j, :n3] + 2 + neq_ac[i - 1]
                    candidates.append((5, v5))
                candidates.append((1, dp[i - 1, j, :] + 2))
            if j:
                if n3:
                    v6 = np.full(n3 + 1, _INF, dtype=np.int32)
                    v6[1:] = dp[i, j - 1, :n3] + 2 + neq_bc[j - 1]
                    candidates.append((6, v6))
                candidates.append((2, dp[i, j - 1, :] + 2))

            candidates.sort(key=lambda mc: priority_rank[mc[0]])
            for mask, vec in candidates:
                better = vec < best  # 严格更小才更新,平局保留高优先级掩码
                best = np.where(better, vec, best)
                pick = np.where(better, mask, pick)

            # 掩码 4(仅消费 C):dp[k] = min(best[k], dp[k-1] + 2)
            # 即 dp[k] = 2k + min_{t<=k} (best[t] - 2t),用累积最小值向量化
            t = best - two_k
            cumm = np.minimum.accumulate(t)
            dp[i, j, :] = cumm + two_k
            prev = np.empty(n3 + 1, dtype=np.int32)
            prev[0] = _INF
            prev[1:] = cumm[:-1]
            # 平局 t[k] == prev[k] 时优先 best 的掩码(掩码 4 优先级最低)
            choice[i, j, :] = np.where(t <= prev, pick, 4)

    # 回溯复原对齐列
    columns: list[tuple[int, list, list]] = []
    i, j, k = n1, n2, n3
    while i or j or k:
        m = int(choice[i, j, k])
        g = [None, None, None]
        p = [None, None, None]
        if m & 1:
            i -= 1
            g[0], p[0] = a[i], i
        if m & 2:
            j -= 1
            g[1], p[1] = b[j], j
        if m & 4:
            k -= 1
            g[2], p[2] = c[k], k
        columns.append((m, g, p))
    columns.reverse()
    return int(dp[n1, n2, n3]), columns


def column_cost(g: list) -> int:
    """单列成本:字符不等或字符与空位配对各计 1,空位间计 0。"""
    cost = 0
    for x in range(3):
        for y in range(x + 1, 3):
            gx, gy = g[x], g[y]
            if gx is None and gy is None:
                continue
            if gx is None or gy is None or gx != gy:
                cost += 1
    return cost


def _build_result(
    total_cost: int,
    columns: list[tuple[int, list, list]],
    anchored: list[bool] | None = None,
) -> dict:
    """由对齐列生成共识结果;提供 anchored 时每列附带 anchored 标记。"""
    out_columns = []
    uncertain = []
    consensus_chars = []
    for idx, (mask, g, p) in enumerate(columns):
        cost = column_cost(g)
        non_empty = [x for x in g if x is not None]
        # 至少两份相同非空字素时达成共识(三个来源下该字素必唯一)
        cons = next((x for x in non_empty if non_empty.count(x) >= 2), None)
        sources = [{"grapheme": g[s], "position": p[s]} for s in range(3)]
        if cons is None:
            cons = REPLACEMENT
            uncertain.append({"column": idx, "sources": sources})
        consensus_chars.append(cons)
        column = {
            "index": idx,
            "mask": mask,
            "cost": cost,
            "sources": sources,
            "consensus": cons,
        }
        if anchored is not None:
            column["anchored"] = anchored[idx]
        out_columns.append(column)

    return {
        "consensus": "".join(consensus_chars),
        "total_cost": total_cost,
        "columns": out_columns,
        "uncertain": uncertain,
    }


def reconcile(a: list[str], b: list[str], c: list[str]) -> dict:
    """对齐三段字素簇序列并生成共识结果。"""
    total_cost, columns = align_three(a, b, c)
    return _build_result(total_cost, columns)


def reconcile_anchored(
    a: list[str],
    b: list[str],
    c: list[str],
    anchors: list[tuple[int, int, int]],
) -> dict:
    """带锚点的对齐:每个锚点 (i, j, k) 强制 a[i]、b[j]、c[k] 同列。

    锚点须按各来源位置严格递增且不越界(由调用方校验)。以锚点把三段
    序列切分为若干区间,各区间独立求全局最小成本对齐后拼接;锚点列
    (掩码 7)照常参与成本与共识计算。每列附带 anchored 标记,所有位置
    均为原序列绝对下标。
    """
    columns: list[tuple[int, list, list]] = []
    anchored: list[bool] = []
    total_cost = 0
    start = (0, 0, 0)
    for anchor in [*anchors, None]:
        stop = anchor if anchor is not None else (len(a), len(b), len(c))
        seg_cost, seg_columns = align_three(
            a[start[0]:stop[0]], b[start[1]:stop[1]], c[start[2]:stop[2]]
        )
        total_cost += seg_cost
        for mask, g, p in seg_columns:
            # 区间内的相对位置换算回原序列绝对位置
            abs_p = [None if p[s] is None else p[s] + start[s] for s in range(3)]
            columns.append((mask, g, abs_p))
            anchored.append(False)
        if anchor is None:
            continue
        i, j, k = anchor
        g = [a[i], b[j], c[k]]
        columns.append((7, g, [i, j, k]))
        anchored.append(True)
        total_cost += column_cost(g)
        start = (i + 1, j + 1, k + 1)
    return _build_result(total_cost, columns, anchored)


class AdjudicationConflict(Exception):
    """一批裁决中存在失效项;conflicts 收集全部原因,保证不产生部分结果。"""

    def __init__(self, conflicts: list[dict]):
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} 项裁决失效")


def adjudicate(
    reconciled: dict,
    decisions: list[tuple[tuple, str, object]],
) -> dict:
    """对已有对齐结果施加人工裁决。

    每项 decision 为 (signature, kind, value):
      - signature 为三来源原位置元组 (p0, p1, p2),空位来源记 None,与对齐列
        sources 中的 position 三元组逐位相等即视为同一列——按原始坐标而非列
        序号定位,列序变化不会使裁决落到其他位置;
      - kind="source" 时 value 为来源下标 0/1/2,采用该列该来源的非空字素;
      - kind="replacement" 时 value 为一个 NFC 字素簇,直接作为采用值。

    重算对齐后一次性校验全部裁决:目标列须存在、须仍为 uncertain、签名不得
    重复、来源选择不得指向空位。任一失效即抛 AdjudicationConflict(收集全部
    冲突),不产出部分结果。成功返回裁决后的完整结果:consensus 替换为裁决
    文本,uncertain 仅保留未裁决列,并附 audits 审计项(签名、原字素、采用
    值、方式),顺序与 decisions 一致。
    """
    columns = reconciled["columns"]
    by_signature: dict[tuple, int] = {}
    for col in columns:
        signature = tuple(s["position"] for s in col["sources"])
        by_signature.setdefault(signature, col["index"])
    uncertain_indices = {u["column"] for u in reconciled["uncertain"]}

    conflicts: list[dict] = []
    seen: set[tuple] = set()
    resolved: list[tuple] = []
    for di, (signature, kind, value) in enumerate(decisions):
        reasons: list[str] = []
        if signature in seen:
            reasons.append("duplicate")
        seen.add(signature)
        col_idx = by_signature.get(signature)
        if col_idx is None:
            reasons.append("column_not_found")
        else:
            if col_idx not in uncertain_indices:
                reasons.append("not_uncertain")
            if kind == "source":
                if columns[col_idx]["sources"][value]["grapheme"] is None:
                    reasons.append("empty_source")
        if reasons:
            conflicts.append(
                {"decision": di, "signature": list(signature), "reasons": reasons}
            )
            continue
        col = columns[col_idx]
        if kind == "source":
            adopted = col["sources"][value]["grapheme"]
            method = f"source:{value}"
        else:
            adopted = value
            method = "replacement"
        original = [s["grapheme"] for s in col["sources"]]
        resolved.append((col_idx, signature, original, adopted, method))

    if conflicts:
        raise AdjudicationConflict(conflicts)

    adopted_by_index = {col_idx: adopted for col_idx, _, _, adopted, _ in resolved}
    new_columns = []
    consensus_chars = []
    for col in columns:
        new_col = dict(col)
        if col["index"] in adopted_by_index:
            new_col["consensus"] = adopted_by_index[col["index"]]
        consensus_chars.append(new_col["consensus"])
        new_columns.append(new_col)

    remaining_uncertain = [
        u for u in reconciled["uncertain"] if u["column"] not in adopted_by_index
    ]
    audits = [
        {
            "signature": list(signature),
            "original": original,
            "adopted": adopted,
            "method": method,
        }
        for _, signature, original, adopted, method in resolved
    ]
    return {
        **reconciled,
        "consensus": "".join(consensus_chars),
        "columns": new_columns,
        "uncertain": remaining_uncertain,
        "audits": audits,
    }
