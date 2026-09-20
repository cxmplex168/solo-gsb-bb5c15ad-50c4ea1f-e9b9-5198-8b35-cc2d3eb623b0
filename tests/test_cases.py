"""持久化核对案件模块的测试:创建、查询、追加裁决、版本控制与重启持久性。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.reconcile import adjudicate, reconcile_anchored
from app.store import CaseStore, RevisionConflict

TEXTS = ["甲丙", "甲丁", "甲乙"]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个用例独立的 SQLite 库,并替换应用全局 store。"""
    store = CaseStore(str(tmp_path / "cases.db"))
    monkeypatch.setattr("app.main.store", store)
    return TestClient(app)


def _create(client, **overrides):
    payload = {"texts": TEXTS, **overrides}
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------- 创建与查询

def test_create_case_returns_initial_state(client):
    body = _create(client)
    assert body["revision"] == 0
    assert body["texts"] == TEXTS
    assert body["anchors"] == []
    assert body["decisions"] == []
    result = body["result"]
    assert result["consensus"] == "甲�"
    assert result["total_cost"] == 3
    assert result["audits"] == []
    assert len(result["uncertain"]) == 1
    assert all("anchored" in col for col in result["columns"])
    assert set(body) == {"case_id", "revision", "texts", "anchors", "decisions", "result"}


def test_create_case_with_anchors(client):
    body = _create(client, texts=["甲", "乙", "丙"], anchors=[{"positions": [0, 0, 0]}])
    assert body["anchors"] == [{"positions": [0, 0, 0]}]
    assert body["result"]["columns"][0]["anchored"] is True
    assert body["result"]["consensus"] == "�"


def test_create_case_validation_errors(client):
    assert client.post("/cases", json={"texts": ["甲", "乙"]}).status_code == 422
    assert client.post("/cases", json={"texts": ["字" * 121, "", ""]}).status_code == 422
    assert (
        client.post(
            "/cases",
            json={"texts": ["甲乙", "甲乙", "甲乙"], "anchors": [{"positions": [0, 0, 2]}]},
        ).status_code
        == 422
    )


def test_get_case_roundtrip(client):
    created = _create(client)
    resp = client.get(f"/cases/{created['case_id']}")
    assert resp.status_code == 200
    assert resp.json() == created


def test_get_missing_case_404(client):
    assert client.get("/cases/does-not-exist").status_code == 404


# ---------------------------------------------------------------- 追加裁决

def test_append_adjudications_success(client):
    case = _create(client)
    resp = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 1
    assert body["decisions"] == [{"signature": [1, 1, 1], "source": 0, "replacement": None}]
    assert body["result"]["consensus"] == "甲丙"
    assert body["result"]["uncertain"] == []
    assert body["result"]["audits"] == [
        {"signature": [1, 1, 1], "original": ["丙", "丁", "乙"], "adopted": "丙", "method": "source:0"}
    ]
    # 查询接口看到同样的持久化状态
    assert client.get(f"/cases/{case['case_id']}").json() == body


def test_append_merges_with_existing_decisions(client):
    case = _create(client, texts=["甲丙乙", "甲丁戊", "甲己亥"])
    assert [u["column"] for u in case["result"]["uncertain"]] == [1, 2]
    r1 = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 1}]},
    )
    assert r1.status_code == 200 and r1.json()["revision"] == 1
    r2 = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 1, "decisions": [{"signature": [2, 2, 2], "replacement": "★"}]},
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["revision"] == 2
    assert len(body["decisions"]) == 2
    assert body["result"]["consensus"] == "甲丁★"
    assert body["result"]["uncertain"] == []
    # 审计项覆盖全部已生效裁决(既有在前、追加在后)
    assert [a["signature"] for a in body["result"]["audits"]] == [[1, 1, 1], [2, 2, 2]]
    assert [a["method"] for a in body["result"]["audits"]] == ["source:1", "replacement"]


def test_append_revision_mismatch_returns_409_and_writes_nothing(client):
    case = _create(client)
    resp = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 7, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error"] == "revision_conflict"
    assert detail["current_revision"] == 0
    # 未写入任何修改
    after = client.get(f"/cases/{case['case_id']}").json()
    assert after["revision"] == 0
    assert after["decisions"] == []
    assert after["result"]["consensus"] == "甲�"


def test_append_stale_revision_after_success_returns_409(client):
    case = _create(client)
    ok = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert ok.status_code == 200
    stale = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 1}]},
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 1
    assert client.get(f"/cases/{case['case_id']}").json()["revision"] == 1


