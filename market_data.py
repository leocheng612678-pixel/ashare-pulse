from __future__ import annotations

import json
import gzip
import math
import re
import statistics
import ssl
import sys
import time
import traceback
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from pathlib import Path

try:
    import certifi

    CA_BUNDLE = Path(certifi.where())
except Exception:
    CA_BUNDLE = None

if CA_BUNDLE is None or not CA_BUNDLE.exists():
    bundled = (
        Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
        / "certifi"
        / "cacert.pem"
    )
    if bundled.exists():
        CA_BUNDLE = bundled

try:
    SSL_CONTEXT = (
        ssl.create_default_context(cafile=str(CA_BUNDLE))
        if CA_BUNDLE and CA_BUNDLE.exists()
        else None
    )
except Exception:
    SSL_CONTEXT = None


CHINA_TZ = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)

INDEX_DEFS = [
    ("000001", "上证指数", "s_sh000001"),
    ("399001", "深证成指", "s_sz399001"),
    ("399006", "创业板指", "s_sz399006"),
    ("000016", "上证50", "s_sh000016"),
    ("000300", "沪深300", "s_sh000300"),
    ("000905", "中证500", "s_sh000905"),
    ("000852", "中证1000", "s_sh000852"),
]

FUTURE_DEFS = [
    ("IF", "IF 主力", "nf_IF0", "000300", "沪深300"),
    ("IH", "IH 主力", "nf_IH0", "000016", "上证50"),
    ("IC", "IC 主力", "nf_IC0", "000905", "中证500"),
    ("IM", "IM 主力", "nf_IM0", "000852", "中证1000"),
]

STOCK_FILTER = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


class DataSourceError(RuntimeError):
    pass


def now_china() -> datetime:
    return datetime.now(CHINA_TZ)


def _number(value: Any, default: float | None = None) -> float | None:
    if value in (None, "", "-", "--"):
        return default
    try:
        number = float(value)
        if math.isnan(number) or math.isinf(number):
            return default
        return number
    except (TypeError, ValueError):
        return default


def _fetch_bytes(
    url: str,
    *,
    encoding: str = "utf-8",
    referer: str = "",
    timeout: int = 25,
    retries: int = 4,
) -> str:
    headers = {"User-Agent": UA, "Accept": "*/*"}
    if referer:
        headers["Referer"] = referer

    error: Exception | None = None
    last_trace = ""
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(
                request, timeout=timeout, context=SSL_CONTEXT
            ) as response:
                body = response.read()
                encoding_header = (
                    response.headers.get("Content-Encoding", "").lower()
                )
            if "gzip" in encoding_header:
                body = gzip.decompress(body)
            if not body:
                raise DataSourceError("接口返回空内容")
            return body.decode(encoding)
        except Exception as exc:  # Network failures vary across Windows TLS stacks.
            error = exc
            last_trace = traceback.format_exc()
            time.sleep(0.35 * (attempt + 1))
    raise DataSourceError(
        f"请求失败：{type(error).__name__}: {error!r}\n{last_trace}"
    )


def _fetch_json(
    url: str,
    *,
    referer: str = "https://quote.eastmoney.com/",
    timeout: int = 25,
    retries: int = 4,
) -> dict[str, Any]:
    raw = _fetch_bytes(
        url, referer=referer, timeout=timeout, retries=retries
    )
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"接口返回了无法解析的 JSON：{exc}") from exc


def _parse_sina_vars(raw: str) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for match in re.finditer(r'var\s+hq_str_([^=]+)="([^"]*)";', raw):
        result[match.group(1)] = match.group(2).split(",")
    return result


def parse_sina_indices(raw: str) -> list[dict[str, Any]]:
    values = _parse_sina_vars(raw)
    indices: list[dict[str, Any]] = []
    for code, name, symbol in INDEX_DEFS:
        fields = values.get(symbol)
        if not fields or len(fields) < 6:
            continue
        close = _number(fields[1])
        change = _number(fields[2], 0.0)
        change_pct = _number(fields[3], 0.0)
        volume = _number(fields[4], 0.0)
        amount_10k = _number(fields[5], 0.0)
        if close is None:
            continue
        indices.append(
            {
                "code": code,
                "name": name,
                "close": close,
                "change": change,
                "change_pct": change_pct,
                "volume": volume,
                "amount": (amount_10k or 0.0) * 10_000,
            }
        )
    if not indices:
        raise DataSourceError("新浪指数行情没有有效数据")
    return indices


