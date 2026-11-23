from __future__ import annotations

import math
from datetime import datetime
from typing import Any


RISK_PROFILES = {
    "conservative": {
        "label": "稳健",
        "center": 30,
        "span": 12,
        "cap": 55,
        "description": "优先控制回撤，保留较高现金比例",
    },
    "balanced": {
        "label": "均衡",
        "center": 50,
        "span": 20,
        "cap": 80,
        "description": "兼顾趋势参与和回撤控制",
    },
    "aggressive": {
        "label": "进取",
        "center": 68,
        "span": 25,
        "cap": 95,
        "description": "承受更大波动，争取更高仓位弹性",
    },
}


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _safe(value: Any) -> float | None:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return None
        return number
    except (TypeError, ValueError):
        return None


def _factor(
    key: str,
    name: str,
    score: float,
    weight: float,
    detail: str,
    status: str = "neutral",
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "score": round(score, 1),
        "weight": weight,
        "contribution": round(score * weight, 1),
        "detail": detail,
        "status": status,
    }


def _trend_factor(technicals: dict[str, Any]) -> dict[str, Any]:
    trend = technicals.get("trend") or {}
    close = _safe(trend.get("close"))
    ma20 = _safe(trend.get("ma20"))
    ma60 = _safe(trend.get("ma60"))
    ret5 = _safe(trend.get("return_5d"))
    if close is None or ma20 is None:
        return _factor("trend", "趋势", 0, 30, "日线不足，暂按中性处理")
    ma_score = _clip((close / ma20 - 1) / 0.03)
    ret_score = _clip((ret5 or 0) / 4.0)
    long_score = 1.0 if ma60 and ma20 > ma60 else (-1.0 if ma60 else 0.0)
    score = ma_score * 0.6 + ret_score * 0.2 + long_score * 0.2
    status = "positive" if score > 0.15 else ("negative" if score < -0.15 else "neutral")
    relation = "上方" if close >= ma20 else "下方"
    detail = (
        f"沪深300位于20日线{relation} "
        f"{(close / ma20 - 1) * 100:+.1f}%，"
        f"近5日 {(ret5 or 0):+.1f}%"
    )
    return _factor("trend", "趋势", score, 30, detail, status)


def _flow_factor(snapshot: dict[str, Any]) -> dict[str, Any]:
    history = ((snapshot.get("capital") or {}).get("history") or [])[-5:]
    turnover_history = (
        (snapshot.get("technicals") or {}).get("turnover_history") or []
    )[-5:]
    if not history:
        return _factor("flow", "资金", 0, 30, "资金流缺失，暂按中性处理")
    current = _safe(history[-1].get("main")) or 0.0
    total5 = sum(_safe(row.get("main")) or 0.0 for row in history)
    turnover = _safe((snapshot.get("technicals") or {}).get("turnover"))
    current_ratio = current / turnover * 100 if turnover else None
    turnover5 = sum(
        _safe(row.get("amount")) or 0.0 for row in turnover_history
    )
    ratio5 = total5 / turnover5 * 100 if turnover5 else None

    current_score = _clip((current_ratio or 0) / 3.0)
    flow5_score = _clip((ratio5 or 0) / 2.2)
    score = flow5_score * 0.7 + current_score * 0.3
    status = "positive" if score > 0.15 else ("negative" if score < -0.15 else "neutral")
    detail = (
        f"今日主力 {current / 1e8:+.1f} 亿；"
        f"近5日合计 {total5 / 1e8:+.1f} 亿"
    )
    return _factor("flow", "资金", score, 30, detail, status)


