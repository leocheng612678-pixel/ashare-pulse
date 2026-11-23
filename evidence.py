from __future__ import annotations

import math
import statistics
from typing import Any


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _stats(returns: list[float]) -> dict[str, Any]:
    if not returns:
        return {
            "count": 0,
            "positive_probability": None,
            "median_return": None,
            "mean_return": None,
            "worst_return": None,
            "best_return": None,
        }
    return {
        "count": len(returns),
        "positive_probability": sum(item > 0 for item in returns)
        / len(returns)
        * 100,
        "median_return": statistics.median(returns),
        "mean_return": statistics.fmean(returns),
        "worst_return": min(returns),
        "best_return": max(returns),
    }


def _score_rows(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    technicals = snapshot.get("technicals") or {}
    csi = snapshot.get("historical_csi") or technicals.get("csi_full_history") or []
    flows = {
        row["date"]: _number(row.get("main"))
        for row in ((snapshot.get("capital") or {}).get("history") or [])
    }
    close_values = [_number(row.get("close")) for row in csi]
    rows: list[dict[str, Any]] = []

    for index in range(60, len(csi) - 20):
        closes = close_values[: index + 1]
        current = closes[-1]
        ma20 = sum(closes[-20:]) / 20
        ma60 = sum(closes[-60:]) / 60
        if not current or not ma20 or not ma60:
            continue
        ret5 = (
            (current / closes[-6] - 1) * 100 if len(closes) > 5 else None
        )
        trend = (
            _clip((current / ma20 - 1) / 0.03) * 0.6
            + _clip((ret5 or 0) / 4.0) * 0.2
            + (1.0 if ma20 > ma60 else -1.0) * 0.2
        )
        day = csi[index]["date"]
        prior_days = [row["date"] for row in csi[: index + 1]][-5:]
        flow_values = [flows.get(item) for item in prior_days]
        if any(value is None for value in flow_values):
            continue
        flow5 = sum(flow_values)
        future5 = (close_values[index + 5] / current - 1) * 100
        future20 = (close_values[index + 20] / current - 1) * 100
        rows.append(
            {
                "date": day,
                "core_score": 50 + 30 * trend,
                "trend_score": trend,
                "flow5": flow5,
                "forward_5d": future5,
                "forward_20d": future20,
            }
        )
    for row_index, row in enumerate(rows):
        prior = [item["flow5"] for item in rows[max(0, row_index - 60) : row_index]]
        if len(prior) < 20:
            row["core_score"] = 50 + 30 * row["trend_score"]
            row["flow5_z"] = None
            continue
        mean = statistics.fmean(prior)
        deviation = statistics.pstdev(prior)
        z_score = (row["flow5"] - mean) / deviation if deviation else 0.0
        flow_score = _clip(z_score / 1.5)
        row["flow5_z"] = z_score
        row["core_score"] = 50 + 30 * row["trend_score"] + 20 * flow_score
    return rows


def build_historical_validation(
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    rows = _score_rows(snapshot)
    if not rows:
        return {
            "available": False,
            "summary": "历史数据不足，暂不能做相似状态验证。",
            "similar": _stats([]),
            "all": _stats([]),
        }
    current = rows[-1]
    similar = [
        row for row in rows if abs(row["core_score"] - current["core_score"]) <= 12
    ]
    buckets = [
        ("0-30", 0, 30),
        ("30-45", 30, 45),
        ("45-55", 45, 55),
        ("55-70", 55, 70),
        ("70-100", 70, 100),
    ]
    bucket_stats = []
    for label, low, high in buckets:
        selected = [
            row for row in rows if low <= row["core_score"] < high
        ]
        ret20 = [row["forward_20d"] for row in selected]
        bucket_stats.append(
            {
                "label": label,
                "sample_count": len(selected),
                "positive_probability_20d": (
                    sum(item > 0 for item in ret20) / len(ret20) * 100
                    if ret20
                    else None
                ),
                "median_return_20d": (
                    statistics.median(ret20) if ret20 else None
                ),
            }
        )
    all_stats_5 = _stats([row["forward_5d"] for row in rows])
    all_stats_20 = _stats([row["forward_20d"] for row in rows])
    similar_5 = _stats([row["forward_5d"] for row in similar])
    similar_20 = _stats([row["forward_20d"] for row in similar])
    confidence = (
        "较高"
        if len(similar) >= 40
        else "中等" if len(similar) >= 20 else "较低"
    )
    return {
        "available": True,
        "as_of": current["date"],
        "methodology": (
            "使用历史日线可得的沪深300趋势、5日主力资金相对过去60日的"
            "标准化强度和后续5/20日收益；样本按当前核心评分±12分分组，"
            "不使用未来数据选样。"
        ),
        "limitations": (
            "该验证未包含ARMS、期指基差、基本面和交易成本，样本来自单一"
            "历史市场阶段，不应视为概率保证。"
        ),
        "current_core_score": current["core_score"],
        "current_trend_score": current["trend_score"],
        "current_flow5_z": current.get("flow5_z"),
        "confidence": confidence,
        "similar": {
            "count": len(similar),
            "forward_5d": similar_5,
            "forward_20d": similar_20,
        },
        "all_history": {
            "count": len(rows),
            "forward_5d": all_stats_5,
            "forward_20d": all_stats_20,
        },
        "buckets": bucket_stats,
    }


def build_evidence(
    snapshot: dict[str, Any],
    strategy: dict[str, Any],
    validation: dict[str, Any],
) -> dict[str, Any]:
    sources = snapshot.get("sources") or {}
    ok_count = sum(1 for item in sources.values() if item.get("status") == "ok")
    cached_count = sum(
        1 for item in sources.values() if item.get("status") == "cached"
    )
    error_count = sum(
        1 for item in sources.values() if item.get("status") == "error"
    )
    breadth = snapshot.get("breadth") or {}
    coverage = _number(breadth.get("coverage"))
    expected = _number(breadth.get("expected_total"), coverage)
    coverage_ratio = coverage / expected * 100 if expected else None
    missing_factors = [
        item["name"]
        for item in strategy.get("factors") or []
        if item.get("status") == "neutral" and "缺失" in item.get("detail", "")
    ]
    quality = 100 - error_count * 15 - cached_count * 5
    if missing_factors:
        quality -= len(missing_factors) * 8
    if coverage_ratio is not None and coverage_ratio < 95:
        quality -= 15
    if snapshot.get("trade_date") and strategy.get("metrics", {}).get(
        "trade_age_days", 0
    ) > 5:
        quality -= 20
    quality = max(0, min(100, quality))

    checks = [
        {
            "name": "数据完整性",
            "status": "good" if coverage_ratio and coverage_ratio >= 95 else "warn",
            "detail": (
                f"全市场覆盖 {coverage:.0f}/{expected:.0f} 只"
                f"（{coverage_ratio:.1f}%）"
                if coverage_ratio is not None
                else "涨跌家数覆盖不可用"
            ),
        },
        {
            "name": "多源稳定性",
            "status": "good" if error_count == 0 else "warn",
            "detail": f"正常 {ok_count}，缓存 {cached_count}，异常 {error_count}",
        },
        {
            "name": "模型因子",
            "status": "good" if not missing_factors else "warn",
            "detail": (
                "四维因子齐全"
                if not missing_factors
                else "缺失：" + "、".join(missing_factors)
            ),
        },
        {
            "name": "历史验证",
            "status": "good" if validation.get("available") else "warn",
            "detail": (
                f"相似样本 {validation.get('similar', {}).get('count', 0)} 次，"
                f"置信度 {validation.get('confidence', '--')}"
                if validation.get("available")
                else "历史样本不足"
            ),
        },
    ]
    return {
        "quality_score": quality,
        "checks": checks,
        "missing_factors": missing_factors,
        "source_counts": {
            "ok": ok_count,
            "cached": cached_count,
            "error": error_count,
        },
        "labels": {
            "main_force": "订单大小推断口径，不等同于真实机构持仓",
            "arms": "仅使用当日全A成交量结构",
            "futures": "主力连续近似口径，具体交易需核对合约和到期日",
            "stock_fundamentals": (
                "当前主动选股主要使用行情估值和量价数据，尚未覆盖完整财报质量"
            ),
        },
    }