def fetch_indices() -> list[dict[str, Any]]:
    symbols = ",".join(item[2] for item in INDEX_DEFS)
    raw = _fetch_bytes(
        f"https://hq.sinajs.cn/list={symbols}",
        encoding="gbk",
        referer="https://finance.sina.com.cn/",
    )
    return parse_sina_indices(raw)


def parse_sina_futures(raw: str) -> list[dict[str, Any]]:
    values = _parse_sina_vars(raw)
    futures: list[dict[str, Any]] = []
    for code, name, symbol, spot_code, spot_name in FUTURE_DEFS:
        fields = values.get(symbol)
        if not fields or len(fields) < 9:
            continue

        # Sina's futures snapshot is not documented consistently. Field 8 is the
        # latest price in the current feed; field 3 is a useful fallback.
        latest = _number(fields[8]) or _number(fields[3])
        if latest is None:
            continue
        quote_date = ""
        quote_time = ""
        for value in reversed(fields):
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                quote_date = value
                idx = fields.index(value)
                if idx + 1 < len(fields):
                    quote_time = fields[idx + 1]
                break
        futures.append(
            {
                "code": code,
                "name": name,
                "future_price": latest,
                "spot_code": spot_code,
                "spot_name": spot_name,
                "quote_date": quote_date,
                "quote_time": quote_time,
                "open": _number(fields[0]),
                "high": _number(fields[1]),
                "low": _number(fields[2]),
            }
        )
    if not futures:
        raise DataSourceError("新浪期指行情没有有效数据")
    return futures


def fetch_futures() -> list[dict[str, Any]]:
    symbols = ",".join(item[2] for item in FUTURE_DEFS)
    raw = _fetch_bytes(
        f"https://hq.sinajs.cn/list={symbols}",
        encoding="gbk",
        referer="https://finance.sina.com.cn/",
    )
    return parse_sina_futures(raw)


def _fetch_index_klines(secid: str, limit: int = 80) -> list[dict[str, Any]]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&klt=101&fqt=0&lmt={limit}&end=20500101"
        "&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    )
    payload = _fetch_json(url)
    rows = ((payload.get("data") or {}).get("klines") or [])
    result: list[dict[str, Any]] = []
    for row in rows:
        fields = row.split(",")
        if len(fields) < 11:
            continue
        close = _number(fields[2])
        if close is None:
            continue
        result.append(
            {
                "date": fields[0],
                "open": _number(fields[1]),
                "close": close,
                "high": _number(fields[3]),
                "low": _number(fields[4]),
                "volume": _number(fields[5], 0.0),
                "amount": _number(fields[6], 0.0),
                "change_pct": _number(fields[8], 0.0),
            }
        )
    if not result:
        raise DataSourceError(f"指数 {secid} 没有日线数据")
    return result


def _moving_average(values: list[float], window: int) -> float | None:
    if len(values) < window:
        return None
    return sum(values[-window:]) / window


def _return_pct(values: list[float], days: int) -> float | None:
    if len(values) <= days or values[-days - 1] == 0:
        return None
    return (values[-1] / values[-days - 1] - 1) * 100


def _annualized_vol(values: list[float], days: int = 20) -> float | None:
    if len(values) <= days:
        return None
    changes = [
        values[index] / values[index - 1] - 1
        for index in range(len(values) - days, len(values))
        if values[index - 1]
    ]
    if len(changes) < 2:
        return None
    mean = sum(changes) / len(changes)
    variance = sum((item - mean) ** 2 for item in changes) / (len(changes) - 1)
    return math.sqrt(variance) * math.sqrt(252) * 100