def _breadth_factor(
    snapshot: dict[str, Any], history: list[dict[str, Any]]
) -> dict[str, Any]:
    breadth = snapshot.get("breadth") or {}
    up = int(_safe(breadth.get("up")) or 0)
    down = int(_safe(breadth.get("down")) or 0)
    arms = _safe(breadth.get("arms"))
    if not up + down:
        return _factor("breadth", "广度", 0, 30, "涨跌家数缺失，暂按中性处理")
    spread = (up - down) / (up + down)
    arms_score = _clip((1.0 - arms) / 0.55) if arms else 0.0
    score = spread * 0.65 + arms_score * 0.35

    recent_arms = []
    for item in history[-4:]:
        value = _safe((item.get("breadth") or {}).get("arms"))
        if value is not None:
            recent_arms.append(value)
    if arms:
        recent_arms.append(arms)
    arms_avg = sum(recent_arms) / len(recent_arms) if recent_arms else None

    trend = (snapshot.get("technicals") or {}).get("trend") or {}
    ret5 = _safe(trend.get("return_5d")) or 0.0
    advance_ratio = up / (up + down) if up + down else 0.0
    overheating = (
        arms is not None
        and arms < 0.58
        and ret5 > 3
        and advance_ratio > 0.35
    )
    if overheating:
        score = min(score, 0.45)
    status = "positive" if score > 0.15 else ("negative" if score < -0.15 else "neutral")
    detail = f"上涨 {up} 家 / 下跌 {down} 家；ARMS {arms:.2f}" if arms else f"上涨 {up} 家 / 下跌 {down} 家"
    if overheating:
        detail += "，短线偏热"
    return _factor(
        "breadth",
        "广度",
        score,
        30 if arms_avg is None else 30,
        detail,
        status,
    )


def _futures_factor(snapshot: dict[str, Any]) -> dict[str, Any]:
    futures = snapshot.get("futures") or []
    target = next((item for item in futures if item.get("code") == "IF"), None)
    basis_pct = _safe((target or {}).get("basis_pct"))
    if basis_pct is None:
        return _factor("futures", "期指", 0, 10, "升贴水缺失，暂按中性处理")
    score = _clip(basis_pct / 1.5)
    status = "positive" if score > 0.15 else ("negative" if score < -0.15 else "neutral")
    word = "升水" if basis_pct > 0 else "贴水"
    return _factor(
        "futures",
        "期指",
        score,
        10,
        f"IF 主力相对沪深300 {word} {abs(basis_pct):.2f}%",
        status,
    )


def _confidence(snapshot: dict[str, Any]) -> tuple[int, list[str]]:
    issues: list[str] = []
    score = 100
    trend = (snapshot.get("technicals") or {}).get("trend") or {}
    if _safe(trend.get("ma20")) is None:
        score -= 25
        issues.append("趋势日线不足")
    if not ((snapshot.get("capital") or {}).get("history")):
        score -= 25
        issues.append("资金流缺失")
    breadth = snapshot.get("breadth") or {}
    if _safe(breadth.get("arms")) is None:
        score -= 20
        issues.append("ARMS 无法计算")
    if not breadth.get("complete", False):
        score -= 10
        issues.append("涨跌家数覆盖不完整")
    futures = snapshot.get("futures") or []
    if not any(_safe(item.get("basis_pct")) is not None for item in futures):
        score -= 10
        issues.append("期指升贴水缺失")
    for source in (snapshot.get("sources") or {}).values():
        if source.get("status") in ("error", "cached"):
            score -= 5
    return max(20, min(100, score)), issues


