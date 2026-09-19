"""核心对齐逻辑与 API 的测试。"""
from __future__ import annotations

import itertools
import random
import unicodedata

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reconcile import (
    MASK_PRIORITY,
    AdjudicationConflict,
    adjudicate,
    align_three,
    column_cost,
    reconcile,
    reconcile_anchored,
)

client = TestClient(app)


# ---------------------------------------------------------------- 参考实现

def _slow_align(a, b, c):
    """纯 Python 三维 DP 参考实现,用于交叉验证。"""
    n1, n2, n3 = len(a), len(b), len(c)
    inf = float("inf")
    dp = {}
    ch = {}

    def col_cost(g):
        cost = 0
        for x, y in ((0, 1), (0, 2), (1, 2)):
            gx, gy = g[x], g[y]
            if gx is None and gy is None:
                continue
            if gx is None or gy is None or gx != gy:
                cost += 1
        return cost

    for i in range(n1 + 1):
        for j in range(n2 + 1):
            for k in range(n3 + 1):
                if i == j == k == 0:
                    dp[(0, 0, 0)] = 0
                    continue
                best, best_mask = inf, None
                for mask in MASK_PRIORITY:
                    di, dj, dk = mask & 1, (mask >> 1) & 1, (mask >> 2) & 1
                    if di > i or dj > j or dk > k:
                        continue
                    g = [
                        a[i - 1] if di else None,
                        b[j - 1] if dj else None,
                        c[k - 1] if dk else None,
                    ]
                    v = dp[(i - di, j - dj, k - dk)] + col_cost(g)
                    if v < best:
                        best, best_mask = v, mask
                dp[(i, j, k)] = best
                ch[(i, j, k)] = best_mask

    columns = []
    i, j, k = n1, n2, n3
    while i or j or k:
        m = ch[(i, j, k)]
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
    return dp[(n1, n2, n3)], columns


# ---------------------------------------------------------------- 基础性质

def test_identical_texts_zero_cost():
    total, columns = align_three(list("档案抄录"), list("档案抄录"), list("档案抄录"))
    assert total == 0
    assert all(m == 7 for m, _, _ in columns)
    assert len(columns) == 4


def test_column_cost_rules():
    assert column_cost(["甲", "甲", "甲"]) == 0
    assert column_cost(["甲", "甲", "乙"]) == 2
    assert column_cost(["甲", "乙", "丙"]) == 3
    assert column_cost(["甲", "甲", None]) == 2
    assert column_cost(["甲", "乙", None]) == 3
    assert column_cost(["甲", None, None]) == 2
    assert column_cost([None, None, None]) == 0


def test_total_cost_matches_column_sum():
    rng = random.Random(7)
    for _ in range(30):
        seqs = [[rng.choice("甲乙丙丁") for _ in range(rng.randint(0, 6))] for _ in range(3)]
        total, columns = align_three(*seqs)
        assert total == sum(column_cost(g) for _, g, _ in columns)


def test_fuzz_against_slow_reference():
    rng = random.Random(42)
    alphabets = ["甲乙", "甲乙丙", "甲乙丙丁戊"]
    for _ in range(120):
        alpha = rng.choice(alphabets)
        seqs = [[rng.choice(alpha) for _ in range(rng.randint(0, 5))] for _ in range(3)]
        fast_total, fast_cols = align_three(*seqs)
        slow_total, slow_cols = _slow_align(*seqs)
        assert fast_total == slow_total, seqs
        assert fast_cols == slow_cols, seqs  # 平局规则一致,列应完全相同


def test_tie_break_prefers_more_sources_then_smaller_mask():
    # 手工验算:dp[1,2,1] 处掩码 7、6、2 同为 4 分,按优先级选 7
    total, columns = align_three(list("甲"), list("甲乙"), list("乙"))
    assert total == 4
    assert [m for m, _, _ in columns] == [2, 7]
    assert columns[1][1] == ["甲", "乙", "乙"]