def _build_trend(rows: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [float(row["close"]) for row in rows]
    amounts = [float(row.get("amount") or 0) for row in rows]
    ma20 = _moving_average(closes, 20)
    ma60 = _moving_average(closes, 60)
    ma5 = _moving_average(closes, 5)
    amount_avg20 = _moving_average(amounts, 20)
    return {
        "code": "000300",
        "name": "沪深300",
        "date": rows[-1]["date"],
        "close": closes[-1],
        "change_pct": rows[-1].get("change_pct"),
        "ma5": ma5,
        "ma20": ma20,
        "ma60": ma60,
        "above_ma20_pct": ((closes[-1] / ma20 - 1) * 100) if ma20 else None,
        "return_5d": _return_pct(closes, 5),
        "return_20d": _return_pct(closes, 20),
        "volatility_20d": _annualized_vol(closes),
        "amount_vs_20d": (
            amounts[-1] / amount_avg20 if amount_avg20 and amounts[-1] else None
        ),
    }


def fetch_market_technicals() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=3) as executor:
        future_csi = executor.submit(_fetch_index_klines, "1.000300", 280)
        future_sh = executor.submit(_fetch_index_klines, "1.000001", 200)
        future_sz = executor.submit(_fetch_index_klines, "0.399001", 200)
        csi_rows = future_csi.result()
        sh_rows = future_sh.result()
        sz_rows = future_sz.result()

    sh_amounts = {row["date"]: row.get("amount") or 0 for row in sh_rows}
    sz_amounts = {row["date"]: row.get("amount") or 0 for row in sz_rows}
    common_dates = sorted(set(sh_amounts).intersection(sz_amounts))[-180:]
    turnover_history = [
        {"date": day, "amount": sh_amounts[day] + sz_amounts[day]}
        for day in common_dates
    ]
    amount_avg20 = _moving_average(
        [float(item["amount"]) for item in turnover_history], 20
    )
    latest_turnover = (
        float(turnover_history[-1]["amount"]) if turnover_history else None
    )
    return {
        "trend": _build_trend(csi_rows),
        "csi_history": [
            {
                "date": row["date"],
                "close": row["close"],
                "change_pct": row.get("change_pct"),
            }
            for row in csi_rows[-30:]
        ],
        "csi_full_history": csi_rows,
        "turnover_history": turnover_history,
        "turnover": latest_turnover,
        "turnover_avg20": amount_avg20,
        "turnover_vs_20d": (
            latest_turnover / amount_avg20
            if latest_turnover and amount_avg20
            else None
        ),
    }


def fetch_sina_index_history(
    symbol: str = "sh000300", days: int = 280
) -> list[dict[str, Any]]:
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/"
        "CN_MarketDataService.getKLineData"
        f"?symbol={symbol}&scale=240&ma=no&datalen={days}"
    )
    raw = _fetch_bytes(
        url,
        referer="https://finance.sina.com.cn/",
        retries=5,
    )
    start, end = raw.find("["), raw.rfind("]")
    if start < 0 or end <= start:
        raise DataSourceError("新浪指数历史没有有效数据")
    try:
        source = json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DataSourceError("新浪指数历史解析失败") from exc
    rows = []
    for item in source:
        close = _number(item.get("close"))
        if close is None:
            continue
        rows.append(
            {
                "date": item.get("day"),
                "open": _number(item.get("open")),
                "close": close,
                "high": _number(item.get("high")),
                "low": _number(item.get("low")),
                "volume": _number(item.get("volume"), 0.0),
                "amount": 0.0,
                "change_pct": None,
            }
        )
    if not rows:
        raise DataSourceError("新浪指数历史为空")
    return rows


def _fetch_capital_for(secid: str) -> dict[str, dict[str, float]]:
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get"
        f"?lmt=240&klt=101&secid={secid}"
        "&fields1=f1,f2,f3,f7"
        "&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61,f62,f63"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    rows = ((payload.get("data") or {}).get("klines") or [])
    result: dict[str, dict[str, float]] = {}
    for row in rows:
        fields = row.split(",")
        if len(fields) < 6:
            continue
        result[fields[0]] = {
            "main": _number(fields[1], 0.0) or 0.0,
            "small": _number(fields[2], 0.0) or 0.0,
            "medium": _number(fields[3], 0.0) or 0.0,
            "large": _number(fields[4], 0.0) or 0.0,
            "super": _number(fields[5], 0.0) or 0.0,
        }
    if not result:
        raise DataSourceError(f"资金流接口 {secid} 没有有效数据")
    return result