def test_append_invalid_decision_returns_409_and_writes_nothing(client):
    case = _create(client)
    resp = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={
            "expected_revision": 0,
            "decisions": [
                {"signature": [0, 0, 0], "source": 0},   # 已共识列,非 uncertain
                {"signature": [9, 9, 9], "source": 0},   # 列不存在
            ],
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert [item["decision"] for item in detail] == [0, 1]
    assert "uncertain" in detail[0]["reasons"][0]
    assert "不存在" in detail[1]["reasons"][0]
    # 整体回滚:版本、裁决列表、当前结果均不变
    after = client.get(f"/cases/{case['case_id']}").json()
    assert after["revision"] == 0
    assert after["decisions"] == []
    assert after["result"]["consensus"] == "甲�"


def test_append_duplicate_of_existing_decision_returns_409(client):
    case = _create(client)
    first = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert first.status_code == 200
    dup = client.post(
        f"/cases/{case['case_id']}/adjudications",
        json={"expected_revision": 1, "decisions": [{"signature": [1, 1, 1], "replacement": "★"}]},
    )
    assert dup.status_code == 409
    detail = dup.json()["detail"]
    # 下标按"既有在前、追加在后"的合并顺序:追加项位于合并列表第 1 位
    assert detail[0]["decision"] == 1
    assert "重复" in detail[0]["reasons"][0]
    after = client.get(f"/cases/{case['case_id']}").json()
    assert after["revision"] == 1
    assert len(after["decisions"]) == 1
    assert after["result"]["consensus"] == "甲丙"


def test_append_to_missing_case_404(client):
    resp = client.post(
        "/cases/does-not-exist/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert resp.status_code == 404


def test_append_request_validation_422(client):
    case = _create(client)
    url = f"/cases/{case['case_id']}/adjudications"
    # 替换值非单字素簇
    assert (
        client.post(
            url,
            json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "replacement": "甲乙"}]},
        ).status_code
        == 422
    )
    # 缺少 expected_revision / 负数 / 空裁决列表
    assert client.post(url, json={"decisions": [{"signature": [1, 1, 1], "source": 0}]}).status_code == 422
    assert (
        client.post(
            url,
            json={"expected_revision": -1, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
        ).status_code
        == 422
    )
    assert client.post(url, json={"expected_revision": 0, "decisions": []}).status_code == 422
    # 422 不产生任何写入
    assert client.get(f"/cases/{case['case_id']}").json()["revision"] == 0


# ---------------------------------------------------------------- 持久性与并发

def test_case_survives_store_reopen(tmp_path, monkeypatch):
    """模拟服务重启:同一数据库文件重新打开后案件仍可查询、可继续追加。"""
    path = str(tmp_path / "cases.db")
    monkeypatch.setattr("app.main.store", CaseStore(path))
    client = TestClient(app)
    created = _create(client)
    case_id = created["case_id"]

    # “重启”:换成指向同一文件的新 CaseStore 实例
    monkeypatch.setattr("app.main.store", CaseStore(path))
    got = client.get(f"/cases/{case_id}")
    assert got.status_code == 200
    assert got.json() == created

    appended = client.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 0, "decisions": [{"signature": [1, 1, 1], "source": 2}]},
    )
    assert appended.status_code == 200
    assert appended.json()["revision"] == 1
    assert appended.json()["result"]["consensus"] == "甲乙"


def test_concurrent_appends_exactly_one_wins(tmp_path):
    """同一版本号并发追加:事务串行化保证只有一笔成功,revision 恰好加一。"""
    store = CaseStore(str(tmp_path / "cases.db"))
    seqs = [list(t) for t in TEXTS]
    result = adjudicate(reconcile_anchored(*seqs, []), [])
    case = store.create_case(texts=TEXTS, anchors=[], result=result)

    def recompute(texts, anchors, merged):
        choices = [(tuple(d["signature"]), "source", d["source"]) for d in merged]
        return adjudicate(reconcile_anchored(*[list(t) for t in texts], []), choices)

    decision = {"signature": [1, 1, 1], "source": 0}
    outcomes: list[str] = []

    def worker():
        try:
            store.append_adjudications(case["case_id"], 0, [decision], recompute)
            outcomes.append("ok")
        except RevisionConflict:
            outcomes.append("conflict")

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1
    assert outcomes.count("conflict") == 3
    final = store.get_case(case["case_id"])
    assert final["revision"] == 1
    assert len(final["decisions"]) == 1
    assert final["result"]["consensus"] == "甲丙"