def test_positions_are_original_indices():
    total, columns = align_three(list("甲X乙"), list("甲乙"), list("甲乙"))
    assert total == 2
    assert [m for m, _, _ in columns] == [7, 1, 7]
    # 第二列只消费来源 0 的 "X"(原位置 1)
    m, g, p = columns[1]
    assert m == 1 and g == ["X", None, None] and p == [1, None, None]
    # 其余列三方对齐,位置各自对应原序列下标
    assert columns[0][1] == ["甲", "甲", "甲"]
    assert columns[0][2] == [0, 0, 0]
    assert columns[2][2] == [2, 1, 1]


def test_empty_texts():
    total, columns = align_three([], [], [])
    assert total == 0 and columns == []
    total, columns = align_three(list("甲乙"), [], [])
    assert total == 4  # 两列,每列仅一个非空字素,成本各 2
    assert [m for m, _, _ in columns] == [1, 1]


# ---------------------------------------------------------------- 共识规则

def test_consensus_majority_and_replacement():
    result = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    assert result["consensus"] == "甲�"
    assert result["total_cost"] == 3  # 首列全同计 0,次列三者互异计 3
    assert len(result["uncertain"]) == 1
    u = result["uncertain"][0]
    assert u["column"] == 1
    assert [s["grapheme"] for s in u["sources"]] == ["丙", "丁", "乙"]
    assert [s["position"] for s in u["sources"]] == [1, 1, 1]


def test_consensus_two_of_three_agree_with_gap():
    # 来源 2 为空位,来源 0/1 一致 -> 仍达成共识
    result = reconcile(list("甲乙"), list("甲丙"), [])
    assert result["consensus"] == "甲�"
    assert result["uncertain"][0]["sources"][2] == {"grapheme": None, "position": None}


def test_consensus_all_identical():
    result = reconcile(list("档案"), list("档案"), list("档案"))
    assert result["consensus"] == "档案"
    assert result["total_cost"] == 0
    assert result["uncertain"] == []


# ---------------------------------------------------------------- API