def fetch_capital_flows() -> dict[str, Any]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        sh_future = executor.submit(_fetch_capital_for, "1.000001")
        sz_future = executor.submit(_fetch_capital_for, "0.399001")
        sh = sh_future.result()
        sz = sz_future.result()

    history = []
    for day in sorted(set(sh).intersection(sz)):
        row = {
            key: (sh[day].get(key, 0.0) + sz[day].get(key, 0.0))
            for key in ("main", "small", "medium", "large", "super")
        }
        row["date"] = day
        history.append(row)
    if not history:
        raise DataSourceError("沪深资金流没有共同交易日")
    return {"history": history}


def _fetch_stock_page(page: int) -> tuple[int, int, list[dict[str, Any]]]:
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        f"?pn={page}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
        f"&fs={STOCK_FILTER}"
        "&fields=f2,f3,f5,f6,f8,f9,f10,f12,f13,f14,f20,f21,f23,f62"
    )
    payload = _fetch_json(url, retries=5)
    data = payload.get("data") or {}
    total = int(_number(data.get("total"), 0) or 0)
    rows = data.get("diff") or []
    if not isinstance(rows, list):
        rows = []
    return page, total, rows


def _is_limit_up(code: str, name: str, change_pct: float) -> bool:
    if code.startswith(("4", "8", "92")):
        threshold = 29.7
    elif name.upper().startswith(("ST", "*ST")):
        threshold = 4.8
    elif code.startswith(("300", "301", "688", "689")):
        threshold = 19.7
    else:
        threshold = 9.7
    return change_pct >= threshold


def _is_limit_down(code: str, name: str, change_pct: float) -> bool:
    if code.startswith(("4", "8", "92")):
        threshold = -29.7
    elif name.upper().startswith(("ST", "*ST")):
        threshold = -4.8
    elif code.startswith(("300", "301", "688", "689")):
        threshold = -19.7
    else:
        threshold = -9.7
    return change_pct <= threshold


def calculate_breadth(stocks: list[dict[str, Any]]) -> dict[str, Any]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for item in stocks:
        code = str(item.get("f12") or "")
        market = str(item.get("f13") or "")
        if code:
            unique[(market, code)] = item

    up = down = flat = suspended = 0
    up_volume = down_volume = flat_volume = 0.0
    up_amount = down_amount = flat_amount = 0.0
    total_amount = total_volume = main_net = 0.0
    limit_up = limit_down = 0
    returns: list[float] = []
    amounts: list[float] = []
    circulating_values: list[float] = []

    for item in unique.values():
        name = str(item.get("f14") or "")
        code = str(item.get("f12") or "")
        close = _number(item.get("f2"))
        change_pct = _number(item.get("f3"))
        volume = _number(item.get("f5"), 0.0) or 0.0
        amount = _number(item.get("f6"), 0.0) or 0.0
        stock_flow = _number(item.get("f62"), 0.0) or 0.0
        total_amount += amount
        total_volume += volume
        main_net += stock_flow
        if amount > 0:
            amounts.append(amount)
        circulating = _number(item.get("f21"))
        if circulating and circulating > 0:
            circulating_values.append(circulating)

        if close is None or close <= 0 or change_pct is None:
            suspended += 1
            continue
        if change_pct > 0.001:
            up += 1
            up_volume += volume
            up_amount += amount
        elif change_pct < -0.001:
            down += 1
            down_volume += volume
            down_amount += amount
        else:
            flat += 1
            flat_volume += volume
            flat_amount += amount
        returns.append(change_pct)
        if _is_limit_up(code, name, change_pct):
            limit_up += 1
        if _is_limit_down(code, name, change_pct):
            limit_down += 1

    active = up + down + flat
    arms = None
    if up and down and up_volume > 0 and down_volume > 0:
        arms = (up / down) / (up_volume / down_volume)
    amount_ratio = (
        up_amount / (up_amount + down_amount)
        if up_amount + down_amount > 0
        else None
    )
    advance_ratio = up / (up + down) if up + down else None
    volume_advance_decline_ratio = (
        up_volume / down_volume if down_volume > 0 else None
    )
    median_return = statistics.median(returns) if returns else None
    mean_return = statistics.fmean(returns) if returns else None
    dispersion = statistics.pstdev(returns) if len(returns) > 1 else None
    top10_amount_share = None
    if amounts:
        ordered = sorted(amounts, reverse=True)
        top10_amount_share = sum(ordered[:10]) / sum(ordered)
    equal_weight_top10_share = None
    if circulating_values:
        ordered_values = sorted(circulating_values, reverse=True)
        equal_weight_top10_share = sum(ordered_values[:10]) / sum(ordered_values)
    return {
        "up": up,
        "down": down,
        "flat": flat,
        "suspended": suspended,
        "active": active,
        "coverage": len(unique),
        "up_volume": up_volume,
        "down_volume": down_volume,
        "flat_volume": flat_volume,
        "up_amount": up_amount,
        "down_amount": down_amount,
        "flat_amount": flat_amount,
        "total_amount": total_amount,
        "total_volume": total_volume,
        "main_net_from_stocks": main_net,
        "advance_ratio": advance_ratio,
        "up_amount_ratio": amount_ratio,
        "arms": arms,
        "limit_up": limit_up,
        "limit_down": limit_down,
        "limit_balance": limit_up - limit_down,
        "up_volume_ratio": (
            up_volume / (up_volume + down_volume)
            if up_volume + down_volume > 0
            else None
        ),
        "median_return": median_return,
        "mean_return": mean_return,
        "return_dispersion": dispersion,
        "top10_amount_share": top10_amount_share,
        "top10_market_cap_share": equal_weight_top10_share,
        "advance_decline_ratio": up / down if down else None,
        "volume_advance_decline_ratio": volume_advance_decline_ratio,
    }


