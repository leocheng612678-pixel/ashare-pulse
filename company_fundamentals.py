from __future__ import annotations

import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import timedelta
from typing import Any

from market_data import _fetch_json, _number, now_china


DATACENTER = "https://datacenter-web.eastmoney.com/api/data/v1/get"
F10_SURVEY = "https://emweb.securities.eastmoney.com/PC_HSF10/CompanySurvey/PageAjax"
RESEARCH_REPORTS = "https://reportapi.eastmoney.com/report/list"
ANNOUNCEMENTS = "https://np-anotice-stock.eastmoney.com/api/security/ann"


def _f10_code(code: str) -> str:
    if code.startswith(("4", "8", "9")):
        return "BJ" + code
    if code.startswith("6"):
        return "SH" + code
    return "SZ" + code


def _latest_financials(code: str) -> dict[str, Any]:
    url = (
        f"{DATACENTER}?reportName=RPT_F10_FINANCE_MAINFINADATA"
        "&columns=ALL"
        f'&filter=(SECURITY_CODE%3D%22{code}%22)'
        "&pageNumber=1&pageSize=20&source=WEB&client=WEB"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    rows = ((payload.get("result") or {}).get("data") or [])
    if not rows:
        raise RuntimeError(f"{code} 没有财务数据")
    rows = sorted(
        rows,
        key=lambda row: str(row.get("REPORT_DATE") or ""),
        reverse=True,
    )
    latest = rows[0]
    annual = next(
        (
            row
            for row in rows
            if str(row.get("REPORT_DATE") or "")[:10].endswith("12-31")
        ),
        None,
    )
    profit_growth = _number(latest.get("PARENTNETPROFITTZ"))
    revenue_growth = _number(latest.get("TOTALOPERATEREVETZ"))
    deducted_growth = _number(latest.get("KCFJCXSYJLRTZ"))
    roe = _number(latest.get("ROEJQ"))
    gross_margin = _number(latest.get("XSMLL"))
    debt_ratio = _number(latest.get("ZCFZL"))
    cash_ratio = _number(latest.get("JYXJLYYSR"))
    industry = str(
        latest.get("PUBLISHNAME")
        or latest.get("BOARD_NAME")
        or ""
    )

    score = 50.0
    risks: list[dict[str, str]] = []
    if roe is not None:
        score += 12 if roe >= 15 else 7 if roe >= 10 else -12 if roe < 0 else 0
        if roe < 5:
            risks.append({"level": "high", "text": "ROE偏低"})
    if revenue_growth is not None:
        score += 8 if revenue_growth >= 10 else 2 if revenue_growth >= 0 else -10
        if revenue_growth < 0:
            risks.append({"level": "medium", "text": "营收同比下降"})
    if profit_growth is not None:
        score += 10 if profit_growth >= 10 else 3 if profit_growth >= 0 else -12
        if profit_growth < 0:
            risks.append({"level": "high", "text": "净利润同比下降"})
    if deducted_growth is not None:
        score += 6 if deducted_growth >= 10 else -8 if deducted_growth < -10 else 0
        if deducted_growth < 0:
            risks.append({"level": "medium", "text": "扣非利润同比下降"})
    if cash_ratio is not None:
        score += 5 if cash_ratio > 0 else -5
        if cash_ratio < 0:
            risks.append({"level": "high", "text": "经营现金流偏弱"})

    is_financial = any(
        word in industry for word in ("银行", "保险", "证券", "金融")
    )
    if debt_ratio is not None and not is_financial:
        score += -10 if debt_ratio > 75 else -4 if debt_ratio > 65 else 3
        if debt_ratio > 75:
            risks.append({"level": "high", "text": "资产负债率偏高"})

    history = []
    for row in rows[:6]:
        history.append(
            {
                "report_date": str(row.get("REPORT_DATE") or "")[:10],
                "report_name": row.get("REPORT_DATE_NAME"),
                "revenue_growth": _number(row.get("TOTALOPERATEREVETZ")),
                "profit_growth": _number(row.get("PARENTNETPROFITTZ")),
                "roe": _number(row.get("ROEJQ")),
            }
        )

    return {
        "code": code,
        "report_date": str(latest.get("REPORT_DATE") or "")[:10],
        "report_name": latest.get("REPORT_DATE_NAME"),
        "notice_date": str(latest.get("NOTICE_DATE") or "")[:10],
        "industry": industry,
        "revenue": _number(latest.get("TOTALOPERATEREVE")),
        "revenue_growth": revenue_growth,
        "net_profit": _number(latest.get("PARENTNETPROFIT")),
        "profit_growth": profit_growth,
        "deducted_profit": _number(latest.get("KCFJCXSYJLR")),
        "deducted_growth": deducted_growth,
        "roe": roe,
        "gross_margin": gross_margin,
        "net_margin": _number(latest.get("XSJLL")),
        "debt_ratio": debt_ratio,
        "cash_flow_ratio": cash_ratio,
        "eps": _number(latest.get("EPSJB")),
        "annual_eps": _number((annual or {}).get("EPSJB")),
        "annual_report_date": str((annual or {}).get("REPORT_DATE") or "")[:10],
        "quality_score": max(0.0, min(100.0, score)),
        "risks": risks[:5],
        "history": history,
        "source": "东方财富财务数据",
    }


def _company_survey(code: str) -> dict[str, Any]:
    url = f"{F10_SURVEY}?code={_f10_code(code)}"
    payload = _fetch_json(
        url,
        referer="https://emweb.securities.eastmoney.com/",
    )
    basic = ((payload.get("jbzl") or [{}])[0])
    profile = str(basic.get("ORG_PROFILE") or "").replace("\r", " ").strip()
    scope = str(basic.get("BUSINESS_SCOPE") or "").replace("\r", " ").strip()
    return {
        "company_name": basic.get("ORG_NAME"),
        "industry": basic.get("EM2016") or basic.get("INDUSTRYCSRC1"),
        "profile": profile[:360],
        "business_scope": scope[:300],
        "website": basic.get("ORG_WEB"),
        "employees": _number(basic.get("EMP_NUM")),
        "chairman": basic.get("CHAIRMAN"),
        "source": "东方财富公司概况",
    }


def _research_forecast(code: str) -> dict[str, Any]:
    today = now_china().date()
    begin = today - timedelta(days=365)
    end = today + timedelta(days=30)
    url = (
        f"{RESEARCH_REPORTS}?pageSize=20&pageNo=1&qType=0&code={code}"
        "&industryCode=*&industry=*&rating=&ratingChange="
        f"&beginTime={begin.isoformat()}&endTime={end.isoformat()}"
        "&fields=&p=1&pageNum=1"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    reports = payload.get("data") or []
    if not reports:
        return {
            "available": False,
            "source": "东方财富券商研报",
            "reason": "最近一年没有可用的券商盈利预测",
        }

    def median_field(field: str) -> float | None:
        values = [
            _number(item.get(field))
            for item in reports
            if _number(item.get(field)) is not None
        ]
        return statistics.median(values) if values else None

    eps_current = median_field("predictThisYearEps")
    eps_next = median_field("predictNextYearEps")
    eps_next2 = median_field("predictNextTwoYearEps")
    dispersion = None
    current_values = [
        _number(item.get("predictThisYearEps"))
        for item in reports
        if _number(item.get("predictThisYearEps")) is not None
    ]
    if len(current_values) >= 2 and statistics.fmean(current_values) > 0:
        dispersion = (
            statistics.pstdev(current_values)
            / statistics.fmean(current_values)
            * 100
        )
    ratings: dict[str, int] = {}
    for item in reports:
        rating = str(item.get("emRatingName") or "未评级")
        ratings[rating] = ratings.get(rating, 0) + 1
    latest_reports = [
        {
            "title": item.get("title"),
            "organization": item.get("orgSName"),
            "date": str(item.get("publishDate") or "")[:10],
            "rating": item.get("emRatingName"),
            "eps_current": _number(item.get("predictThisYearEps")),
            "eps_next": _number(item.get("predictNextYearEps")),
        }
        for item in reports[:3]
    ]
    return {
        "available": True,
        "sample_size": len(reports),
        "total_hits": payload.get("hits"),
        "eps_current_year": eps_current,
        "eps_next_year": eps_next,
        "eps_next_two_year": eps_next2,
        "eps_dispersion_pct": dispersion,
        "ratings": ratings,
        "latest_reports": latest_reports,
        "source": "东方财富券商研报",
        "note": "预测值来自券商研究，不代表公司公告或收益承诺",
    }


def _announcement_risk(code: str) -> dict[str, Any]:
    url = (
        f"{ANNOUNCEMENTS}?sr=-1&page_size=20&page_index=1"
        f"&ann_type=A&client_source=web&stock_list={code}"
    )
    payload = _fetch_json(url, referer="https://data.eastmoney.com/")
    rows = ((payload.get("data") or {}).get("list") or [])
    high_risk = (
        "立案",
        "退市",
        "处罚",
        "预亏",
        "下修",
        "终止",
        "诉讼",
        "仲裁",
        "调查",
        "风险提示",
    )
    medium_risk = (
        "减持",
        "解禁",
        "质押",
        "问询",
        "监管",
        "异常波动",
        "业绩下降",
    )
    positive = (
        "回购",
        "增持",
        "中标",
        "订单",
        "预增",
        "分红",
        "股权激励",
    )
    risk_score = 50.0
    events = []
    for row in rows[:12]:
        title = str(row.get("title_ch") or row.get("title") or "")
        date = str(row.get("notice_date") or row.get("display_time") or "")[:10]
        flags = []
        if any(word in title for word in high_risk):
            risk_score += 10
            flags.append("高风险")
        if any(word in title for word in medium_risk):
            risk_score += 5
            flags.append("关注")
        if any(word in title for word in positive):
            risk_score -= 3
            flags.append("正面")
        events.append(
            {
                "date": date,
                "title": title,
                "flags": flags,
            }
        )
    risk_score = max(0.0, min(100.0, risk_score))
    return {
        "available": bool(rows),
        "risk_score": risk_score,
        "label": (
            "高风险"
            if risk_score >= 75
            else "需关注" if risk_score >= 60 else "低风险"
        ),
        "events": events[:8],
        "source": "东方财富公告",
    }


def collect_fundamentals(code: str) -> dict[str, Any]:
    financial = _latest_financials(code)
    survey = _company_survey(code)
    forecast = _research_forecast(code)
    try:
        announcements = _announcement_risk(code)
    except Exception as exc:
        announcements = {
            "available": False,
            "risk_score": 50.0,
            "label": "数据不足",
            "events": [],
            "error": str(exc),
        }
    annual_eps = financial.get("annual_eps")
    eps_current = forecast.get("eps_current_year")
    implied_growth = (
        (eps_current / annual_eps - 1) * 100
        if eps_current is not None and annual_eps and annual_eps > 0
        else None
    )
    forecast["implied_eps_growth"] = implied_growth
    if financial.get("industry"):
        survey["industry"] = survey.get("industry") or financial["industry"]
    return {
        "available": True,
        "financial": financial,
        "company": survey,
        "forecast": forecast,
        "announcements": announcements,
    }


def enrich_stocks_with_fundamentals(
    stocks: list[dict[str, Any]], max_workers: int = 3
) -> list[dict[str, Any]]:
    if not stocks:
        return stocks
    results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(collect_fundamentals, stock["code"]): stock["code"]
            for stock in stocks
        }
        for future in as_completed(futures):
            code = futures[future]
            try:
                results[code] = future.result()
            except Exception as exc:
                results[code] = {
                    "available": False,
                    "error": str(exc),
                }
    output = []
    for stock in stocks:
        item = dict(stock)
        enrichment = results.get(
            stock["code"], {"available": False, "error": "数据缺失"}
        )
        item["company_fundamentals"] = enrichment
        technical_score = _number(item.get("score"), 50.0)
        financial = enrichment.get("financial") or {}
        forecast = enrichment.get("forecast") or {}
        financial_score = _number(financial.get("quality_score"), 50.0)
        implied_growth = _number(forecast.get("implied_eps_growth"), 0.0)
        forecast_score = 50.0 + max(-30.0, min(30.0, implied_growth)) * 1.5
        dispersion = _number(forecast.get("eps_dispersion_pct"), 0.0)
        if dispersion > 40:
            forecast_score -= 6.0
        forecast_score = max(0.0, min(100.0, forecast_score))
        announcements = enrichment.get("announcements") or {}
        announcement_risk = _number(announcements.get("risk_score"), 50.0)
        item["technical_score"] = technical_score
        item["fundamental_score"] = financial_score
        item["forecast_score"] = forecast_score
        item["event_risk_score"] = announcement_risk
        if enrichment.get("available") and forecast.get("available"):
            composite = (
                technical_score * 0.60
                + financial_score * 0.25
                + forecast_score * 0.15
            )
        elif enrichment.get("available"):
            composite = technical_score * 0.70 + financial_score * 0.30
        else:
            composite = technical_score
        composite -= max(0.0, announcement_risk - 50.0) * 0.10
        item["score"] = round(composite, 1)
        item["reason"] = (
            str(item.get("reason") or "")
            + f"；财务质量 {financial_score:.0f}；前景 {forecast_score:.0f}"
        )
        output.append(item)
    return sorted(output, key=lambda item: item.get("score", 0), reverse=True)
