"""公平性报告（/fairness-report）引擎与 HTTP 层测试：pytest 运行。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.errors import RequestValidationErrors
from app.main import app
from app.models import FairnessReportRequest, SimulateRequest
from app.fairness import fairness_report
from app.simulation import simulate

client = TestClient(app)


def _run(buckets, requests, window_seconds):
    return fairness_report(
        FairnessReportRequest.model_validate(
            {"buckets": buckets, "requests": requests, "window_seconds": window_seconds}
        )
    )


def _principal(stats, name):
    return next(p for p in stats if p.principal == name)


def test_global_stats_and_weighted_jain():
    # alice 两笔放行共 8，bob 一笔放行 4、一笔被 api_qps 拒绝
    resp = _run(
        [
            {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
            {"id": "api_qps", "capacity": 5, "refill_rate": 5},
        ],
        [
            {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
            {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "bob", "weight": 1},
            {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
            {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"], "principal": "bob", "weight": 1},
        ],
        window_seconds=2,
    )
    assert resp.window_seconds == 2
    assert resp.window_count == 2
    assert [w.model_dump() for w in resp.windows] == [
        {"index": 0, "start": 0.0, "end": 2.0},
        {"index": 1, "start": 2.0, "end": 4.0},
    ]

    alice = _principal(resp.principals, "alice")
    bob = _principal(resp.principals, "bob")
    assert alice.allowed_consumption == 8 and alice.rejected_count == 0
    assert bob.allowed_consumption == 4 and bob.rejected_count == 1
    # 等权重：理论份额各为总放行 12 的一半
    assert alice.expected_share == 6 and bob.expected_share == 6
    assert alice.attainment_ratio == pytest.approx(8 / 6)
    assert bob.attainment_ratio == pytest.approx(4 / 6)
    assert alice.shortfall == 0 and bob.shortfall == 2
    # 加权 Jain = 12^2 / (2 * (8^2 + 4^2)) = 0.9
    assert resp.jain_index == pytest.approx(0.9)

    # 最受限：bob；首个造成其短缺的桶为 api_qps（req-2 的限制桶）
    mc = resp.most_constrained
    assert mc.principal == "bob"
    assert mc.attainment_ratio == pytest.approx(4 / 6)
    assert mc.shortfall == 2
    assert mc.first_shortage_bucket == "api_qps"
    assert mc.first_shortage_request_id == "req-2"
    assert mc.first_shortage_timestamp == 0.5


def test_per_window_alignment_with_theoretical_share():
    resp = _run(
        [
            {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
            {"id": "api_qps", "capacity": 5, "refill_rate": 5},
        ],
        [
            {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
            {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "bob", "weight": 1},
            {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
            {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"], "principal": "bob", "weight": 1},
        ],
        window_seconds=2,
    )
    assert len(resp.window_reports) == 2

    # 窗口 0 [0,2)：alice 获 8、bob 获 0（理论份额各 4）-> Jain = 0.5
    w0 = resp.window_reports[0]
    assert (w0.index, w0.start, w0.end) == (0, 0.0, 2.0)
    assert w0.jain_index == pytest.approx(0.5)
    assert _principal(w0.principals, "alice").attainment_ratio == 2
    bob0 = _principal(w0.principals, "bob")
    assert bob0.allowed_consumption == 0 and bob0.rejected_count == 1
    assert bob0.expected_share == 4 and bob0.shortfall == 4
    assert w0.most_constrained.principal == "bob"
    assert w0.most_constrained.first_shortage_bucket == "api_qps"

    # 窗口 1 [2,4)：仅 bob 活跃且足额获得 -> Jain = 1，无人受限
    w1 = resp.window_reports[1]
    assert (w1.index, w1.start, w1.end) == (1, 2.0, 4.0)
    assert w1.jain_index == 1
    assert [p.principal for p in w1.principals] == ["bob"]
    assert w1.most_constrained is None


def test_weighted_share_proportional_allocation_is_perfectly_fair():
    # alice 权重 2 获得 8，bob 权重 1 获得 4：获得量与权重严格成比例 -> Jain = 1
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "alice", "weight": 2},
            {"id": "r2", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "alice", "weight": 2},
            {"id": "r3", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "bob", "weight": 1},
        ],
        window_seconds=10,
    )
    assert resp.jain_index == 1
    alice = _principal(resp.principals, "alice")
    bob = _principal(resp.principals, "bob")
    # 理论份额按权重 2:1 切分总放行 12
    assert alice.expected_share == 8 and bob.expected_share == 4
    assert alice.attainment_ratio == 1 and bob.attainment_ratio == 1
    # 无人存在理论缺口 -> 无最受限者
    assert resp.most_constrained is None


def test_zero_granted_window_jain_is_one_and_no_constrained():
    # 全部请求被拒：总放行 0，理论份额无法定义 -> Jain 约定为 1，达成率为 null
    resp = _run(
        [{"id": "A", "capacity": 1, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 4, "chain": ["A"], "principal": "alice", "weight": 1},
            {"id": "r2", "timestamp": 1, "cost": 4, "chain": ["A"], "principal": "bob", "weight": 1},
        ],
        window_seconds=5,
    )
    assert resp.jain_index == 1
    assert resp.most_constrained is None
    for p in resp.principals:
        assert p.allowed_consumption == 0
        assert p.expected_share == 0
        assert p.attainment_ratio is None
        assert p.shortfall == 0
    w0 = resp.window_reports[0]
    assert w0.jain_index == 1
    assert w0.most_constrained is None


def test_constrained_without_rejection_reports_null_bucket():
    # carol 权重 3 但只请求 2，dave 权重 1 获得 6：carol 相对理论份额短缺，
    # 但她从未被拒 -> 短缺证据字段为 null
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 2, "chain": ["A"], "principal": "carol", "weight": 3},
            {"id": "r2", "timestamp": 0, "cost": 6, "chain": ["A"], "principal": "dave", "weight": 1},
        ],
        window_seconds=10,
    )
    mc = resp.most_constrained
    # 总放行 8：carol 理论份额 6 实得 2，dave 理论份额 2 实得 6
    assert mc.principal == "carol"
    assert mc.attainment_ratio == pytest.approx(2 / 6)
    assert mc.shortfall == 4
    assert mc.first_shortage_bucket is None
    assert mc.first_shortage_request_id is None
    assert mc.first_shortage_timestamp is None


def test_half_open_boundary_request_belongs_to_right_window():
    # r2 恰好落在边界 t=2：计入右侧窗口 [2, 4)
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 0, "cost": 5, "chain": ["A"], "principal": "alice", "weight": 1},
            {"id": "r2", "timestamp": 2, "cost": 5, "chain": ["A"], "principal": "alice", "weight": 1},
        ],
        window_seconds=2,
    )
    assert resp.window_count == 2
    w0, w1 = resp.window_reports
    assert _principal(w0.principals, "alice").allowed_consumption == 5
    assert _principal(w1.principals, "alice").allowed_consumption == 5


def test_window_reports_cover_inactive_principals_by_omission():
    # bob 只在窗口 1 活跃：窗口 0 的 principals 不包含 bob
    resp = _run(
        [{"id": "A", "capacity": 100, "refill_rate": 0}],
        [
            {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "alice", "weight": 1},
            {"id": "r2", "timestamp": 5, "cost": 1, "chain": ["A"], "principal": "bob", "weight": 1},
        ],
        window_seconds=3,
    )
    assert [p.principal for p in resp.window_reports[0].principals] == ["alice"]
    assert [p.principal for p in resp.window_reports[1].principals] == ["bob"]
    # 全局仍覆盖两者
    assert [p.principal for p in resp.principals] == ["alice", "bob"]


def test_first_shortage_bucket_follows_processing_order():
    # 输入乱序：eve 的短缺证据必须按处理顺序（时间戳升序）取首个被拒请求
    resp = _run(
        [
            {"id": "A", "capacity": 2, "refill_rate": 0},
            {"id": "B", "capacity": 2, "refill_rate": 0},
        ],
        [
            {"id": "late", "timestamp": 5, "cost": 1, "chain": ["B"], "principal": "eve", "weight": 1},
            {"id": "fill", "timestamp": 0, "cost": 2, "chain": ["A", "B"], "principal": "max", "weight": 1},
            {"id": "early", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "eve", "weight": 1},
        ],
        window_seconds=10,
    )
    # 处理顺序：fill(t=0 放行，A/B 各扣 2) -> early(t=1，A 不足被拒) -> late(t=5，B 不足被拒)
    eve = _principal(resp.principals, "eve")
    assert eve.rejected_count == 2
    mc = resp.most_constrained
    assert mc.principal == "eve"
    # 首个被拒的是 early（t=1），限制桶为 A
    assert mc.first_shortage_bucket == "A"
    assert mc.first_shortage_request_id == "early"
    assert mc.first_shortage_timestamp == 1


def test_consumption_counted_once_per_request_regardless_of_chain_length():
    # 放行消耗按请求计一次 cost，与链上桶数无关
    resp = _run(
        [
            {"id": "A", "capacity": 10, "refill_rate": 0},
            {"id": "B", "capacity": 10, "refill_rate": 0},
        ],
        [{"id": "r1", "timestamp": 0, "cost": 3, "chain": ["A", "B"], "principal": "alice", "weight": 1}],
        window_seconds=5,
    )
    assert _principal(resp.principals, "alice").allowed_consumption == 3


def test_consistent_with_simulate_verdicts():
    # 与 /simulate 完全一致的放行/拒绝序列：逐 principal 合计必须等于模拟结果
    buckets = [
        {"id": "A", "capacity": 10, "refill_rate": 1},
        {"id": "B", "capacity": 5, "refill_rate": 0.5},
    ]
    requests = [
        {
            "id": f"r{i}",
            "timestamp": t,
            "cost": 3,
            "chain": ["A", "B"],
            "principal": "alice" if i % 2 == 0 else "bob",
            "weight": 1,
        }
        for i, t in enumerate([0, 0, 1, 2, 5, 9, 9, 12])
    ]
    report = _run(buckets, requests, window_seconds=4)
    # SimulateRequest 禁止多余字段：剥离 principal/weight 后回放同一序列
    sim_requests = [
        {k: v for k, v in req.items() if k not in ("principal", "weight")} for req in requests
    ]
    sim = simulate(SimulateRequest.model_validate({"buckets": buckets, "requests": sim_requests}))

    for name in ("alice", "bob"):
        allowed_count = sum(
            1 for r, req in zip(sim.results, requests) if r.allowed and req["principal"] == name
        )
        rejected_count = sum(
            1 for r, req in zip(sim.results, requests) if not r.allowed and req["principal"] == name
        )
        stats = _principal(report.principals, name)
        assert stats.allowed_consumption == 3 * allowed_count
        assert stats.rejected_count == rejected_count
    # 逐窗口合计 = 全局合计
    assert sum(p.allowed_consumption for w in report.window_reports for p in w.principals) == sum(
        p.allowed_consumption for p in report.principals
    )


def test_large_timestamps_windowed_by_elapsed_time():
    # 任意时间原点：窗口以最早请求时刻为原点
    resp = _run(
        [{"id": "A", "capacity": 10, "refill_rate": 1}],
        [
            {"id": "r1", "timestamp": 1_000_000, "cost": 10, "chain": ["A"], "principal": "alice", "weight": 1},
            {"id": "r2", "timestamp": 1_000_002, "cost": 2, "chain": ["A"], "principal": "alice", "weight": 1},
        ],
        window_seconds=2,
    )
    assert resp.windows[0].start == 1_000_000
    assert resp.windows[0].end == 1_000_002
    assert _principal(resp.window_reports[0].principals, "alice").allowed_consumption == 10
    assert _principal(resp.window_reports[1].principals, "alice").allowed_consumption == 2


# ---------- 校验 ----------

def test_weight_must_be_positive_finite_number():
    for bad in (0, -1, "1", None, True, float("inf"), float("nan")):
        with pytest.raises(Exception):
            FairnessReportRequest.model_validate(
                {
                    "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                    "requests": [
                        {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": bad}
                    ],
                    "window_seconds": 1,
                }
            )


def test_principal_must_be_non_empty():
    for bad in ("", 1, None):
        with pytest.raises(Exception):
            FairnessReportRequest.model_validate(
                {
                    "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                    "requests": [
                        {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": bad, "weight": 1}
                    ],
                    "window_seconds": 1,
                }
            )


def test_principal_and_weight_are_required():
    with pytest.raises(Exception):
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "weight": 1}],
                "window_seconds": 1,
            }
        )
    with pytest.raises(Exception):
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [{"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p"}],
                "window_seconds": 1,
            }
        )


def test_inconsistent_weight_rejected():
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
                "requests": [
                    {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "alice", "weight": 2},
                    {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "alice", "weight": 3},
                ],
                "window_seconds": 1,
            }
        )
    assert any(e["type"] == "inconsistent_weight" for e in exc.value.errors)
    assert exc.value.errors[0]["loc"] == ("requests", 1, "weight")


def test_shared_validation_still_applies():
    # 未知桶引用
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["GHOST"], "principal": "p", "weight": 1}
                ],
                "window_seconds": 1,
            }
        )
    assert any(e["type"] == "unknown_bucket_ref" for e in exc.value.errors)

    # 重复请求 ID
    with pytest.raises(RequestValidationErrors) as exc:
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1},
                    {"id": "r", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1},
                ],
                "window_seconds": 1,
            }
        )
    assert any(e["type"] == "duplicate_request" for e in exc.value.errors)

    # window_seconds 必须为正
    with pytest.raises(Exception):
        FairnessReportRequest.model_validate(
            {
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1}
                ],
                "window_seconds": 0,
            }
        )


# ---------- HTTP ----------

def test_http_fairness_report_ok():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [
                {"id": "tenant_quota", "capacity": 100, "refill_rate": 10, "initial": 100},
                {"id": "api_qps", "capacity": 5, "refill_rate": 5},
            ],
            "requests": [
                {"id": "req-1", "timestamp": 0, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
                {"id": "req-2", "timestamp": 0.5, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "bob", "weight": 1},
                {"id": "req-3", "timestamp": 1, "cost": 4, "chain": ["tenant_quota", "api_qps"], "principal": "alice", "weight": 1},
                {"id": "req-4", "timestamp": 3, "cost": 4, "chain": ["tenant_quota"], "principal": "bob", "weight": 1},
            ],
            "window_seconds": 2,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["window_seconds"] == 2
    assert data["window_count"] == 2
    assert data["jain_index"] == pytest.approx(0.9)
    assert [p["principal"] for p in data["principals"]] == ["alice", "bob"]
    assert data["most_constrained"]["principal"] == "bob"
    assert data["most_constrained"]["first_shortage_bucket"] == "api_qps"
    assert len(data["window_reports"]) == 2
    assert data["window_reports"][0]["jain_index"] == pytest.approx(0.5)
    assert data["window_reports"][1]["most_constrained"] is None


def test_http_invalid_weight_is_422():
    for bad in (0, -2, "1", None, True):
        response = client.post(
            "/fairness-report",
            json={
                "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
                "requests": [
                    {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": bad}
                ],
                "window_seconds": 1,
            },
        )
        assert response.status_code == 422, bad
        detail = response.json()["detail"]
        assert isinstance(detail, list) and detail


def test_http_inconsistent_weight_is_422():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 10, "refill_rate": 0}],
            "requests": [
                {"id": "r1", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "alice", "weight": 2},
                {"id": "r2", "timestamp": 1, "cost": 1, "chain": ["A"], "principal": "alice", "weight": 3},
            ],
            "window_seconds": 1,
        },
    )
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert any(e["type"] == "inconsistent_weight" for e in detail)


def test_http_missing_window_seconds_is_422():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1}
            ],
        },
    )
    assert response.status_code == 422


def test_http_extra_field_rejected():
    response = client.post(
        "/fairness-report",
        json={
            "buckets": [{"id": "A", "capacity": 1, "refill_rate": 0}],
            "requests": [
                {"id": "r", "timestamp": 0, "cost": 1, "chain": ["A"], "principal": "p", "weight": 1}
            ],
            "window_seconds": 1,
            "bogus": 1,
        },
    )
    assert response.status_code == 422


def test_http_root_lists_fairness_report():
    assert client.get("/").json()["fairness_report"] == "/fairness-report"