def _candidate_seeds(
    stocks: list[dict[str, Any]], limit: int = 45
) -> list[dict[str, Any]]:
    seeds: dict[str, dict[str, Any]] = {}
    for item in stocks:
        code = str(item.get("f12") or "")
        name = str(item.get("f14") or "")
        close = _number(item.get("f2"))
        change_pct = _number(item.get("f3"))
        amount = _number(item.get("f6"), 0.0) or 0.0
        if (
            len(code) != 6
            or not name
            or name.upper().startswith(("ST", "*ST"))
            or close is None
            or close < 3
            or change_pct is None
            or change_pct < -2.0
            or change_pct > 9.5
            or amount < 3e8
        ):
            continue
        seeds[code] = {
            "code": code,
            "name": name,
            "price": close,
            "change_pct": change_pct,
            "amount": amount,
            "pe": _number(item.get("f9")) or _number(item.get("per")),
            "pb": _number(item.get("f23")) or _number(item.get("pb")),
            "turnover_rate": _number(item.get("f8"))
            or _number(item.get("turnoverratio")),
            "market_cap": _number(item.get("f20")) or _number(item.get("mktcap")),
            "float_market_cap": _number(item.get("f21"))
            or _number(item.get("nmc")),
        }
    return sorted(
        seeds.values(),
        key=lambda row: (row["amount"], max(row["change_pct"], 0)),
        reverse=True,
    )[:limit]


def _fetch_market_breadth_eastmoney() -> dict[str, Any]:
    _, total, first_rows = _fetch_stock_page(1)
    if not first_rows or total <= 0:
        raise DataSourceError("全市场行情列表为空")
    pages = math.ceil(total / 100)

    rows = list(first_rows)
    failed_pages: list[int] = []
    if pages > 1:
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = {
                executor.submit(_fetch_stock_page, page): page
                for page in range(2, pages + 1)
            }
            for future in as_completed(futures):
                page = futures[future]
                try:
                    _, page_total, page_rows = future.result()
                    total = page_total or total
                    rows.extend(page_rows)
                except Exception:
                    failed_pages.append(page)

    result = calculate_breadth(rows)
    result["expected_total"] = total
    result["failed_pages"] = sorted(failed_pages)
    result["complete"] = not failed_pages and len(rows) >= total * 0.95
    result["provider"] = "东方财富"
    result["candidate_seeds"] = _candidate_seeds(rows)
    if len(rows) < total * 0.75:
        raise DataSourceError(
            f"全市场股票仅抓到 {len(rows)}/{total} 条，数据覆盖不足"
        )
    return result


