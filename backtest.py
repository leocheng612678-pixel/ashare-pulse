from __future__ import annotations

import math
import statistics
from typing import Any


def _clip(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _position_for_score(score: float) -> float:
    if score >= 20:
        return 0.80
    if score >= 0:
        return 0.50
    if score >= -20:
        return 0.20
    return 0.0


def run_core_backtest(snapshot: dict[str, Any]) -> dict[str, Any]:
    history = snapshot.get("historical_csi") or (
        (snapshot.get("technicals") or {}).get("csi_full_history") or []
    )
    capital = (snapshot.get("capital") or {}).get("history") or []
    if len(history) < 90 or len(capital) < 30:
        return {
            "available": False,
            "reason": "历史行情或资金流样本不足",
        }
    closes = {
        str(row.get("date")): float(row.get("close"))
        for row in history
        if row.get("date") and row.get("close") is not None
    }
    dates = sorted(closes)
    flow_by_date = {
        str(row.get("date")): _number(row.get("main")) for row in capital
    }
    close_values = [closes[day] for day in dates]
    flow_values = [flow_by_date.get(day) for day in dates]
    rolling_flow5: list[float | None] = [None] * len(dates)
    for index in range(4, len(dates)):
        values = flow_values[index - 4 : index + 1]
        if all(value is not None for value in values):
            rolling_flow5[index] = sum(value for value in values if value is not None)

    equity = 1.0
    benchmark = 1.0
    previous_position = 0.0
    daily_returns: list[float] = []
    strategy_returns: list[float] = []
    positions: list[float] = []
    turnover = 0.0
    trades = 0
    curve = []
    one_way_cost = 0.001

    for index in range(60, len(dates) - 1):
        day = dates[index]
        current = close_values[index]
        ma20 = statistics.fmean(close_values[index - 19 : index + 1])
        ma60 = statistics.fmean(close_values[index - 59 : index + 1])
        ret5 = (
            (current / close_values[index - 5] - 1) * 100
            if index >= 5
            else 0.0
        )
        trend = (
            _clip((current / ma20 - 1) / 0.03, -1, 1) * 0.6
            + _clip(ret5 / 4.0, -1, 1) * 0.2
            + (1.0 if ma20 > ma60 else -1.0) * 0.2
        )
        current_flow5 = rolling_flow5[index]
        prior_flow5 = [
            value
            for value in rolling_flow5[max(0, index - 60) : index]
            if value is not None
        ]
        if current_flow5 is None or len(prior_flow5) < 20:
            continue
        mean = statistics.fmean(prior_flow5)
        deviation = statistics.pstdev(prior_flow5)
        flow_z = (current_flow5 - mean) / deviation if deviation else 0.0
        score = 50 + 30 * trend + 20 * _clip(flow_z / 1.5, -1, 1)
        position = _position_for_score(score)
        next_return = close_values[index + 1] / current - 1
        change = abs(position - previous_position)
        cost = change * one_way_cost
        if change >= 0.1:
            trades += 1
        turnover += change
        strategy_return = position * next_return - cost
        equity *= 1 + strategy_return
        benchmark *= 1 + next_return
        cumulative_return = (equity - 1) * 100
        curve.append(
            {
                "date": dates[index + 1],
                "equity": equity,
                "benchmark": benchmark,
                "position": position,
                "score": score,
                "return_pct": cumulative_return,
            }
        )
        daily_returns.append(next_return)
        strategy_returns.append(strategy_return)
        positions.append(position)
        previous_position = position

    if not strategy_returns:
        return {"available": False, "reason": "无法生成有效回测样本"}

    peak = 1.0
    max_drawdown = 0.0
    for point in curve:
        peak = max(peak, point["equity"])
        max_drawdown = min(max_drawdown, point["equity"] / peak - 1)
    mean = statistics.fmean(strategy_returns)
    volatility = statistics.pstdev(strategy_returns) * math.sqrt(252)
    years = len(strategy_returns) / 252
    cagr = equity ** (1 / years) - 1 if years > 0 and equity > 0 else 0
    sharpe = (
        (mean * 252 - 0.02) / volatility if volatility > 0 else None
    )
    benchmark_return = benchmark - 1
    return {
        "available": True,
        "methodology": (
            "使用无未来数据的沪深300趋势与5日资金流标准化分数；"
            "在收盘产生信号，下一交易日收盘调整仓位。"
        ),
        "assumptions": (
            "单边成本10bp；未计指数ETF跟踪误差、申赎限制和盘中冲击。"
        ),
        "sample_days": len(strategy_returns),
        "start_date": curve[0]["date"],
        "end_date": curve[-1]["date"],
        "total_return_pct": (equity - 1) * 100,
        "annualized_return_pct": cagr * 100,
        "benchmark_return_pct": benchmark_return * 100,
        "excess_return_pct": ((equity - 1) - benchmark_return) * 100,
        "max_drawdown_pct": max_drawdown * 100,
        "annualized_volatility_pct": volatility * 100,
        "sharpe": sharpe,
        "daily_win_rate_pct": (
            sum(value > 0 for value in strategy_returns)
            / len(strategy_returns)
            * 100
        ),
        "active_day_win_rate_pct": (
            sum(
                actual > 0
                for actual, position in zip(strategy_returns, positions)
                if position > 0
            )
            / max(1, sum(position > 0 for position in positions))
            * 100
        ),
        "average_exposure_pct": statistics.fmean(positions) * 100,
        "turnover": turnover,
        "trade_count": trades,
        "curve": curve[-120:],
    }
