from __future__ import annotations

import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from asset_selector import _average, _return_pct, _volatility_pct, fetch_history


def _drawdown_from_high(closes: list[float], days: int = 20) -> float | None:
    values = closes[-days:]
    if not values:
        return None
    high = max(values)
    return (values[-1] / high - 1) * 100 if high else None


def _evaluate_seed(
    seed: dict[str, Any],
    *,
    market_score: float,
    csi_return_20d: float | None,
) -> dict[str, Any] | None:
    history = fetch_history(seed["code"], 80)
    closes = [float(row["close"]) for row in history]
    volumes = [float(row.get("volume") or 0) for row in history]
    if len(closes) < 60:
        return None

    current = seed["price"]
    ma20 = _average(closes[-20:])
    ma60 = _average(closes[-60:])
    ret5 = _return_pct(closes, 5)
    ret20 = _return_pct(closes, 20)
    volatility = _volatility_pct(closes, 20)
    drawdown20 = _drawdown_from_high(closes, 20)
    volume_avg5 = _average(volumes[-5:])
    volume_ratio = (
        volumes[-1] / volume_avg5 if volume_avg5 and volumes[-1] else None
    )
    true_ranges = []
    for index in range(max(1, len(history) - 14), len(history)):
        high = float(history[index].get("high") or history[index]["close"])
        low = float(history[index].get("low") or history[index]["close"])
        previous_close = float(history[index - 1]["close"])
        true_ranges.append(
            max(high - low, abs(high - previous_close), abs(low - previous_close))
        )
    atr14 = sum(true_ranges) / len(true_ranges) if true_ranges else None
    resistance20 = max(
        float(row.get("high") or row["close"]) for row in history[-21:-1]
    )
    support20 = min(
        float(row.get("low") or row["close"]) for row in history[-20:]
    )
    above_ma20 = (current / ma20 - 1) * 100 if ma20 else None
    relative20 = (
        ret20 - csi_return_20d
        if ret20 is not None and csi_return_20d is not None
        else None
    )
    pe = seed.get("pe")
    pb = seed.get("pb")
    turnover_rate = seed.get("turnover_rate")
    float_market_cap = seed.get("float_market_cap")

    valuation_penalty = 0.0
    if pe is None:
        valuation_risk = "估值数据缺失"
        valuation_penalty = 1.5
    elif pe <= 0:
        valuation_risk = "当前亏损，PE不可用"
        valuation_penalty = 4.0
    elif pe > 80:
        valuation_risk = "PE偏高"
        valuation_penalty = 3.0
    elif pb is not None and pb > 8:
        valuation_risk = "PB偏高"
        valuation_penalty = 2.0
    else:
        valuation_risk = "估值未见极端值"
    if turnover_rate is not None and turnover_rate > 15:
        valuation_penalty += 1.5

    pullback_low = ma20 * 0.995
    pullback_high = min(ma20 * 1.015, current * 1.01)
    pullback_mid = (pullback_low + pullback_high) / 2
    breakout_trigger = max(resistance20 * 1.005, current * 1.01)
    atr_value = atr14 or current * 0.03
    pullback_stop = max(
        support20 * 0.985,
        pullback_mid - 1.5 * atr_value,
    )
    pullback_stop = min(pullback_stop, pullback_mid * 0.985)
    breakout_stop = breakout_trigger - 1.5 * atr_value
    breakout_target_1 = breakout_trigger + 2 * atr_value
    breakout_target_2 = breakout_trigger + 3.5 * atr_value
    risk_per_share = max(pullback_mid - pullback_stop, 0.01)
    risk_budget_100k = 1000.0
    risk_based_shares = int(risk_budget_100k / risk_per_share / 100) * 100
    max_allocation_shares = int(20000 / pullback_mid / 100) * 100
    suggested_shares = min(risk_based_shares, max_allocation_shares)
    trade_plan = {
        "primary_setup": "回踩20日线不破",
        "pullback_entry_low": round(pullback_low, 2),
        "pullback_entry_high": round(pullback_high, 2),
        "pullback_entry_mid": round(pullback_mid, 2),
        "breakout_trigger": round(breakout_trigger, 2),
        "stop_price": round(pullback_stop, 2),
        "breakout_stop": round(breakout_stop, 2),
        "breakout_target_1": round(breakout_target_1, 2),
        "breakout_target_2": round(breakout_target_2, 2),
        "target_1": round(pullback_mid + 2 * atr_value, 2),
        "target_2": round(pullback_mid + 3.5 * atr_value, 2),
        "risk_per_share": round(risk_per_share, 2),
        "risk_pct": round(risk_per_share / pullback_mid * 100, 2),
        "risk_budget_100k": risk_budget_100k,
        "suggested_shares_100k": suggested_shares,
        "suggested_value_100k": round(suggested_shares * pullback_mid, 0),
        "risk_based_shares_100k": risk_based_shares,
        "allocation_cap_applied": suggested_shares < risk_based_shares,
        "planned_risk_100k": round(suggested_shares * risk_per_share, 0),
        "execution": (
            "当前不执行，仅观察价位"
            if market_score < 15
            else "满足触发条件后分两档执行"
        ),
    }

    passes = bool(
        ma20
        and ma60
        and current > ma20 > ma60
        and (ret5 or 0) > 0
        and (ret20 or 0) > 0
        and (volatility or 999) < 55
        and (drawdown20 or -999) > -10
        and seed.get("amount", 0) >= 5e8
    )

    raw_score = (
        (ret5 or 0) * 1.15
        + (ret20 or 0) * 0.42
        + (above_ma20 or 0) * 1.25
        + (relative20 or 0) * 0.25
        - (volatility or 30) * 0.11
        + min(5.0, math.log10(max(seed.get("amount", 0), 1)) - 8)
        - valuation_penalty
    )
    score = max(0.0, min(100.0, 50.0 + raw_score))

    if not passes:
        return None

    if market_score <= -45:
        status = "观察不买"
        action = "空仓等待，不执行买入"
    elif market_score < 0:
        status = "等待确认"
        action = "评分回到0以上后再考虑首档"
    elif market_score < 15:
        status = "小仓观察"
        action = "只做计划仓位的五分之一"
    else:
        status = "分批候选"
        action = "回踩20日线不破时执行首档"

    return {
        "code": seed["code"],
        "name": seed["name"],
        "price": current,
        "change_pct": seed["change_pct"],
        "amount": seed["amount"],
        "ma20": ma20,
        "ma60": ma60,
        "above_ma20_pct": above_ma20,
        "return_5d": ret5,
        "return_20d": ret20,
        "relative_20d": relative20,
        "volatility_20d": volatility,
        "drawdown_20d": drawdown20,
        "volume_ratio": volume_ratio,
        "atr14": atr14,
        "atr_pct": (atr14 / current * 100 if atr14 and current else None),
        "support_20d": support20,
        "resistance_20d": resistance20,
        "pe": pe,
        "pb": pb,
        "turnover_rate": turnover_rate,
        "float_market_cap": float_market_cap,
        "valuation_risk": valuation_risk,
        "trade_plan": trade_plan,
        "recent_returns_30d": [
            (
                closes[index] / closes[index - 1] - 1
            )
            * 100
            for index in range(max(1, len(closes) - 30), len(closes))
            if closes[index - 1]
        ],
        "price_adjustment": (
            history[-1].get("price_adjustment")
            if history
            else "unknown"
        ),
        "score": round(score, 1),
        "status": status,
        "action": action,
        "trigger": (
            f"收盘保持在20日线 {ma20:.2f} 上方，且市场资金流不再继续恶化"
        ),
        "invalidation": (
            f"收盘跌破20日线 {ma20:.2f} 超过2%，或20日线跌破60日线"
        ),
        "reason": (
            f"5日 {ret5:+.1f}% / 20日 {ret20:+.1f}%；"
            f"20日波动 {(volatility or 0):.0f}%；{valuation_risk}"
        ),
    }


def select_active_stocks(
    seeds: list[dict[str, Any]],
    *,
    market_score: float,
    csi_return_20d: float | None,
    top_n: int = 6,
) -> list[dict[str, Any]]:
    if not seeds:
        return []
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {
            executor.submit(
                _evaluate_seed,
                seed,
                market_score=market_score,
                csi_return_20d=csi_return_20d,
            ): seed
            for seed in seeds
        }
        for future in as_completed(futures):
            try:
                item = future.result()
                if item:
                    results.append(item)
            except Exception:
                continue
    results.sort(
        key=lambda item: (
            item["score"],
            item.get("return_5d") or 0,
            item.get("amount") or 0,
        ),
        reverse=True,
    )
    return results[:top_n]