def _fetch_sina_stock_page(page: int) -> list[dict[str, Any]]:
    url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/Market_Center.getHQNodeData"
        f"?page={page}&num=100&sort=changepercent&asc=0"
        "&node=hs_a&symbol=&_s_r_a=page"
    )
    raw = _fetch_bytes(
        url,
        referer="https://vip.stock.finance.sina.com.cn/",
        retries=5,
    )
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise DataSourceError(f"新浪全市场接口解析失败：{exc}") from exc
    if not isinstance(data, list):
        raise DataSourceError("新浪全市场接口没有返回列表")
    return data


def _fetch_market_breadth_sina() -> dict[str, Any]:
    count_url = (
        "https://vip.stock.finance.sina.com.cn/quotes_service/api/"
        "json_v2.php/Market_Center.getHQNodeStockCount?node=hs_a"
    )
    count_raw = _fetch_bytes(
        count_url, referer="https://vip.stock.finance.sina.com.cn/"
    )
    try:
        total = int(json.loads(count_raw))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise DataSourceError("新浪无法取得全市场股票数量") from exc
    if total <= 0:
        raise DataSourceError("新浪全市场股票数量异常")

    pages = math.ceil(total / 100)
    page_results: dict[int, list[dict[str, Any]]] = {}
    failed_pages: list[int] = []
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(_fetch_sina_stock_page, page): page
            for page in range(1, pages + 1)
        }
        for future in as_completed(futures):
            page = futures[future]
            try:
                page_results[page] = future.result()
            except Exception:
                failed_pages.append(page)

    rows: list[dict[str, Any]] = []
    for page in sorted(page_results):
        for item in page_results[page]:
            symbol = str(item.get("symbol") or "")
            rows.append(
                {
                    "f12": str(item.get("code") or symbol[2:]),
                    "f13": symbol[:2],
                    "f14": str(item.get("name") or ""),
                    "f2": _number(item.get("trade")),
                    "f3": _number(item.get("changepercent")),
                    "f5": _number(item.get("volume"), 0.0),
                    "f6": _number(item.get("amount"), 0.0),
                    "f62": 0.0,
                    "per": _number(item.get("per")),
                    "pb": _number(item.get("pb")),
                    "mktcap": _number(item.get("mktcap")),
                    "nmc": _number(item.get("nmc")),
                    "turnoverratio": _number(item.get("turnoverratio")),
                }
            )
    result = calculate_breadth(rows)
    result["expected_total"] = total
    result["failed_pages"] = sorted(failed_pages)
    result["complete"] = not failed_pages and len(rows) >= total * 0.95
    result["provider"] = "新浪财经"
    result["candidate_seeds"] = _candidate_seeds(rows)
    if len(rows) < total * 0.75:
        raise DataSourceError(
            f"新浪全市场股票仅抓到 {len(rows)}/{total} 条，覆盖不足"
        )
    return result


def fetch_market_breadth() -> dict[str, Any]:
    errors: list[str] = []
    try:
        return _fetch_market_breadth_eastmoney()
    except Exception as exc:
        errors.append(f"东方财富：{exc}")
    try:
        return _fetch_market_breadth_sina()
    except Exception as exc:
        errors.append(f"新浪：{exc}")
    raise DataSourceError("；".join(errors))


