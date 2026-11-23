from __future__ import annotations

import math
from collections import Counter
from typing import Any


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _correlation(left: list[float], right: list[float]) -> float | None:
    length = min(len(left), len(right))
    if length < 10:
        return None
    x = left[-length:]
    y = right[-length:]
    mean_x = sum(x) / length
    mean_y = sum(y) / length
    covariance = sum(
        (a - mean_x) * (b - mean_y) for a, b in zip(x, y)
    )
    variance_x = sum((a - mean_x) ** 2 for a in x)
    variance_y = sum((b - mean_y) ** 2 for b in y)
    if variance_x <= 0 or variance_y <= 0:
        return None
    return covariance / math.sqrt(variance_x * variance_y)


def candidate_portfolio_risk(
    stocks: list[dict[str, Any]], top_n: int = 6
) -> dict[str, Any]:
    selected = stocks[:top_n]
    if not selected:
        return {
            "available": False,
            "status": "无候选",
            "reason": "没有可评估的股票组合",
        }
    returns = [
        [
            _number(value)
            for value in stock.get("recent_returns_30d") or []
        ]
        for stock in selected
    ]
    pairs = []
    correlations = []
    for left_index in range(len(selected)):
        for right_index in range(left_index + 1, len(selected)):
            value = _correlation(
                returns[left_index],
                returns[right_index],
            )
            if value is None:
                continue
            correlations.append(value)
            pairs.append(
                {
                    "left": selected[left_index].get("name"),
                    "right": selected[right_index].get("name"),
                    "correlation": value,
                }
            )

    industries = []
    for stock in selected:
        company = (
            (stock.get("company_fundamentals") or {}).get("company") or {}
        )
        industry = str(company.get("industry") or "未分类")
        industries.append(industry)
    counts = Counter(industries)
    max_industry_share = max(counts.values()) / len(selected) * 100
    hhi = sum((count / len(selected)) ** 2 for count in counts.values())
    average_correlation = (
        sum(correlations) / len(correlations) if correlations else 0.0
    )
    max_correlation = max(correlations) if correlations else 0.0
    average_volatility = sum(
        _number(stock.get("volatility_20d")) for stock in selected
    ) / len(selected)
    risk_score = (
        average_correlation * 45
        + max_correlation * 20
        + hhi * 100 * 20
        + min(average_volatility, 70) * 0.15
    )
    risk_score = max(0.0, min(100.0, risk_score))
    status = (
        "高风险"
        if risk_score >= 65
        else "中等风险" if risk_score >= 40 else "分散尚可"
    )
    return {
        "available": True,
        "stock_count": len(selected),
        "status": status,
        "risk_score": risk_score,
        "average_correlation": average_correlation,
        "max_correlation": max_correlation,
        "industry_hhi": hhi,
        "max_industry_share": max_industry_share,
        "average_volatility_20d": average_volatility,
        "industries": industries,
        "pairs": sorted(
            pairs,
            key=lambda item: item["correlation"],
            reverse=True,
        )[:10],
        "method": (
            "使用候选股最近30日日收益相关性、行业HHI、最大行业占比和"
            "20日波动率计算组合集中度；不包含用户真实持仓权重。"
        ),
    }
