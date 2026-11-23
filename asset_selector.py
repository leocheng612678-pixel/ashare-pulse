from __future__ import annotations

import json
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from market_data import DataSourceError, _fetch_bytes, _number


ETF_UNIVERSE = [
    ("510300", "沪深300ETF"),
    ("510500", "中证500ETF"),
    ("159915", "创业板ETF"),
    ("588000", "科创50ETF"),
    ("512100", "中证1000ETF"),
    ("510050", "上证50ETF"),
]


def normalize_code(value: str) -> str:
    code = re.sub(r"[^0-9]", "", str(value or ""))
    if len(code) != 6:
        raise ValueError(f"证券代码必须是 6 位数字：{value}")
    return code


def sina_symbol(code: str) -> str:
    code = normalize_code(code)
    if code.startswith(("4", "8", "9")):
        return f"bj{code}"
    if code.startswith(("5", "6")):
        return f"sh{code}"
    return f"sz{code}"


def is_etf(code: str) -> bool:
    code = normalize_code(code)
    return code.startswith(("5", "15", "16", "18"))


def _parse_quote_vars(raw: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for match in re.finditer(r'var\s+hq_str_([^=]+)="([^"]*)";', raw):
        result[match.group(1)] = match.group(2).split(",")
    return result


def fetch_quotes(codes: list[str]) -> dict[str, dict[str, Any]]:
    normalized = [normalize_code(code) for code in codes]
    symbols = [sina_symbol(code) for code in normalized]
    raw = _fetch_bytes(
        f"https://hq.sinajs.cn/list={','.join(symbols)}",
        encoding="gbk",
        referer="https://finance.sina.com.cn/",
        retries=4,
    )
    fields_by_symbol = _parse_quote_vars(raw)
    result: dict[str, dict[str, Any]] = {}
    for code, symbol in zip(normalized, symbols):
        fields = fields_by_symbol.get(symbol)
        if not fields or len(fields) < 10:
            continue
        current = _number(fields[3])
        previous = _number(fields[2])
        if current is None or current <= 0:
            continue
        change_pct = (
            (current / previous - 1) * 100 if previous and previous > 0 else None
        )
        result[code] = {
            "code": code,
            "symbol": symbol,
            "name": fields[0],
            "is_etf": is_etf(code),
            "open": _number(fields[1]),
            "previous_close": previous,
            "price": current,
            "change_pct": change_pct,
            "high": _number(fields[4]),
            "low": _number(fields[5]),
            "volume": _number(fields[8], 0.0),
            "amount": _number(fields[9], 0.0),
        }
    return result


def fetch_history(code: str, days: int = 70) -> list[dict[str, Any]]:
    symbol = sina_symbol(code)
    if not symbol.startswith("bj"):
        tencent_url = (
            "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?param={symbol},day,,,{days},qfq"
        )
        try:
            raw_json = _fetch_bytes(
                tencent_url,
                referer="https://gu.qq.com/",
                retries=3,
            )
            payload = json.loads(raw_json)
            bucket = (payload.get("data") or {}).get(symbol) or {}
            rows = bucket.get("qfqday") or bucket.get("day") or []
            result: list[dict[str, Any]] = []
            for row in rows:
                if len(row) < 6:
                    continue
                close = _number(row[2])
                if close is None:
                    continue
                result.append(
                    {
                        "date": row[0],
                        "open": _number(row[1]),
                        "close": close,
                        "high": _number(row[3]),
                        "low": _number(row[4]),
                        "volume": _number(row[5], 0.0),
                        "price_adjustment": "qfq",
                    }
                )
            if result:
                return result
        except Exception:
            pass
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/"
        "CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale=240&ma=no&datalen={days}"
    )
    raw = _fetch_bytes(
        url,
        referer="https://finance.sina.com.cn/",
        retries=4,
    )
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise DataSourceError(f"{code} 没有可用日线")
    try:
        rows = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"{code} 日线解析失败") from exc
    result: list[dict[str, Any]] = []
    for row in rows:
        close = _number(row.get("close"))
        if close is None:
            continue
        result.append(
            {
                "date": row.get("day"),
                "open": _number(row.get("open")),
                "high": _number(row.get("high")),
                "low": _number(row.get("low")),
                "close": close,
                "volume": _number(row.get("volume"), 0.0),
                "price_adjustment": "unadjusted_fallback",
            }
        )
    if not result:
        raise DataSourceError(f"{code} 日线为空")
    return result