def _fetch_sectors(direction: str) -> list[dict[str, Any]]:
    po = "1" if direction == "in" else "0"
    url = (
        "https://push2.eastmoney.com/api/qt/clist/get"
        f"?pn=1&pz=30&po={po}&np=1&fltt=2&invt=2&fid=f62"
        "&fs=m:90+t:2&fields=f2,f3,f12,f14,f62,f184"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    rows = (payload.get("data") or {}).get("diff") or []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        name = str(row.get("f14") or "")
        flow = _number(row.get("f62"))
        if not name or flow is None or name in seen:
            continue
        if direction == "in" and flow <= 0:
            continue
        if direction == "out" and flow >= 0:
            continue
        seen.add(name)
        result.append(
            {
                "name": name,
                "change_pct": _number(row.get("f3"), 0.0),
                "main_net": flow,
                "main_net_ratio": _number(row.get("f184"), 0.0),
            }
        )
        if len(result) == 5:
            break
    return result


def fetch_sector_flows() -> dict[str, list[dict[str, Any]]]:
    with ThreadPoolExecutor(max_workers=2) as executor:
        inflow_future = executor.submit(_fetch_sectors, "in")
        outflow_future = executor.submit(_fetch_sectors, "out")
        return {
            "inflow": inflow_future.result(),
            "outflow": outflow_future.result(),
        }


def _source(
    status: str,
    *,
    detail: str = "",
    fetched_at: str | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "detail": detail,
        "fetched_at": fetched_at or now_china().isoformat(timespec="seconds"),
    }


def _attempt(
    label: str,
    sources: dict[str, dict[str, Any]],
    fn: Callable[[], Any],
) -> Any:
    started = time.monotonic()
    try:
        value = fn()
        elapsed = time.monotonic() - started
        sources[label] = _source(
            "ok", detail=f"{elapsed:.1f} 秒"
        )
        return value
    except Exception as exc:
        sources[label] = _source("error", detail=str(exc))
        return None


def fetch_market_snapshot() -> tuple[dict[str, Any], list[dict[str, Any]]]:
    sources: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, Any]] = []

    def attempt(label: str, fn: Callable[[], Any]) -> Any:
        value = _attempt(label, sources, fn)
        if value is None:
            errors.append({"source": label, "message": sources[label]["detail"]})
        return value

    with ThreadPoolExecutor(max_workers=5) as executor:
        jobs = {
            "指数行情": executor.submit(attempt, "指数行情", fetch_indices),
            "期指升贴水": executor.submit(
                attempt, "期指升贴水", fetch_futures
            ),
            "资金流": executor.submit(
                attempt, "资金流", fetch_capital_flows
            ),
            "市场技术面": executor.submit(
                attempt, "市场技术面", fetch_market_technicals
            ),
            "历史样本": executor.submit(
                attempt, "历史样本", fetch_sina_index_history
            ),
        }
        values = {key: future.result() for key, future in jobs.items()}

    # Breadth makes more than 50 requests, so it runs separately to avoid
    # stressing the public endpoint while the other sources are in flight.
    breadth = attempt("涨跌家数", fetch_market_breadth)
    sectors = attempt("行业资金", fetch_sector_flows)

    indices = values["指数行情"] or []
    futures = values["期指升贴水"] or []
    capital = values["资金流"] or {"history": []}
    technicals = values["市场技术面"] or {}
    historical_csi = values["历史样本"] or []
    index_map = {item["code"]: item for item in indices}

    for future in futures:
        spot = index_map.get(future.get("spot_code"))
        if not spot:
            continue
        future["spot"] = spot["close"]
        future["basis"] = future["future_price"] - spot["close"]
        future["basis_pct"] = (
            future["basis"] / spot["close"] * 100 if spot["close"] else None
        )

    trade_dates: list[str] = []
    if futures and futures[0].get("quote_date"):
        trade_dates.append(futures[0]["quote_date"])
    if capital["history"]:
        trade_dates.append(capital["history"][-1]["date"])
    trend_date = (technicals.get("trend") or {}).get("date")
    if trend_date:
        trade_dates.append(trend_date)
    trade_date = max(trade_dates) if trade_dates else now_china().date().isoformat()

    snapshot = {
        "generated_at": now_china().isoformat(timespec="seconds"),
        "trade_date": trade_date,
        "indices": indices,
        "futures": futures,
        "capital": capital,
        "breadth": breadth,
        "technicals": technicals,
        "historical_csi": historical_csi,
        "sectors": sectors or {"inflow": [], "outflow": []},
        "sources": sources,
        "errors": errors,
    }
    return snapshot, errors


if __name__ == "__main__":
    snapshot, errors = fetch_market_snapshot()
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))
    if errors:
        print(json.dumps({"errors": errors}, ensure_ascii=False, indent=2))
