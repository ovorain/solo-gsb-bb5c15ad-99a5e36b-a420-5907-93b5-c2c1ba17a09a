"""公平性分析：按 principal 统计放行消耗与拒绝，计算加权 Jain 公平指数。"""
from __future__ import annotations

import math

from app.models import (
    EPS,
    ConstrainedPrincipal,
    FairnessReportRequest,
    FairnessReportResponse,
    FairnessWindowReport,
    PrincipalFairness,
    SimulateRequest,
    WindowRange,
)
from app.simulation import _round, simulate


def _weighted_jain(obtained: list[float], weights: list[float]) -> float:
    """加权 Jain 公平指数：J = (Σx)² / (Σw · Σ(x²/w))。

    各 principal 的获得量与权重严格成比例（x_i ∝ w_i）时取 1；权重全相等时
    退化为经典 Jain 指数。总获得量为 0（全员为零，完全相等）时按约定返回 1。
    """
    total = sum(obtained)
    if total <= EPS:
        return 1.0
    sum_w = sum(weights)
    denom = sum_w * sum(x * x / w for x, w in zip(obtained, weights))
    # 浮点毛刺钳回 [0, 1]
    return _round(min(1.0, max(0.0, (total * total) / denom)))


def fairness_report(payload: FairnessReportRequest) -> FairnessReportResponse:
    """与 /simulate 共享时间排序、连续补充与链式原子判定，按 principal 统计公平性。

    范围语义（全局与每个窗口各自独立套用）：
    - 实际获得量 = 范围内该 principal 放行请求的 cost 合计（每个请求计一次，
      与链长无关）；理论权重份额 = 范围内总放行消耗 × (自身权重 / 范围内权重和)；
    - 窗口统计只覆盖窗口内有请求的活跃 principal；全局覆盖全部 principal；
    - 最受限 principal = 理论份额存在缺口（shortfall > 0）者中达成率最低者，
      并列时取缺口较大者，再并列取 principal 名字典序较小者；其短缺证据为
      范围内首个被拒请求（处理顺序：时间戳升序、同刻按输入顺序）的限制桶。
    """
    requests = payload.requests
    # 与模拟引擎一致：窗口以最早请求时刻为原点
    t_start = min(request.timestamp for request in requests)
    t_end = max(request.timestamp for request in requests)
    width = float(payload.window_seconds)

    # 放行/拒绝判定完全复用 /simulate 引擎（结果按原始输入顺序返回）
    sim = simulate(SimulateRequest(buckets=payload.buckets, requests=requests))

    def window_index(timestamp: float) -> int:
        # 半开区间：边界上的请求属于右侧窗口；EPS 吸附浮点毛刺
        return int(math.floor((timestamp - t_start) / width + EPS))

    window_count = window_index(t_end) + 1

    # principal 按请求输入序首次出现排列；weight 一致性已由模型校验保证
    principals: list[str] = []
    weight_of: dict[str, float] = {}
    for request in requests:
        if request.principal not in weight_of:
            weight_of[request.principal] = float(request.weight)
            principals.append(request.principal)

    # 处理顺序：时间戳升序，同刻按输入顺序（与模拟引擎一致）
    order = sorted(
        range(len(requests)),
        key=lambda i: (requests[i].timestamp, i),
    )

    # 全局与逐窗口的 principal 统计；短缺证据 = 范围内首个被拒请求的限制桶
    allowed = {p: 0.0 for p in principals}
    rejected = {p: 0 for p in principals}
    first_rejection: dict[str, tuple[float, str, str]] = {}
    win_active = {p: [False] * window_count for p in principals}
    win_allowed = {p: [0.0] * window_count for p in principals}
    win_rejected = {p: [0] * window_count for p in principals}
    win_first_rejection: dict[str, list[tuple[float, str, str] | None]] = {
        p: [None] * window_count for p in principals
    }

    for i in order:
        request = requests[i]
        result = sim.results[i]
        principal = request.principal
        k = window_index(request.timestamp)
        win_active[principal][k] = True
        if result.allowed:
            allowed[principal] += float(request.cost)
            win_allowed[principal][k] += float(request.cost)
        else:
            rejected[principal] += 1
            win_rejected[principal][k] += 1
            evidence = (_round(request.timestamp), request.id, result.rejected_by)
            if principal not in first_rejection:
                first_rejection[principal] = evidence
            if win_first_rejection[principal][k] is None:
                win_first_rejection[principal][k] = evidence

    def build_scope(
        scope_principals: list[str],
        allowed_of,
        rejected_of,
        first_rejection_of,
    ) -> tuple[list[PrincipalFairness], float, ConstrainedPrincipal | None]:
        """组装某一范围（全局/单窗口）的逐 principal 统计、Jain 指数与最受限者。"""
        total = sum(allowed_of(p) for p in scope_principals)
        sum_w = sum(weight_of[p] for p in scope_principals)

        # 原始（未舍入）值用于判定与排序，输出统一 _round
        expected: dict[str, float] = {}
        shortfall: dict[str, float] = {}
        ratio: dict[str, float | None] = {}
        for p in scope_principals:
            expected[p] = total * weight_of[p] / sum_w
            shortfall[p] = max(0.0, expected[p] - allowed_of(p))
            ratio[p] = allowed_of(p) / expected[p] if expected[p] > EPS else None

        stats = [
            PrincipalFairness(
                principal=p,
                weight=_round(weight_of[p]),
                allowed_consumption=_round(allowed_of(p)),
                rejected_count=rejected_of(p),
                expected_share=_round(expected[p]),
                attainment_ratio=_round(ratio[p]) if ratio[p] is not None else None,
                shortfall=_round(shortfall[p]),
            )
            for p in scope_principals
        ]
        jain = _weighted_jain(
            [allowed_of(p) for p in scope_principals],
            [weight_of[p] for p in scope_principals],
        )

        constrained: ConstrainedPrincipal | None = None
        short = [p for p in scope_principals if shortfall[p] > EPS]
        if short:
            # 达成率最低者最受限；并列取缺口较大者，再并列取名字字典序较小者
            worst = min(short, key=lambda p: (ratio[p], -shortfall[p], p))
            evidence = first_rejection_of(worst)
            constrained = ConstrainedPrincipal(
                principal=worst,
                attainment_ratio=_round(ratio[worst]),
                shortfall=_round(shortfall[worst]),
                first_shortage_bucket=evidence[2] if evidence else None,
                first_shortage_request_id=evidence[1] if evidence else None,
                first_shortage_timestamp=evidence[0] if evidence else None,
            )
        return stats, jain, constrained

    global_stats, global_jain, global_constrained = build_scope(
        principals,
        lambda p: allowed[p],
        lambda p: rejected[p],
        lambda p: first_rejection.get(p),
    )

    windows = [
        WindowRange(
            index=k,
            start=_round(t_start + k * width),
            end=_round(t_start + (k + 1) * width),
        )
        for k in range(window_count)
    ]

    window_reports: list[FairnessWindowReport] = []
    for k in range(window_count):
        active = [p for p in principals if win_active[p][k]]
        stats, jain, constrained = build_scope(
            active,
            lambda p, k=k: win_allowed[p][k],
            lambda p, k=k: win_rejected[p][k],
            lambda p, k=k: win_first_rejection[p][k],
        )
        window_reports.append(
            FairnessWindowReport(
                index=k,
                start=windows[k].start,
                end=windows[k].end,
                jain_index=jain,
                principals=stats,
                most_constrained=constrained,
            )
        )

    return FairnessReportResponse(
        window_seconds=_round(width),
        window_count=window_count,
        windows=windows,
        jain_index=global_jain,
        principals=global_stats,
        most_constrained=global_constrained,
        window_reports=window_reports,
    )
