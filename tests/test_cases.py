"""持久化核对案件的测试:创建、查询、追加裁决、版本冲突与重启持久性。"""
from __future__ import annotations

import unicodedata

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立的 SQLite 库文件。"""
    monkeypatch.setenv("CASES_DB_PATH", str(tmp_path / "cases.db"))
    return TestClient(app)


def _create(client, texts=("甲丙", "甲丁", "甲乙"), anchors=None):
    payload = {"texts": list(texts)}
    if anchors is not None:
        payload["anchors"] = anchors
    resp = client.post("/cases", json=payload)
    assert resp.status_code == 201
    return resp.json()


# ---------------------------------------------------------------- 创建与查询


def test_create_and_get_roundtrip(client):
    created = _create(client)
    assert created["revision"] == 1
    assert created["texts"] == ["甲丙", "甲丁", "甲乙"]
    assert created["anchors"] == []
    assert created["decisions"] == []
    assert created["result"]["consensus"] == "甲�"
    assert created["result"]["audits"] == []
    assert len(created["result"]["uncertain"]) == 1

    got = client.get(f"/cases/{created['case_id']}")
    assert got.status_code == 200
    assert got.json() == created


def test_create_with_anchors(client):
    created = _create(
        client, texts=("甲乙", "甲丙", "甲乙"), anchors=[{"positions": [1, 1, 1]}]
    )
    assert created["anchors"] == [{"positions": [1, 1, 1]}]
    assert created["result"]["consensus"] == "甲乙"
    assert created["result"]["columns"][1]["anchored"] is True


def test_create_response_shape(client):
    created = _create(client)
    assert set(created) == {"case_id", "revision", "texts", "anchors", "decisions", "result"}
    assert set(created["result"]) == {"consensus", "total_cost", "columns", "uncertain", "audits"}


def test_create_validation_errors_are_422(client):
    # 文本数量、字素簇上限与锚点规则沿用无状态接口的校验
    assert client.post("/cases", json={"texts": ["a", "b"]}).status_code == 422
    assert client.post("/cases", json={"texts": ["字" * 121, "", ""]}).status_code == 422
    assert (
        client.post(
            "/cases",
            json={"texts": ["甲乙", "甲乙", "甲乙"], "anchors": [{"positions": [2, 0, 0]}]},
        ).status_code
        == 422
    )


def test_get_unknown_case_returns_404(client):
    assert client.get("/cases/no-such-case").status_code == 404


# ---------------------------------------------------------------- 追加裁决


def test_append_happy_path_bumps_revision(client):
    case_id = _create(client)["case_id"]
    resp = client.post(
        f"/cases/{case_id}/adjudications",
        json={
            "expected_revision": 1,
            "decisions": [{"signature": [1, 1, 1], "source": 0}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["revision"] == 2
    assert body["result"]["consensus"] == "甲丙"
    assert body["result"]["uncertain"] == []
    assert body["result"]["audits"] == [
        {
            "signature": [1, 1, 1],
            "original": ["丙", "丁", "乙"],
            "adopted": "丙",
            "method": "source:0",
        }
    ]
    assert body["decisions"] == [
        {"signature": [1, 1, 1], "source": 0, "replacement": None}
    ]
    # 查询接口返回同一状态
    assert client.get(f"/cases/{case_id}").json() == body


def test_append_merges_with_existing_decisions(client):
    case_id = _create(client, texts=("甲丙乙", "甲丁戊", "甲己亥"))["case_id"]

    r1 = client.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 1, "decisions": [{"signature": [1, 1, 1], "source": 1}]},
    )
    assert r1.status_code == 200
    assert r1.json()["revision"] == 2
    assert r1.json()["result"]["consensus"] == "甲丁�"

    r2 = client.post(
        f"/cases/{case_id}/adjudications",
        json={
            "expected_revision": 2,
            "decisions": [{"signature": [2, 2, 2], "replacement": "★"}],
        },
    )
    assert r2.status_code == 200
    body = r2.json()
    assert body["revision"] == 3
    assert body["result"]["consensus"] == "甲丁★"
    assert body["result"]["uncertain"] == []
    # 既有裁决与新增裁决合并:审计项与裁决列表均包含两条
    assert [a["method"] for a in body["result"]["audits"]] == ["source:1", "replacement"]
    assert [d["signature"] for d in body["decisions"]] == [[1, 1, 1], [2, 2, 2]]


def test_append_replacement_is_nfc_normalized(client):
    case_id = _create(client)["case_id"]
    nfd = unicodedata.normalize("NFD", "é")
    resp = client.post(
        f"/cases/{case_id}/adjudications",
        json={
            "expected_revision": 1,
            "decisions": [{"signature": [1, 1, 1], "replacement": nfd}],
        },
    )
    assert resp.status_code == 200
    nfc = unicodedata.normalize("NFC", "é")
    assert resp.json()["decisions"][0]["replacement"] == nfc
    assert resp.json()["result"]["audits"][0]["adopted"] == nfc


def test_append_stale_revision_returns_409_without_write(client):
    case_id = _create(client)["case_id"]
    ok = client.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 1, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert ok.status_code == 200

    for stale in (1, 3):  # 落后与超前都不接受
        resp = client.post(
            f"/cases/{case_id}/adjudications",
            json={"expected_revision": stale, "decisions": []},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["expected_revision"] == stale
        assert detail["current_revision"] == 2

    # 版本冲突未产生任何写入
    got = client.get(f"/cases/{case_id}").json()
    assert got["revision"] == 2
    assert len(got["decisions"]) == 1
    assert got["result"]["consensus"] == "甲丙"


def test_append_invalid_decision_returns_409_without_write(client):
    case_id = _create(client)["case_id"]
    resp = client.post(
        f"/cases/{case_id}/adjudications",
        json={
            "expected_revision": 1,
            "decisions": [
                {"signature": [1, 1, 1], "source": 0},   # 合法
                {"signature": [0, 0, 0], "source": 0},   # 非 uncertain 列
                {"signature": [9, 9, 9], "source": 0},   # 列不存在
            ],
        },
    )
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert [item["decision"] for item in detail] == [1, 2]
    assert "uncertain" in detail[0]["reasons"][0]
    assert "不存在" in detail[1]["reasons"][0]

    # 任一裁决失效即整体拒绝:合法的第一项也未写入,revision 不变
    got = client.get(f"/cases/{case_id}").json()
    assert got["revision"] == 1
    assert got["decisions"] == []
    assert got["result"]["consensus"] == "甲�"


def test_append_duplicating_existing_decision_returns_409(client):
    case_id = _create(client)["case_id"]
    ok = client.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 1, "decisions": [{"signature": [1, 1, 1], "source": 0}]},
    )
    assert ok.status_code == 200

    # 与既有裁决合并校验:同一签名不得再次裁决
    resp = client.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 2, "decisions": [{"signature": [1, 1, 1], "source": 1}]},
    )
    assert resp.status_code == 409
    assert "重复" in resp.json()["detail"][0]["reasons"][0]
    assert client.get(f"/cases/{case_id}").json()["revision"] == 2


def test_append_unknown_case_returns_404(client):
    resp = client.post(
        "/cases/no-such-case/adjudications",
        json={"expected_revision": 1, "decisions": []},
    )
    assert resp.status_code == 404


def test_append_validation_errors_are_422(client):
    case_id = _create(client)["case_id"]
    url = f"/cases/{case_id}/adjudications"

    # 缺少 expected_revision / 版本号小于 1
    assert client.post(url, json={"decisions": []}).status_code == 422
    assert client.post(url, json={"expected_revision": 0}).status_code == 422
    # 替换值非单字素簇、source 与 replacement 同时给出
    assert (
        client.post(
            url,
            json={
                "expected_revision": 1,
                "decisions": [{"signature": [1, 1, 1], "replacement": "甲乙"}],
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            url,
            json={
                "expected_revision": 1,
                "decisions": [{"signature": [1, 1, 1], "source": 0, "replacement": "X"}],
            },
        ).status_code
        == 422
    )
    # 422 不触碰库中状态
    assert client.get(f"/cases/{case_id}").json()["revision"] == 1


# ---------------------------------------------------------------- 持久性


def test_case_survives_restart(client, tmp_path):
    case_id = _create(client)["case_id"]
    resp = client.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 1, "decisions": [{"signature": [1, 1, 1], "source": 2}]},
    )
    assert resp.status_code == 200

    # 模拟服务重启:同一库文件、全新客户端实例
    assert (tmp_path / "cases.db").exists()
    restarted = TestClient(app)
    got = restarted.get(f"/cases/{case_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["revision"] == 2
    assert body["result"]["consensus"] == "甲乙"
    assert body["decisions"] == [
        {"signature": [1, 1, 1], "source": 2, "replacement": None}
    ]
    # 重启后仍可继续追加
    again = restarted.post(
        f"/cases/{case_id}/adjudications",
        json={"expected_revision": 2, "decisions": []},
    )
    assert again.status_code == 200
    assert again.json()["revision"] == 3
