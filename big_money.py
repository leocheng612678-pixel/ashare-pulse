from __future__ import annotations

import math
import json
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from market_data import _fetch_json, _number
from storage import DATA_DIR


FLOW_CACHE_DIR = DATA_DIR / "big_money_flows"


def _cache_path(code: str):
    FLOW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return FLOW_CACHE_DIR / f"{code}.json"


def _load_cached_rows(code: str) -> list[dict[str, Any]]:
    path = _cache_path(code)
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def _save_cached_rows(code: str, rows: list[dict[str, Any]]) -> None:
    try:
        _cache_path(code).write_text(
            json.dumps(rows[-120:], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        pass


def _clip(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _flow_for_stock(code: str, days: int = 90) -> list[dict[str, Any]]:
    prefix = "1" if code.startswith("6") else "0"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        f"?lmt={days}&klt=101&secid={prefix}.{code}"
        "&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    rows = ((payload.get("data") or {}).get("klines") or [])
    result = []
    for raw in rows:
        fields = raw.split(",")
        if len(fields) < 13:
            continue
        result.append(
            {
                "date": fields[0],
                "main": _number(fields[1], 0.0),
                "small": _number(fields[2], 0.0),
                "medium": _number(fields[3], 0.0),
                "large": _number(fields[4], 0.0),
                "super": _number(fields[5], 0.0),
                "close": _number(fields[11]),
                "change_pct": _number(fields[12], 0.0),
            }
        )
    if not result:
        raise RuntimeError(f"{code} 没有个股资金流历史")
    return result


def _current_flows(codes: list[str]) -> dict[str, dict[str, Any]]:
    secids = [
        f"{'1' if code.startswith('6') else '0'}.{code}"
        for code in codes
    ]
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get"
        f"?fltt=2&secids={','.join(secids)}"
        "&fields=f2,f3,f12,f13,f14,f124,f62,f66,f69,f72,f75,"
        "f78,f81,f84,f87,f184"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    rows = ((payload.get("data") or {}).get("diff") or [])
    if isinstance(rows, dict):
        rows = [rows]
    return {
        str(row.get("f12")): {
            "date": "",
            "main": _number(row.get("f62"), 0.0),
            "super": _number(row.get("f66"), 0.0),
            "large": _number(row.get("f72"), 0.0),
            "medium": _number(row.get("f78"), 0.0),
            "small": _number(row.get("f84"), 0.0),
            "change_pct": _number(row.get("f3"), 0.0),
            "main_ratio": _number(row.get("f184"), 0.0),
        }
        for row in rows
        if row.get("f12")
    }


def _streak(values: list[float]) -> int:
    if not values:
        return 0
    sign = 1 if values[-1] > 0 else -1 if values[-1] < 0 else 0
    if sign == 0:
        return 0
    count = 0
    for value in reversed(values):
        if value * sign <= 0:
            break
        count += 1
    return count * sign


def _rolling_sum(values: list[float], window: int) -> list[float]:
    return [
        sum(values[index - window + 1 : index + 1])
        for index in range(window - 1, len(values))
    ]


def _zscore_latest(
    values: list[float], window: int = 5, lookback: int = 60
) -> float:
    rolling = _rolling_sum(values, window)
    if len(rolling) < 2:
        return 0.0
    current = rolling[-1]
    prior = rolling[max(0, len(rolling) - lookback - 1) : -1]
    if len(prior) < 10:
        return 0.0
    mean = statistics.fmean(prior)
    deviation = statistics.pstdev(prior)
    return (current - mean) / deviation if deviation else 0.0


def _flow_behavior(
    rows: list[dict[str, Any]],
    *,
    price_return_5d: float | None,
    amount: float | None,
    volume_ratio: float | None,
) -> dict[str, Any]:
    main_values = [float(row["main"]) for row in rows]
    super_values = [float(row["super"]) for row in rows]
    main5 = sum(main_values[-5:])
    super5 = sum(super_values[-5:])
    main20 = sum(main_values[-20:])
    super20 = sum(super_values[-20:])
    main1 = main_values[-1]
    super1 = super_values[-1]
    previous5 = main_values[-6:-1]
    acceleration = main1 - statistics.fmean(previous5) if previous5 else 0.0
    main_z = _zscore_latest(main_values)
    super_z = _zscore_latest(super_values)
    main20_z = _zscore_latest(main_values, 20)
    super20_z = _zscore_latest(super_values, 20)
    positive_consistency = (
        sum(value > 0 for value in main_values[-20:]) / 20 * 100
        if len(main_values) >= 20
        else None
    )
    main_streak = _streak(main_values)
    super_streak = _streak(super_values)
    super_share = super5 / main5 if abs(main5) > 1 else 0.0
    intensity = (
        main5 / max(amount * 5, 1) * 100
        if amount and amount > 0
        else None
    )

    price_divergence = 0.0
    divergence_text = "价格与资金方向基本一致"
    if price_return_5d is not None and main5 > 0 and price_return_5d < 0:
        price_divergence = 14.0
        divergence_text = "资金流入但价格未涨，存在吸筹背离"
    elif price_return_5d is not None and main5 < 0 and price_return_5d > 0:
        price_divergence = -16.0
        divergence_text = "价格上涨但资金流出，存在派发背离"

    score = (
        _clip(main_z / 2.0) * 20
        + _clip(main20_z / 2.0) * 15
        + _clip(super_z / 2.0) * 15
        + _clip(super20_z / 2.0) * 10
        + _clip(intensity or 0, -10, 10) * 1.2
        + (1 if main_streak > 0 else -1 if main_streak < 0 else 0)
        * min(abs(main_streak), 5)
        * 3
        + (1 if main5 > 0 else -1 if main5 < 0 else 0)
        * _clip(super_share, -1.0, 1.0)
        * 8
        + price_divergence
        + _clip((volume_ratio or 1) - 1, -1, 1) * 8
        + _clip(acceleration / max(abs(statistics.fmean(main_values[-20:])), 1), -2, 2)
        * 5
    )
    score = round(max(-100.0, min(100.0, score)), 1)
    if len(rows) < 10:
        score = round(score * 0.55, 1)

    if main5 > 0 and super5 > 0 and main_streak >= 2 and (price_return_5d or 0) < 2:
        state = "疑似吸筹"
    elif main5 > 0 and super5 > 0 and (price_return_5d or 0) >= 2:
        state = "资金推动"
    elif main5 < 0 and super5 < 0 and (price_return_5d or 0) > 0:
        state = "高位派发"
    elif main5 < 0 and super5 < 0 and (price_return_5d or 0) < -2:
        state = "集中撤离"
    else:
        state = "资金分歧"

    evidence = [
        f"5日主力资金 {main5 / 1e8:+.2f} 亿，历史标准分 {main_z:+.2f}",
        f"20日主力资金 {main20 / 1e8:+.2f} 亿，历史标准分 {main20_z:+.2f}",
        f"5日超大单 {super5 / 1e8:+.2f} 亿，超大单标准分 {super_z:+.2f}",
        f"20日超大单 {super20 / 1e8:+.2f} 亿，历史标准分 {super20_z:+.2f}",
        f"主力连续流入天数 {main_streak:+d}，单日资金加速度 "
        f"{acceleration / 1e8:+.2f} 亿",
        divergence_text,
    ]
    if intensity is not None:
        evidence.append(f"5日资金强度约为近5日成交额的 {intensity:+.2f}%")
    if positive_consistency is not None:
        evidence.append(f"近20日主力资金为正的天数占比 {positive_consistency:.0f}%")
    if len(rows) < 10:
        evidence.append(
            f"历史资金样本仅 {len(rows)} 日，当前结论低置信度，需次日确认"
        )
    return {
        "score": score,
        "state": state,
        "main_net_1d": main1,
        "main_net_5d": main5,
        "main_net_20d": main20,
        "super_net_1d": super1,
        "super_net_5d": super5,
        "super_net_20d": super20,
        "main_flow_z": main_z,
        "super_flow_z": super_z,
        "main20_flow_z": main20_z,
        "super20_flow_z": super20_z,
        "positive_consistency": positive_consistency,
        "main_streak": main_streak,
        "super_streak": super_streak,
        "super_share": super_share,
        "flow_acceleration": acceleration,
        "flow_intensity_pct": intensity,
        "price_divergence": divergence_text,
        "evidence": evidence,
        "observations": len(rows),
        "confidence": (
            "高" if len(rows) >= 40 else "中" if len(rows) >= 10 else "低"
        ),
        "algorithm": "BM-1 大资金行为复合评分",
    }


def analyze_market_big_money(snapshot: dict[str, Any]) -> dict[str, Any]:
    capital = ((snapshot.get("capital") or {}).get("history") or [])
    trend = (snapshot.get("technicals") or {}).get("trend") or {}
    breadth = snapshot.get("breadth") or {}
    if not capital:
        return {
            "available": False,
            "state": "数据不足",
            "score": 0,
            "evidence": ["市场资金流历史不可用"],
            "algorithm": "BM-1 大资金行为复合评分",
        }
    rows = [
        {
            "main": row.get("main") or 0,
            "super": row.get("super") or 0,
        }
        for row in capital
    ]
    behavior = _flow_behavior(
        rows,
        price_return_5d=_number(trend.get("return_5d")),
        amount=_number((snapshot.get("technicals") or {}).get("turnover")),
        volume_ratio=_number(
            (snapshot.get("technicals") or {}).get("turnover_vs_20d"), 1.0
        ),
    )
    advance_ratio = _number(breadth.get("advance_ratio"), 0.5)
    if behavior["main_net_5d"] > 0 and advance_ratio < 0.4:
        behavior["state"] = "指数护盘/资金分歧"
        behavior["evidence"].append(
            "5日资金为正但上涨家数占比不足40%，大资金可能集中于少数权重股"
        )
    behavior["available"] = True
    behavior["breadth_advance_ratio"] = advance_ratio
    return behavior


def _analyze_stock(
    stock: dict[str, Any],
    current_flow: dict[str, Any] | None,
    trade_date: str | None,
) -> dict[str, Any]:
    try:
        rows = _flow_for_stock(stock["code"])
        source = "东方财富个股资金流历史"
        _save_cached_rows(stock["code"], rows)
    except Exception as exc:
        cached = _load_cached_rows(stock["code"])
        if current_flow:
            row = dict(current_flow)
            row["date"] = trade_date or row.get("date") or ""
            if row["date"]:
                cached = [item for item in cached if item.get("date") != row["date"]]
                cached.append(row)
                cached.sort(key=lambda item: str(item.get("date") or ""))
                _save_cached_rows(stock["code"], cached)
        if not cached:
            raise
        rows = cached
        source = f"东方财富资金流缓存/当日数据；历史接口失败：{exc}"
    behavior = _flow_behavior(
        rows,
        price_return_5d=_number(stock.get("return_5d")),
        amount=_number(stock.get("amount")),
        volume_ratio=_number(stock.get("volume_ratio"), 1.0),
    )
    behavior["available"] = True
    behavior["source"] = source
    behavior["history"] = rows[-30:]
    return behavior


def enrich_stocks_with_big_money(
    stocks: list[dict[str, Any]],
    max_workers: int = 3,
    trade_date: str | None = None,
) -> list[dict[str, Any]]:
    if not stocks:
        return stocks
    try:
        current_flows = _current_flows([stock["code"] for stock in stocks])
    except Exception:
        current_flows = {}
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(
                _analyze_stock,
                stock,
                current_flows.get(stock["code"]),
                trade_date,
            ): stock["code"]
            for stock in stocks
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {
                    "available": False,
                    "state": "数据不足",
                    "score": 0,
                    "error": str(exc),
                }
    output = []
    for stock in stocks:
        item = dict(stock)
        behavior = results.get(
            stock["code"],
            {"available": False, "state": "数据不足", "score": 0},
        )
        item["capital_behavior"] = behavior
        previous_score = _number(item.get("score"), 50)
        if behavior.get("available"):
            behavior_score = (behavior["score"] + 100) / 2
            item["score"] = round(
                previous_score * 0.75 + behavior_score * 0.25, 1
            )
            item["reason"] = (
                str(item.get("reason") or "")
                + "；大资金 "
                + behavior["state"]
                + f" {behavior['score']:+.0f}"
            )
        output.append(item)
    return sorted(output, key=lambda item: item.get("score", 0), reverse=True)