def _average(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _return_pct(values: list[float], days: int) -> float | None:
    if len(values) <= days or values[-days - 1] == 0:
        return None
    return (values[-1] / values[-days - 1] - 1) * 100


def _volatility_pct(values: list[float], days: int = 20) -> float | None:
    if len(values) <= days:
        return None
    returns = [
        values[index] / values[index - 1] - 1
        for index in range(len(values) - days, len(values))
        if values[index - 1]
    ]
    if len(returns) < 2:
        return None
    mean = sum(returns) / len(returns)
    variance = sum((item - mean) ** 2 for item in returns) / (len(returns) - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100


def evaluate_asset(
    code: str,
    quote: dict[str, Any],
    history: list[dict[str, Any]],
    *,
    market_score: float,
    role: str,
) -> dict[str, Any]:
    closes = [float(row["close"]) for row in history]
    current = quote.get("price") or closes[-1]
    ma20 = _average(closes[-20:])
    ma60 = _average(closes[-60:])
    ret5 = _return_pct(closes, 5)
    ret20 = _return_pct(closes, 20)
    volatility = _volatility_pct(closes)
    amount = _number(quote.get("amount"), 0.0) or 0.0
    liquid = amount >= 2e8
    trend_up = bool(
        ma20
        and ma60
        and current > ma20
        and ma20 > ma60
        and (ret5 or 0) > 0
    )
    trend_down = bool(ma20 and current < ma20)

    if role == "watch":
        if market_score <= -15:
            status = "不买"
            reason = "市场评分未进入偏多区间"
        elif trend_up and liquid:
            status = "候选"
            reason = "位于20/60日线上方且成交活跃"
        elif trend_down:
            status = "剔除"
            reason = "跌破20日线"
        else:
            status = "观察"
            reason = "趋势尚未形成多头排列"
    else:
        if not ma20 or not ma60:
            status = "观察"
            reason = "历史日线不足，暂不调整"
        elif current < ma20 and ma20 < ma60:
            status = "卖出/清仓"
            reason = "价格位于20日线下方且中期均线走弱"
        elif current < ma20 and market_score <= -45:
            status = "减仓"
            reason = "弱市中跌破20日线"
        elif current < ma20:
            status = "减仓"
            reason = "跌破20日线，先降低风险"
        elif trend_up:
            status = "持有"
            reason = "20/60日线保持多头结构"
        else:
            status = "持有观察"
            reason = "位于20日线上方，但趋势强度不足"

    return {
        **quote,
        "ma20": ma20,
        "ma60": ma60,
        "distance_ma20_pct": (
            (current / ma20 - 1) * 100 if ma20 and current else None
        ),
        "return_5d": ret5,
        "return_20d": ret20,
        "volatility_20d": volatility,
        "liquid": liquid,
        "trend_up": trend_up,
        "status": status,
        "reason": reason,
        "role": role,
    }


def evaluate_codes(
    codes: list[str],
    *,
    market_score: float,
    role: str = "holdings",
    limit: int = 10,
) -> list[dict[str, Any]]:
    normalized: list[str] = []
    for code in codes:
        try:
            item = normalize_code(code)
        except ValueError:
            continue
        if item not in normalized:
            normalized.append(item)
        if len(normalized) >= limit:
            break
    if not normalized:
        return []

    quotes = fetch_quotes(normalized)
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(fetch_history, code): code
            for code in normalized
            if code in quotes
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                history = future.result()
                results.append(
                    evaluate_asset(
                        code,
                        quotes[code],
                        history,
                        market_score=market_score,
                        role=role,
                    )
                )
            except Exception:
                quote = quotes[code]
                results.append(
                    {
                        **quote,
                        "status": "数据不足",
                        "reason": "无法获取足够历史日线",
                        "role": role,
                    }
                )
    order = {code: index for index, code in enumerate(normalized)}
    return sorted(results, key=lambda item: order.get(item["code"], 999))


def choose_etf_candidates(market_score: float) -> list[dict[str, Any]]:
    if market_score < 15:
        return []
    results = evaluate_codes(
        [code for code, _ in ETF_UNIVERSE],
        market_score=market_score,
        role="watch",
        limit=6,
    )
    qualified = [
        item
        for item in results
        if item.get("status") == "候选" and item.get("trend_up")
    ]
    qualified.sort(
        key=lambda item: (
            (item.get("return_5d") or 0) * 0.6
            + (item.get("return_20d") or 0) * 0.4
            - (item.get("volatility_20d") or 0) * 0.03
        ),
        reverse=True,
    )
    return qualified[:2]


def build_asset_decision(strategy: dict[str, Any]) -> dict[str, Any]:
    score = float(strategy.get("score") or 0)
    exposure = strategy.get("exposure") or {}
    if score <= -45:
        choice = "空仓"
        cash_instruction = (
            f"权益仓位目标 {exposure.get('low', 0)}%—"
            f"{exposure.get('high', 10)}%，不新增股票或 ETF。"
        )
        reason = "趋势、资金、广度和期指同时偏弱，优先保护本金。"
    elif score <= -15:
        choice = "现金为主"
        cash_instruction = "仅保留试探仓，不主动选股。"
        reason = "市场尚未形成可交易的多头结构。"
    elif score < 15:
        choice = "持有观察"
        cash_instruction = "维持低仓，等待放量突破或资金转正。"
        reason = "信号分歧，追涨赔率不足。"
    elif score < 45:
        choice = "宽基ETF分批"
        cash_instruction = "只考虑流动性好的宽基 ETF，分三档建仓。"
        reason = "市场转强，但尚未达到全面进攻条件。"
    else:
        choice = "ETF优先、股票滞后"
        cash_instruction = "ETF先确认，个股只在自身趋势同步转强后参与。"
        reason = "多头共振，但仍避免单一股票集中风险。"
    return {
        "choice": choice,
        "reason": reason,
        "cash_instruction": cash_instruction,
        "target_low": exposure.get("low"),
        "target_high": exposure.get("high"),
        "buy_now": False if score < 15 else True,
        "stock_policy": (
            "主动选股持续运行；市场防守时只生成观察名单，不执行买入。"
            if score < 15
            else "主动候选仍需满足20日线触发条件后才分批执行。"
        ),
        "sell_policy": "录入实际持仓后，按20/60日线与市场评分给出减仓或清仓状态。",
    }