def test_api_happy_path():
    resp = client.post("/reconcile", json={"texts": ["档案", "档案", "档案"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "档案"
    assert body["total_cost"] == 0
    assert len(body["columns"]) == 2
    assert body["uncertain"] == []


def test_api_nfc_normalization():
    # "é" 的分解形式(NFD)与组合形式(NFC)应视为同一字素
    nfd = unicodedata.normalize("NFD", "é")
    nfc = unicodedata.normalize("NFC", "é")
    assert nfd != nfc
    resp = client.post("/reconcile", json={"texts": [nfd, nfc, nfc]})
    assert resp.status_code == 200
    assert resp.json()["total_cost"] == 0
    assert resp.json()["consensus"] == nfc


def test_api_grapheme_clusters_counted_not_codepoints():
    # 家庭 emoji 是单个字素簇但含多个码点;120 个簇合法,121 个拒绝
    family = "👨‍👩‍👧‍👦"
    ok = client.post("/reconcile", json={"texts": [family * 120, "", ""]})
    assert ok.status_code == 200
    too_many = client.post("/reconcile", json={"texts": [family * 121, "", ""]})
    assert too_many.status_code == 422


def test_api_requires_exactly_three_texts():
    for payload in ({"texts": ["a", "b"]}, {"texts": ["a", "b", "c", "d"]}, {"texts": []}):
        assert client.post("/reconcile", json=payload).status_code == 422


def test_api_rejects_over_120_graphemes():
    assert client.post("/reconcile", json={"texts": ["字" * 121, "", ""]}).status_code == 422
    assert client.post("/reconcile", json={"texts": ["字" * 120, "", ""]}).status_code == 200


def test_api_response_shape():
    resp = client.post("/reconcile", json={"texts": ["甲乙", "甲丙", "甲丁"]})
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"consensus", "total_cost", "columns", "uncertain"}
    col = body["columns"][0]
    assert set(col) == {"index", "mask", "cost", "sources", "consensus"}
    assert len(col["sources"]) == 3
    assert body["consensus"] == "甲�"
    assert len(body["uncertain"]) == 1


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


# ---------------------------------------------------------------- 锚点对齐

def _slow_anchored(a, b, c, anchors):
    """参考实现:按锚点切分区间,各区间用慢速 DP 对齐后拼接。"""
    total = 0
    columns = []  # (mask, g, abs_p, anchored)
    start = (0, 0, 0)
    for anchor in [*anchors, None]:
        stop = anchor if anchor is not None else (len(a), len(b), len(c))
        seg_cost, seg_cols = _slow_align(
            a[start[0]:stop[0]], b[start[1]:stop[1]], c[start[2]:stop[2]]
        )
        total += seg_cost
        for m, g, p in seg_cols:
            abs_p = [None if p[s] is None else p[s] + start[s] for s in range(3)]
            columns.append((m, g, abs_p, False))
        if anchor is None:
            continue
        i, j, k = anchor
        g = [a[i], b[j], c[k]]
        total += column_cost(g)
        columns.append((7, g, [i, j, k], True))
        start = (i + 1, j + 1, k + 1)
    return total, columns


def test_anchored_forces_shared_column():
    # 无锚点时 "X" 单独成列(总成本 2);锚点强制 X/甲/甲 同列,总成本变 4
    plain = reconcile(list("X甲"), list("甲"), list("甲"))
    assert plain["total_cost"] == 2
    result = reconcile_anchored(list("X甲"), list("甲"), list("甲"), [(0, 0, 0)])
    assert result["total_cost"] == 4
    assert [c["anchored"] for c in result["columns"]] == [True, False]
    anchor_col = result["columns"][0]
    assert anchor_col["mask"] == 7
    assert anchor_col["cost"] == 2
    assert anchor_col["consensus"] == "甲"
    assert [s["grapheme"] for s in anchor_col["sources"]] == ["X", "甲", "甲"]
    assert [s["position"] for s in anchor_col["sources"]] == [0, 0, 0]
    # 后续列位置仍为原序列绝对下标
    tail = result["columns"][1]
    assert tail["sources"][0] == {"grapheme": "甲", "position": 1}
    assert tail["sources"][1] == {"grapheme": None, "position": None}


def test_anchored_empty_anchors_matches_plain():
    seqs = [list("甲乙丙"), list("甲丙"), list("甲乙X丙")]
    plain = reconcile(*seqs)
    anchored = reconcile_anchored(*seqs, [])
    assert anchored["consensus"] == plain["consensus"]
    assert anchored["total_cost"] == plain["total_cost"]
    assert anchored["uncertain"] == plain["uncertain"]
    assert [c["anchored"] for c in anchored["columns"]] == [False] * len(plain["columns"])
    for got, want in zip(anchored["columns"], plain["columns"]):
        assert {k: v for k, v in got.items() if k != "anchored"} == want


def test_anchored_column_participates_in_consensus_and_uncertain():
    # 锚点强制三个互异字素同列:成本 3,共识为 �,进入 uncertain
    result = reconcile_anchored(list("甲"), list("乙"), list("丙"), [(0, 0, 0)])
    assert result["total_cost"] == 3
    assert result["consensus"] == "�"
    assert result["columns"][0]["anchored"] is True
    assert result["uncertain"] == [
        {
            "column": 0,
            "sources": [
                {"grapheme": "甲", "position": 0},
                {"grapheme": "乙", "position": 0},
                {"grapheme": "丙", "position": 0},
            ],
        }
    ]


def test_anchored_multiple_monotonic_anchors():
    result = reconcile_anchored(
        list("甲乙丙丁"), list("甲乙丙丁"), list("甲乙丙丁"), [(0, 0, 0), (3, 3, 3)]
    )
    assert result["total_cost"] == 0
    assert result["consensus"] == "甲乙丙丁"
    assert [c["anchored"] for c in result["columns"]] == [True, False, False, True]
    # 中间区间列的位置已换算回绝对下标
    assert [[s["position"] for s in c["sources"]] for c in result["columns"]] == [
        [0, 0, 0],
        [1, 1, 1],
        [2, 2, 2],
        [3, 3, 3],
    ]


def test_anchored_fuzz_against_segmented_reference():
    rng = random.Random(2024)
    for _ in range(80):
        alpha = rng.choice(["甲乙", "甲乙丙"])
        seqs = [[rng.choice(alpha) for _ in range(rng.randint(0, 5))] for _ in range(3)]
        # 随机合法锚点:各来源位置严格递增且不越界
        anchors = []
        prev = [-1, -1, -1]
        for _ in range(rng.randint(0, 2)):
            pos = []
            for s in range(3):
                lo, hi = prev[s] + 1, len(seqs[s]) - 1
                if lo > hi:
                    break
                pos.append(rng.randint(lo, hi))
            if len(pos) < 3:
                break
            anchors.append(tuple(pos))
            prev = pos
        result = reconcile_anchored(*seqs, anchors)
        ref_total, ref_cols = _slow_anchored(*seqs, anchors)
        assert result["total_cost"] == ref_total
        got = [
            (
                c["mask"],
                [s["grapheme"] for s in c["sources"]],
                [s["position"] for s in c["sources"]],
                c["anchored"],
            )
            for c in result["columns"]
        ]
        assert got == ref_cols
        # 总成本等于各列成本之和(锚点列也计入)
        assert result["total_cost"] == sum(c["cost"] for c in result["columns"])


# ---------------------------------------------------------------- 锚点 API

def test_api_anchored_happy_path():
    resp = client.post(
        "/reconcile/anchored",
        json={"texts": ["甲乙", "甲丙", "甲乙"], "anchors": [{"positions": [1, 1, 1]}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "甲乙"
    assert body["total_cost"] == 2
    assert [c["anchored"] for c in body["columns"]] == [False, True]
    anchor_col = body["columns"][1]
    assert set(anchor_col) == {"index", "mask", "cost", "sources", "consensus", "anchored"}
    assert anchor_col["mask"] == 7
    assert [s["grapheme"] for s in anchor_col["sources"]] == ["乙", "丙", "乙"]
    assert [s["position"] for s in anchor_col["sources"]] == [1, 1, 1]
    assert anchor_col["consensus"] == "乙"
    assert body["uncertain"] == []


def test_api_anchored_defaults_to_empty_anchors():
    resp = client.post("/reconcile/anchored", json={"texts": ["甲", "甲", "甲"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "甲"
    assert [c["anchored"] for c in body["columns"]] == [False]


def test_api_anchored_rejects_invalid_anchors():
    base = {"texts": ["甲乙", "甲乙", "甲乙"]}

    def post(anchors):
        return client.post("/reconcile/anchored", json={**base, "anchors": anchors})

    # 越界(上界与负数)
    assert post([{"positions": [0, 0, 2]}]).status_code == 422
    assert post([{"positions": [-1, 0, 0]}]).status_code == 422
    # 非严格递增(来源 0 回退)
    assert post([{"positions": [1, 0, 0]}, {"positions": [0, 1, 1]}]).status_code == 422
    # 重复锚点
    assert post([{"positions": [0, 0, 0]}, {"positions": [0, 0, 0]}]).status_code == 422
    # 某一来源位置重复
    assert post([{"positions": [0, 0, 0]}, {"positions": [1, 0, 1]}]).status_code == 422
    # positions 长度不为 3
    assert post([{"positions": [0, 0]}]).status_code == 422
    assert post([{"positions": [0, 0, 0, 0]}]).status_code == 422
    # 空锚点列表合法
    assert post([]).status_code == 200


def test_api_anchored_respects_grapheme_limit():
    assert (
        client.post(
            "/reconcile/anchored",
            json={"texts": ["字" * 121, "", ""], "anchors": []},
        ).status_code
        == 422
    )


# ---------------------------------------------------------------- 性能

def test_worst_case_performance():
    import time

    rng = random.Random(1)
    alphabet = [chr(0x4E00 + i) for i in range(200)]
    seqs = [[rng.choice(alphabet) for _ in range(120)] for _ in range(3)]
    start = time.perf_counter()
    result = reconcile(*seqs)
    elapsed = time.perf_counter() - start
    assert result["total_cost"] > 0
    assert elapsed < 10, f"120x120x120 对齐耗时 {elapsed:.1f}s"


# ---------------------------------------------------------------- 人工裁决(核心)


def _sig(col):
    """由对齐列构造三来源原始位置签名。"""
    return tuple(s["position"] for s in col["sources"])


def test_adjudicate_source_choice_replaces_uncertain():
    base = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    result = adjudicate(base, [((1, 1, 1), "source", 0)])
    assert result["consensus"] == "甲丙"
    assert result["uncertain"] == []
    assert result["columns"][1]["consensus"] == "丙"
    assert result["audits"] == [
        {
            "signature": [1, 1, 1],
            "original": ["丙", "丁", "乙"],
            "adopted": "丙",
            "method": "source:0",
        }
    ]
    # 未裁决的部分与对齐本身不变:总成本、列结构保持
    assert result["total_cost"] == base["total_cost"]
    assert [c["mask"] for c in result["columns"]] == [c["mask"] for c in base["columns"]]


def test_adjudicate_replacement_grapheme():
    base = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    result = adjudicate(base, [((1, 1, 1), "replacement", "★")])
    assert result["consensus"] == "甲★"
    assert result["audits"][0] == {
        "signature": [1, 1, 1],
        "original": ["丙", "丁", "乙"],
        "adopted": "★",
        "method": "replacement",
    }


def test_adjudicate_partial_leaves_remaining_uncertain():
    # 两个不确定列,只裁决第一个
    base = reconcile(list("甲丙乙"), list("甲丁戊"), list("甲己亥"))
    assert [u["column"] for u in base["uncertain"]] == [1, 2]
    result = adjudicate(base, [((1, 1, 1), "source", 1)])
    assert result["consensus"] == "甲丁�"
    assert [u["column"] for u in result["uncertain"]] == [2]
    assert len(result["audits"]) == 1


def test_adjudicate_null_positions_in_signature():
    # 只消费单个来源的列:签名含 None,仍能唯一定位
    base = reconcile(list("甲乙"), [], [])
    sig0, sig1 = _sig(base["columns"][0]), _sig(base["columns"][1])
    assert sig0 == (0, None, None) and sig1 == (1, None, None)
    result = adjudicate(base, [(sig0, "source", 0), (sig1, "replacement", "丁")])
    assert result["consensus"] == "甲丁"
    assert result["audits"][0]["original"] == ["甲", None, None]
    assert result["audits"][1]["original"] == ["乙", None, None]


def test_adjudicate_audits_follow_request_order_not_column_order():
    base = reconcile(list("甲丙乙"), list("甲丁戊"), list("甲己亥"))
    decisions = [((2, 2, 2), "source", 2), ((1, 1, 1), "replacement", "★")]
    result = adjudicate(base, decisions)
    assert result["consensus"] == "甲★亥"
    assert [a["signature"] for a in result["audits"]] == [[2, 2, 2], [1, 1, 1]]


def test_adjudicate_empty_decisions_is_identity_plus_audits():
    base = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    result = adjudicate(base, [])
    assert result["consensus"] == base["consensus"]
    assert result["uncertain"] == base["uncertain"]
    assert result["columns"] == base["columns"]
    assert result["audits"] == []


def test_adjudicate_conflict_missing_column():
    base = reconcile(list("甲"), list("甲"), list("甲"))
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(base, [((5, 5, 5), "source", 0)])
    assert exc.value.conflicts == [
        {"decision": 0, "signature": [5, 5, 5], "reasons": ["column_not_found"]}
    ]


def test_adjudicate_conflict_not_uncertain():
    base = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(base, [((0, 0, 0), "source", 0)])
    assert exc.value.conflicts[0]["reasons"] == ["not_uncertain"]


def test_adjudicate_conflict_duplicate_signature():
    base = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(
            base,
            [((1, 1, 1), "source", 0), ((1, 1, 1), "replacement", "★")],
        )
    reasons = {item["decision"]: item["reasons"] for item in exc.value.conflicts}
    assert reasons == {1: ["duplicate"]}  # 仅后出现者计重复,首个本身合法


def test_adjudicate_conflict_empty_source():
    base = reconcile(list("甲乙"), [], [])
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(base, [((0, None, None), "source", 1)])
    assert exc.value.conflicts[0]["reasons"] == ["empty_source"]


def test_adjudicate_collects_all_conflicts_atomically():
    # 多项失效时一次性返回全部原因,且不产出部分结果
    base = reconcile(list("甲丙"), list("甲丁"), list("甲乙"))
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(
            base,
            [
                ((9, 9, 9), "source", 0),       # 列不存在
                ((0, 0, 0), "source", 0),       # 非 uncertain
                ((1, 1, 1), "source", 0),       # 合法
                ((1, 1, 1), "source", 1),       # 重复
                ((1, 1, 1), "source", 2),       # 仍重复
            ],
        )
    conflicts = exc.value.conflicts
    assert [item["decision"] for item in conflicts] == [0, 1, 3, 4]
    assert conflicts[0]["reasons"] == ["column_not_found"]
    assert conflicts[1]["reasons"] == ["not_uncertain"]
    assert conflicts[2]["reasons"] == ["duplicate"]
    assert conflicts[3]["reasons"] == ["duplicate"]
    # 即便其中存在一项合法裁决,整体失败时原结果未被修改
    assert base["consensus"] == "甲�"


def test_adjudicate_multiple_reasons_on_one_decision():
    # 重复签名 + 列不存在,两个原因同时报出
    base = reconcile(list("甲"), list("甲"), list("甲"))
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(
            base,
            [((3, 3, 3), "source", 0), ((3, 3, 3), "source", 0)],
        )
    assert exc.value.conflicts[0]["reasons"] == ["column_not_found"]
    assert exc.value.conflicts[1]["reasons"] == ["duplicate", "column_not_found"]


def test_adjudicate_works_with_anchored_columns():
    base = reconcile_anchored(list("甲"), list("乙"), list("丙"), [(0, 0, 0)])
    assert base["uncertain"][0]["column"] == 0
    result = adjudicate(base, [((0, 0, 0), "source", 1)])
    assert result["consensus"] == "乙"
    assert result["columns"][0]["anchored"] is True
    assert result["audits"][0] == {
        "signature": [0, 0, 0],
        "original": ["甲", "乙", "丙"],
        "adopted": "乙",
        "method": "source:1",
    }


def test_adjudicate_signature_survives_column_index_shift():
    # 定位只看三来源原始位置签名,与列 index 无关:把对齐列整体重排并重新编号后,
    # 裁决仍落到签名匹配的物理列上,而非同 index 的其他列。
    base = reconcile(list("子甲丙乙"), list("子丁戊己"), list("子庚子辛"))
    sig_by_index = {c["index"]: _sig(c) for c in base["columns"]}
    cols = sorted(base["columns"], key=lambda c: c["index"], reverse=True)
    reordered = dict(base)
    reordered["columns"] = [dict(c, index=new) for new, c in enumerate(cols)]
    reordered["uncertain"] = [
        {
            **u,
            "column": next(
                c["index"] for c in reordered["columns"] if _sig(c) == sig_by_index[u["column"]]
            ),
        }
        for u in base["uncertain"]
    ]
    # 反转重编号:签名 (1,1,1) 从 index 1 移到 index 2;index 1 改被 (2,2,2) 占据
    assert [(c["index"], _sig(c)) for c in reordered["columns"]] == [
        (0, (3, 3, 3)),
        (1, (2, 2, 2)),
        (2, (1, 1, 1)),
        (3, (0, 0, 0)),
    ]
    result = adjudicate(reordered, [((1, 1, 1), "source", 1)])
    target = next(c for c in result["columns"] if _sig(c) == (1, 1, 1))
    assert target["consensus"] == "丁"
    assert next(c for c in result["columns"] if _sig(c) == (2, 2, 2))["consensus"] == "�"
    assert next(c for c in result["columns"] if _sig(c) == (0, 0, 0))["consensus"] == "子"


def test_adjudicate_anchor_changes_which_columns_exist():
    # 锚点改变对齐结构:同一签名可能只在其中一种对齐里存在,
    # 按坐标命中正确物理列;结构不符时报 column_not_found 而非误裁。
    plain = reconcile(list("X甲"), list("甲"), list("甲"))
    assert _sig(plain["columns"][0]) == (0, None, None)
    r1 = adjudicate(plain, [((0, None, None), "source", 0)])
    assert r1["columns"][0]["sources"][0]["grapheme"] == "X"
    assert r1["consensus"][0] == "X"

    anchored = reconcile_anchored(list("X甲"), list("甲"), list("甲"), [(0, 0, 0)])
    assert [(c["index"], _sig(c)) for c in anchored["columns"]] == [
        (0, (0, 0, 0)),
        (1, (1, None, None)),
    ]
    # 旧签名在锚点对齐中已不存在
    with pytest.raises(AdjudicationConflict) as exc:
        adjudicate(anchored, [((0, None, None), "source", 0)])
    assert exc.value.conflicts[0]["reasons"] == ["column_not_found"]
    # 新签名 (1,None,None) 命中锚点之后的单源甲列
    r2 = adjudicate(anchored, [((1, None, None), "source", 0)])
    assert r2["columns"][1]["mask"] == 1
    assert r2["columns"][1]["consensus"] == "甲"


# ---------------------------------------------------------------- 人工裁决 API


def test_api_adjudicate_source_happy_path():
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲丙", "甲丁", "甲乙"],
            "decisions": [{"signature": [1, 1, 1], "source": 0}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "甲丙"
    assert body["uncertain"] == []
    assert body["columns"][1]["consensus"] == "丙"
    assert body["audits"] == [
        {
            "signature": [1, 1, 1],
            "original": ["丙", "丁", "乙"],
            "adopted": "丙",
            "method": "source:0",
        }
    ]
    assert set(body) == {"consensus", "total_cost", "columns", "uncertain", "audits"}
    # 无锚点时列也带 anchored 标记,响应结构与 anchored 接口一致
    assert body["columns"][0]["anchored"] is False


def test_api_adjudicate_replacement_normalizes_nfc_and_graphemes():
    nfd = unicodedata.normalize("NFD", "é")
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲丙", "甲丁", "甲乙"],
            "decisions": [{"signature": [1, 1, 1], "replacement": nfd}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["audits"][0]["adopted"] == unicodedata.normalize("NFC", "é")

    # 多码点但单个字素簇的 emoji 合法
    family = "👨‍👩‍👧‍👦"
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲丙", "甲丁", "甲乙"],
            "decisions": [{"signature": [1, 1, 1], "replacement": family}],
        },
    )
    assert resp.status_code == 200
    assert resp.json()["consensus"] == "甲" + family


def test_api_adjudicate_with_anchors():
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲", "乙", "丙"],
            "anchors": [{"positions": [0, 0, 0]}],
            "decisions": [{"signature": [0, 0, 0], "source": 2}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "丙"
    assert body["columns"][0]["anchored"] is True
    assert body["audits"][0]["method"] == "source:2"


def test_api_adjudicate_null_signature_slots():
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲乙", "", ""],
            "decisions": [
                {"signature": [0, None, None], "source": 0},
                {"signature": [1, None, None], "replacement": "丁"},
            ],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "甲丁"
    assert body["uncertain"] == []
    assert [a["signature"] for a in body["audits"]] == [[0, None, None], [1, None, None]]


def test_api_adjudicate_defaults_empty_decisions():
    resp = client.post("/reconcile/adjudicate", json={"texts": ["甲丙", "甲丁", "甲乙"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["consensus"] == "甲�"
    assert body["audits"] == []
    assert len(body["uncertain"]) == 1
    assert all("anchored" in col for col in body["columns"])


def test_api_adjudicate_conflicts_return_409_with_partial_safety():
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲丙", "甲丁", "甲乙"],
            "decisions": [
                {"signature": [9, 9, 9], "source": 0},
                {"signature": [0, 0, 0], "source": 0},
                {"signature": [1, 1, 1], "source": 0},
                {"signature": [1, 1, 1], "source": 1},
            ],
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert [item["decision"] for item in detail] == [0, 1, 3]
    assert "不存在" in detail[0]["reasons"][0]
    assert "uncertain" in detail[1]["reasons"][0]
    assert "重复" in detail[2]["reasons"][0]
    # 409 响应不包含任何裁决后的共识结果
    assert set(resp.json()) == {"detail"}


def test_api_adjudicate_empty_source_returns_409():
    resp = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["甲乙", "", ""],
            "decisions": [{"signature": [0, None, None], "source": 2}],
        },
    )
    assert resp.status_code == 409
    assert "空位" in resp.json()["detail"][0]["reasons"][0]


def test_api_adjudicate_recomputes_alignment_each_request():
    # 同一签名在不带锚点时为不确定列、带锚点后变为确定列:服务端按本次请求重算
    # 对齐,旧签名若已不是 uncertain 则拒绝(列序/列内容变化不会误裁)
    without_anchor = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["X甲", "甲", "甲"],
            "decisions": [{"signature": [0, None, None], "source": 0}],
        },
    )
    assert without_anchor.status_code == 200  # 无锚点:X 单独成列,可裁决

    with_anchor = client.post(
        "/reconcile/adjudicate",
        json={
            "texts": ["X甲", "甲", "甲"],
            "anchors": [{"positions": [0, 0, 0]}],
            "decisions": [{"signature": [0, None, None], "source": 0}],
        },
    )
    assert with_anchor.status_code == 409
    assert "不存在" in with_anchor.json()["detail"][0]["reasons"][0]


def test_api_adjudicate_validation_errors_are_422():
    base = {"texts": ["甲丙", "甲丁", "甲乙"]}

    def post(decision):
        return client.post(
            "/reconcile/adjudicate", json={**base, "decisions": [decision]}
        )

    # source 与 replacement 同时提供 / 都不提供
    assert post({"signature": [1, 1, 1], "source": 0, "replacement": "X"}).status_code == 422
    assert post({"signature": [1, 1, 1]}).status_code == 422
    # signature 长度不为 3 / 含负数
    assert post({"signature": [1, 1], "source": 0}).status_code == 422
    assert post({"signature": [-1, 1, 1], "source": 0}).status_code == 422
    # source 越界
    assert post({"signature": [1, 1, 1], "source": 3}).status_code == 422
    # replacement 为空串、超过一个字素簇
    assert post({"signature": [1, 1, 1], "replacement": ""}).status_code == 422
    assert post({"signature": [1, 1, 1], "replacement": "甲乙"}).status_code == 422
    # 字素簇上限与锚点规则同样适用
    assert (
        client.post(
            "/reconcile/adjudicate",
            json={"texts": ["字" * 121, "", ""], "decisions": []},
        ).status_code
        == 422
    )
    assert (
        client.post(
            "/reconcile/adjudicate",
            json={
                "texts": ["甲乙", "甲乙", "甲乙"],
                "anchors": [{"positions": [2, 0, 0]}],
                "decisions": [],
            },
        ).status_code
        == 422
    )


def test_api_legacy_endpoints_still_compatible():
    r1 = client.post("/reconcile", json={"texts": ["甲丙", "甲丁", "甲乙"]})
    assert r1.status_code == 200
    assert set(r1.json()) == {"consensus", "total_cost", "columns", "uncertain"}
    assert "anchored" not in r1.json()["columns"][0]

    r2 = client.post(
        "/reconcile/anchored",
        json={"texts": ["甲乙", "甲丙", "甲乙"], "anchors": [{"positions": [1, 1, 1]}]},
    )
    assert r2.status_code == 200
    assert "anchored" in r2.json()["columns"][0]
    assert client.get("/health").json() == {"status": "ok"}
