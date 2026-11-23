from __future__ import annotations

import math
from typing import Any


FRAMEWORK_VERSION = "CN-EQUITY-OBS-1"


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _gate(
    key: str,
    name: str,
    status: str,
    detail: str,
    implication: str,
) -> dict[str, str]:
    return {
        "key": key,
        "name": name,
        "status": status,
        "detail": detail,
        "implication": implication,
    }


def build_observation_framework(snapshot: dict[str, Any]) -> dict[str, Any]:
    strategy = snapshot.get("strategy") or {}
    evidence = snapshot.get("evidence") or {}
    breadth = snapshot.get("breadth") or {}
    money = snapshot.get("big_money") or {}
    validation = snapshot.get("historical_validation") or {}
    adaptive = snapshot.get("adaptive_model") or {}
    asset = snapshot.get("asset_decision") or {}
    technicals = snapshot.get("technicals") or {}
    trend = technicals.get("trend") or {}
    active = snapshot.get("active_stocks") or {}
    stocks = active.get("stocks") or []

    quality = _number(evidence.get("quality_score"))
    coverage = _number(breadth.get("coverage"))
    expected = _number(breadth.get("expected_total"), coverage)
    coverage_ratio = coverage / expected * 100 if expected else 0.0
    score = _number(strategy.get("score"))
    money_score = _number(money.get("score"))
    advance_ratio = _number(breadth.get("advance_ratio"))
    arms = _number(breadth.get("arms"), 1.0)
    futures = snapshot.get("futures") or []
    if_basis = next(
        (
            _number(item.get("basis_pct"))
            for item in futures
            if item.get("code") == "IF"
        ),
        None,
    )

    data_ok = quality >= 60 and coverage_ratio >= 90
    market_buy_allowed = (
        score >= 15
        and money_score >= 0
        and advance_ratio >= 0.5
        and (if_basis is None or if_basis > -2.0)
        and not adaptive.get("circuit_breaker")
    )

    if adaptive.get("circuit_breaker"):
        state = "模型熔断"
        posture = "暂停新增交易，保留观察和模拟记录"
        data_ok = False
    elif not data_ok:
        state = "暂停判断"
        posture = "数据不足，不交易"
    elif score <= -45 and money_score <= -20 and advance_ratio < 0.35:
        state = "防守"
        posture = "空仓或仅保留0%—10%观察仓"
    elif score <= -15 and money_score < 0:
        state = "偏空"
        posture = "控制权益仓位，不追反弹"
    elif score >= 15 and money_score >= 0 and advance_ratio >= 0.5:
        state = "进攻"
        posture = "按候选与价格触发分批执行"
    elif score >= -15:
        state = "震荡"
        posture = "持有观察，等待资金与广度共振"
    else:
        state = "分歧"
        posture = "降低交易频率，等待方向确认"

    factor_values = []
    for factor in strategy.get("factors") or []:
        value = _number(factor.get("score"))
        factor_values.append(value)
    factor_values.append(money_score / 100)
    positive = sum(value > 0.15 for value in factor_values)
    negative = sum(value < -0.15 for value in factor_values)
    neutral = len(factor_values) - positive - negative
    consensus = max(positive, negative, neutral) / len(factor_values) * 100

    gates = [
        _gate(
            "data",
            "数据可信度",
            "pass" if data_ok else "fail",
            f"质量 {quality:.0f}/100，全市场覆盖 {coverage_ratio:.1f}%",
            "决定模型是否允许输出交易动作",
        ),
        _gate(
            "trend",
            "趋势结构",
            "pass" if _number(trend.get("above_ma20_pct")) > 0 else "fail",
            (
                f"沪深300相对20日线 "
                f"{_number(trend.get('above_ma20_pct')):+.2f}%"
            ),
            "决定是否允许顺势仓位",
        ),
        _gate(
            "capital",
            "大资金行为",
            "pass" if money_score >= 0 else "fail",
            f"BM-1 {money_score:+.1f}，状态 {money.get('state', '--')}",
            "决定资金是否支持新增风险",
        ),
        _gate(
            "breadth",
            "市场广度",
            "pass" if advance_ratio >= 0.5 else "fail",
            f"上涨占比 {advance_ratio * 100:.1f}%，ARMS {arms:.3f}",
            "决定上涨是否具有普遍性",
        ),
        _gate(
            "futures",
            "期指风险偏好",
            "pass" if if_basis is not None and if_basis > -1.0 else "warn",
            (
                f"IF主力基差 {if_basis:+.2f}%"
                if if_basis is not None
                else "IF基差不可用"
            ),
            "反映对冲需求与机构预期",
        ),
    ]

    dimensions = [
        {
            "name": "趋势",
            "score": _number(trend.get("above_ma20_pct")),
            "status": "positive" if _number(trend.get("above_ma20_pct")) > 0 else "negative",
        },
        {
            "name": "资金",
            "score": money_score,
            "status": "positive" if money_score > 0 else "negative",
        },
        {
            "name": "广度",
            "score": advance_ratio * 100,
            "status": "positive" if advance_ratio >= 0.5 else "negative",
        },
        {
            "name": "期指",
            "score": if_basis or 0,
            "status": "positive" if (if_basis or 0) > -1 else "negative",
        },
        {
            "name": "历史验证",
            "score": _number(
                ((validation.get("similar") or {}).get("forward_20d") or {}).get(
                    "positive_probability"
                )
            ),
            "status": (
                "positive"
                if _number(
                    ((validation.get("similar") or {}).get("forward_20d") or {}).get(
                        "positive_probability"
                    )
                )
                >= 50
                else "negative"
            ),
        },
    ]

    technical_pass = len(stocks)
    fundamental_ready = sum(
        bool((stock.get("company_fundamentals") or {}).get("available"))
        for stock in stocks
    )
    behavior_ready = sum(
        bool((stock.get("capital_behavior") or {}).get("available"))
        for stock in stocks
    )
    trade_ready = sum(
        (stock.get("trade_plan") or {}).get("execution", "").startswith("满足")
        for stock in stocks
    )
    candidate_funnel = {
        "market_universe": int(round(coverage)),
        "liquid_screen": int(_number(active.get("universe_size"))),
        "technical_pass": technical_pass,
        "fundamental_ready": fundamental_ready,
        "behavior_ready": behavior_ready,
        "trade_ready": trade_ready if market_buy_allowed else 0,
    }

    scenarios = [
        {
            "name": "转强条件",
            "triggers": [
                "市场综合评分回到 +15 以上",
                "BM-1大资金评分回到 0 以上",
                "上涨家数占比达到 50%以上",
                "价格站上20日线并得到成交量确认",
            ],
            "action": "从观察仓开始，按候选股价格计划分两档建仓",
        },
        {
            "name": "弱势延续",
            "triggers": [
                "市场评分低于 -45",
                "BM-1评分低于 -20",
                "ARMS高于1.2或上涨占比低于35%",
            ],
            "action": "维持空仓或0%—10%观察仓，不摊平亏损",
        },
        {
            "name": "中性震荡",
            "triggers": [
                "市场评分介于 -15 和 +15",
                "资金与趋势方向不一致",
            ],
            "action": "只持有高质量既有仓位，不新增高波动股票",
        },
    ]

    return {
        "version": FRAMEWORK_VERSION,
        "as_of": snapshot.get("trade_date"),
        "state": state,
        "posture": posture,
        "market_score": score,
        "big_money_score": money_score,
        "historical_win_rate_20d": (
            ((validation.get("similar") or {}).get("forward_20d") or {}).get(
                "positive_probability"
            )
        ),
        "gates": gates,
        "dimensions": dimensions,
        "consistency": {
            "positive": positive,
            "negative": negative,
            "neutral": neutral,
            "consensus_pct": consensus,
            "description": (
                "信号高度一致"
                if consensus >= 75
                else "信号部分一致"
                if consensus >= 50
                else "信号明显分歧"
            ),
        },
        "candidate_funnel": candidate_funnel,
        "decision": {
            "buy_allowed": market_buy_allowed,
            "choice": asset.get("choice", posture),
            "target_low": asset.get("target_low"),
            "target_high": asset.get("target_high"),
            "reason": "；".join(
                gate["detail"] for gate in gates if gate["status"] == "fail"
            )
            or "主要门控条件通过",
        },
        "scenarios": scenarios,
        "missing_layers": [
            "宏观流动性：DR007、10年期国债收益率、股权风险溢价",
            "政策与公告事件：解禁、减持、财报、监管处罚",
            "组合层：持仓相关性、行业集中度、组合VaR",
        ],
        "principle": (
            "任何单一指标都不能直接触发交易；必须先通过数据门控，"
            "再通过市场状态、资金、广度和个股价格计划，最后执行风险预算。"
        ),
    }