def _risk_flags(snapshot: dict[str, Any]) -> list[dict[str, str]]:
    flags: list[dict[str, str]] = []
    breadth = snapshot.get("breadth") or {}
    arms = _safe(breadth.get("arms"))
    trend = (snapshot.get("technicals") or {}).get("trend") or {}
    vol = _safe(trend.get("volatility_20d"))
    capital = ((snapshot.get("capital") or {}).get("history") or [])
    main_today = _safe(capital[-1].get("main")) if capital else None

    if arms is not None and arms > 1.35:
        flags.append(
            {
                "level": "high",
                "title": "广度偏弱",
                "detail": "ARMS 显著高于 1，下跌股成交量占比偏高。",
            }
        )
    up = int(_safe(breadth.get("up")) or 0)
    down = int(_safe(breadth.get("down")) or 0)
    advance_ratio = up / (up + down) if up + down else 0.0
    ret5 = _safe(trend.get("return_5d")) or 0.0
    if arms is not None and arms < 0.58 and ret5 > 3 and advance_ratio > 0.35:
        flags.append(
            {
                "level": "medium",
                "title": "短线偏热",
                "detail": "上涨股成交占比高度集中，追涨的赔率可能下降。",
            }
        )
    if vol is not None and vol > 30:
        flags.append(
            {
                "level": "high",
                "title": "波动升高",
                "detail": f"沪深300近20日年化波动约 {vol:.0f}%。",
            }
        )
    if main_today is not None and main_today < -300e8:
        flags.append(
            {
                "level": "high",
                "title": "资金大幅流出",
                "detail": f"沪深指数口径主力资金净流出 {abs(main_today) / 1e8:.0f} 亿元。",
            }
        )
    futures = snapshot.get("futures") or []
    target = next((item for item in futures if item.get("code") == "IF"), None)
    basis_pct = _safe((target or {}).get("basis_pct"))
    if basis_pct is not None and basis_pct < -2:
        flags.append(
            {
                "level": "medium",
                "title": "期指深度贴水",
                "detail": "IF 主力贴水超过 2%，反映对冲需求或谨慎预期较强。",
            }
        )
    return flags[:4]


def _exposure(score: float, profile_key: str, confidence: int) -> dict[str, Any]:
    profile = RISK_PROFILES.get(profile_key, RISK_PROFILES["balanced"])
    center = profile["center"]
    span = profile["span"]
    mid = center + score / 100 * span
    if score <= -45:
        defensive_high = {
            "conservative": 5,
            "balanced": 10,
            "aggressive": 20,
        }.get(profile_key, 10)
        mid = min(mid, defensive_high / 2)
        band = 3
        low = 0
        high = defensive_high
        return {
            "low": low,
            "high": high,
            "mid": int(round(mid / 5) * 5),
            "cap": profile["cap"],
            "profile": profile_key,
            "profile_label": profile["label"],
        }
    band = 5 if confidence >= 70 else 7
    low = max(0, min(profile["cap"], mid - band))
    high = max(0, min(profile["cap"], mid + band))
    return {
        "low": int(round(low / 5) * 5),
        "high": int(round(high / 5) * 5),
        "mid": int(round(mid / 5) * 5),
        "cap": profile["cap"],
        "profile": profile_key,
        "profile_label": profile["label"],
    }


def _action_text(score: float, exposure: dict[str, int]) -> tuple[str, str]:
    if score >= 45:
        return (
            "进攻但不追高",
            "趋势、资金和广度形成共振时，按计划分三档加仓；单日大涨后等待回踩确认。",
        )
    if score >= 15:
        return (
            "偏多，分批参与",
            "只在目标仓位下沿附近加一档，次日资金与广度继续改善再补第二档。",
        )
    if score > -15:
        return (
            "中性，维持仓位",
            "保持目标区间，减少频繁交易；新仓等待趋势或资金出现明确改善。",
        )
    if score > -45:
        return (
            "偏空，控制回撤",
            "逐步降至目标区间下沿，反弹中减仓，不在弱势里补跌。",
        )
    return (
        "防守，优先现金",
        "仅保留试探性仓位；若持仓超过区间上沿，分两到三次降低风险敞口。",
    )


def build_strategy(
    snapshot: dict[str, Any],
    history: list[dict[str, Any]] | None = None,
    risk_profile: str = "balanced",
    factor_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    history = history or []
    factors = [
        _trend_factor(snapshot.get("technicals") or {}),
        _flow_factor(snapshot),
        _breadth_factor(snapshot, history),
        _futures_factor(snapshot),
    ]
    if factor_weights:
        for factor in factors:
            weight = factor_weights.get(factor["key"])
            if weight is not None:
                factor["weight"] = round(float(weight), 2)
                factor["contribution"] = round(
                    factor["score"] * factor["weight"], 2
                )
    score = round(sum(item["contribution"] for item in factors), 1)
    confidence, confidence_issues = _confidence(snapshot)
    exposure = _exposure(score, risk_profile, confidence)
    action, summary = _action_text(score, exposure)

    if score >= 45:
        regime = "进攻"
        tone = "positive"
    elif score >= 15:
        regime = "偏多"
        tone = "positive"
    elif score > -15:
        regime = "震荡"
        tone = "neutral"
    elif score > -45:
        regime = "偏空"
        tone = "negative"
    else:
        regime = "防守"
        tone = "negative"

    turnover = _safe((snapshot.get("technicals") or {}).get("turnover"))
    main_today = None
    capital_history = (snapshot.get("capital") or {}).get("history") or []
    if capital_history:
        main_today = _safe(capital_history[-1].get("main"))
    flow_ratio = (
        main_today / turnover * 100
        if main_today is not None and turnover
        else None
    )

    trade_date = snapshot.get("trade_date") or ""
    try:
        generated_date = datetime.strptime(trade_date, "%Y-%m-%d").date()
        current_date = datetime.now().date()
        age_days = (current_date - generated_date).days
    except ValueError:
        age_days = 0

    return {
        "score": score,
        "regime": regime,
        "tone": tone,
        "action": action,
        "summary": summary,
        "confidence": confidence,
        "confidence_issues": confidence_issues,
        "exposure": exposure,
        "factors": factors,
        "risk_flags": _risk_flags(snapshot),
        "metrics": {
            "turnover": turnover,
            "turnover_vs_20d": _safe(
                (snapshot.get("technicals") or {}).get("turnover_vs_20d")
            ),
            "main_net_today": main_today,
            "main_net_ratio": flow_ratio,
            "trade_age_days": age_days,
        },
        "rules": {
            "add": [
                "现仓位低于目标区间时，先执行计划缺口的三分之一。",
                "只在收盘确认趋势未恶化、资金流出收窄后执行下一档。",
                "单次加仓不超过总资金的 10%，不用杠杆。",
            ],
            "reduce": [
                "现仓位高于目标区间上沿时，分两到三次降至区间内。",
                "沪深300收盘跌破20日线超过1%，且主力资金连续两日净流出，减一档。",
                "波动显著放大或模型置信度低于50时，优先降低风险。",
            ],
            "risk": [
                "账户回撤达到个人预设上限（建议5%—8%）时暂停加仓。",
                "ETF或一篮子标的应分散执行，避免用单日信号满仓押注。",
                "数据缺失或行情过期时，不根据本评分新增风险。",
            ],
        },
    }


def personalize_plan(
    strategy: dict[str, Any],
    capital: float | None,
    current_position_pct: float | None,
) -> dict[str, Any]:
    exposure = strategy.get("exposure") or {}
    low = float(exposure.get("low") or 0)
    high = float(exposure.get("high") or 0)
    mid = float(exposure.get("mid") or 0)
    result: dict[str, Any] = {
        "target_low": low,
        "target_high": high,
        "target_mid": mid,
    }
    if capital is None or current_position_pct is None:
        result.update(
            {
                "action": "待填写资金与当前仓位",
                "gap_pct": None,
                "gap_amount": None,
                "first_tranche": None,
            }
        )
        return result

    current = max(0.0, min(100.0, float(current_position_pct)))
    gap_pct = mid - current
    gap_amount = capital * gap_pct / 100
    if gap_pct > 3:
        action = f"分批加仓，首档约 {gap_pct / 3:.1f}% 总资金"
    elif gap_pct < -3:
        action = f"分批减仓，首档约 {abs(gap_pct) / 2:.1f}% 总资金"
    else:
        action = "维持现有仓位"
    result.update(
        {
            "action": action,
            "current_position": current,
            "gap_pct": gap_pct,
            "gap_amount": gap_amount,
            "first_tranche": abs(gap_amount) / 3 if gap_amount else 0,
        }
    )
    return result
