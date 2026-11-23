from __future__ import annotations

import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from storage import DATA_DIR, now_china


LEARNING_DIR = DATA_DIR / "learning"
PREDICTIONS_PATH = LEARNING_DIR / "predictions.json"
MODEL_STATE_PATH = DATA_DIR / "adaptive_model.json"

DEFAULT_WEIGHTS = {
    "trend": 30.0,
    "flow": 30.0,
    "breadth": 30.0,
    "futures": 10.0,
}
WEIGHT_BOUNDS = {
    "trend": (15.0, 45.0),
    "flow": (15.0, 45.0),
    "breadth": (15.0, 45.0),
    "futures": (5.0, 20.0),
}
MIN_SAMPLES = 20


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def load_model_state() -> dict[str, Any]:
    state = _read_json(MODEL_STATE_PATH, {})
    weights = dict(DEFAULT_WEIGHTS)
    if isinstance(state.get("weights"), dict):
        for key in weights:
            try:
                value = float(state["weights"].get(key, weights[key]))
                low, high = WEIGHT_BOUNDS[key]
                weights[key] = max(low, min(high, value))
            except (TypeError, ValueError):
                pass
    return {
        "version": 1,
        "weights": weights,
        "baseline_weights": dict(DEFAULT_WEIGHTS),
        "update_count": int(state.get("update_count") or 0),
        "last_adapted": state.get("last_adapted"),
        "metrics": state.get("metrics") or {},
    }


def save_model_state(state: dict[str, Any]) -> None:
    _write_json(MODEL_STATE_PATH, state)


def _prediction_rows() -> list[dict[str, Any]]:
    value = _read_json(PREDICTIONS_PATH, [])
    return value if isinstance(value, list) else []


def _save_predictions(rows: list[dict[str, Any]]) -> None:
    _write_json(PREDICTIONS_PATH, rows[-500:])


def record_prediction(snapshot: dict[str, Any]) -> dict[str, Any]:
    trade_date = snapshot.get("trade_date")
    strategy = snapshot.get("strategy") or {}
    trend = (snapshot.get("technicals") or {}).get("trend") or {}
    if not trade_date:
        return {"recorded": False, "reason": "缺少交易日期"}
    factor_scores = {
        str(item.get("key")): float(item.get("score") or 0)
        for item in strategy.get("factors") or []
    }
    rows = _prediction_rows()
    candidates = [
        {
            "code": stock.get("code"),
            "name": stock.get("name"),
            "price": float(stock.get("price") or 0),
            "score": float(stock.get("score") or 0),
        }
        for stock in (
            (snapshot.get("active_stocks") or {}).get("stocks") or []
        )[:6]
        if stock.get("code") and stock.get("price")
    ]
    existing = next(
        (row for row in rows if row.get("trade_date") == trade_date),
        None,
    )
    prediction = {
        "trade_date": trade_date,
        "recorded_at": now_china().isoformat(timespec="seconds"),
        "score": float(strategy.get("score") or 0),
        "regime": strategy.get("regime"),
        "factor_scores": factor_scores,
        "index_close": float(trend.get("close") or 0),
        "framework_state": (snapshot.get("framework") or {}).get("state"),
        "candidates": candidates,
    }
    if existing:
        if not existing.get("candidates") and candidates:
            existing["candidates"] = candidates
            _save_predictions(rows)
            return {"recorded": True, "updated_candidates": len(candidates)}
        return {"recorded": False, "reason": "该交易日预测已记录"}
    rows.append(prediction)
    rows.sort(key=lambda row: str(row.get("trade_date") or ""))
    _save_predictions(rows)
    return {"recorded": True, "count": len(rows)}


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    mean_x = sum(xs) / len(xs)
    mean_y = sum(ys) / len(ys)
    numerator = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    var_x = sum((x - mean_x) ** 2 for x in xs)
    var_y = sum((y - mean_y) ** 2 for y in ys)
    if var_x <= 0 or var_y <= 0:
        return None
    return numerator / math.sqrt(var_x * var_y)


def attach_outcomes(snapshot: dict[str, Any]) -> dict[str, Any]:
    history = snapshot.get("historical_csi") or (
        (snapshot.get("technicals") or {}).get("csi_full_history") or []
    )
    closes = {
        str(row.get("date")): float(row.get("close"))
        for row in history
        if row.get("date") and row.get("close") is not None
    }
    dates = sorted(closes)
    index_by_date = {day: index for index, day in enumerate(dates)}
    rows = _prediction_rows()
    updated = 0
    stock_history: dict[str, dict[str, float]] = {}
    for row in rows:
        day = str(row.get("trade_date") or "")
        if day not in index_by_date:
            continue
        index = index_by_date[day]
        base = float(row.get("index_close") or closes[day])
        if not base:
            continue
        if index + 5 < len(dates) and row.get("actual_5d") is None:
            row["actual_5d"] = (
                closes[dates[index + 5]] / base - 1
            ) * 100
            updated += 1
        if index + 20 < len(dates) and row.get("actual_20d") is None:
            row["actual_20d"] = (
                closes[dates[index + 20]] / base - 1
            ) * 100
            updated += 1
        candidates = row.get("candidates") or []
        if not candidates:
            continue
        future_5_date = dates[index + 5] if index + 5 < len(dates) else None
        future_20_date = dates[index + 20] if index + 20 < len(dates) else None
        if not future_5_date and not future_20_date:
            continue
        if any(
            candidate.get("actual_5d") is None
            or candidate.get("actual_20d") is None
            for candidate in candidates
        ):
            try:
                from asset_selector import fetch_history
            except Exception:
                fetch_history = None
            if fetch_history:
                for candidate in candidates:
                    code = candidate.get("code")
                    if not code or code in stock_history:
                        continue
                    try:
                        stock_rows = fetch_history(code, 160)
                        stock_history[code] = {
                            str(item.get("date")): float(item.get("close"))
                            for item in stock_rows
                            if item.get("date") and item.get("close") is not None
                        }
                    except Exception:
                        stock_history[code] = {}
                for candidate in candidates:
                    prices = stock_history.get(candidate.get("code")) or {}
                    entry = float(candidate.get("price") or 0)
                    if entry <= 0:
                        continue
                    if future_5_date and future_5_date in prices:
                        candidate["actual_5d"] = (
                            prices[future_5_date] / entry - 1
                        ) * 100
                    if future_20_date and future_20_date in prices:
                        candidate["actual_20d"] = (
                            prices[future_20_date] / entry - 1
                        ) * 100
                five_values = [
                    float(item["actual_5d"])
                    for item in candidates
                    if item.get("actual_5d") is not None
                ]
                twenty_values = [
                    float(item["actual_20d"])
                    for item in candidates
                    if item.get("actual_20d") is not None
                ]
                if five_values:
                    row["candidate_portfolio_5d"] = statistics.fmean(five_values)
                    if row.get("actual_5d") is not None:
                        row["excess_vs_csi_5d"] = (
                            row["candidate_portfolio_5d"] - row["actual_5d"]
                        )
                if twenty_values:
                    row["candidate_portfolio_20d"] = statistics.fmean(
                        twenty_values
                    )
                    if row.get("actual_20d") is not None:
                        row["excess_vs_csi_20d"] = (
                            row["candidate_portfolio_20d"] - row["actual_20d"]
                        )
                updated += 1
    if updated:
        _save_predictions(rows)
    return {"updated_outcomes": updated, "predictions": len(rows)}


def _bounded_weights(raw: dict[str, float]) -> dict[str, float]:
    bounded: dict[str, float] = {}
    for key, value in raw.items():
        low, high = WEIGHT_BOUNDS[key]
        bounded[key] = max(low, min(high, value))
    for _ in range(20):
        difference = 100.0 - sum(bounded.values())
        if abs(difference) < 1e-9:
            break
        adjustable = [
            key
            for key, value in bounded.items()
            if (
                difference > 0
                and value < WEIGHT_BOUNDS[key][1] - 1e-9
            )
            or (
                difference < 0
                and value > WEIGHT_BOUNDS[key][0] + 1e-9
            )
        ]
        if not adjustable:
            break
        share = difference / len(adjustable)
        for key in adjustable:
            low, high = WEIGHT_BOUNDS[key]
            bounded[key] = max(low, min(high, bounded[key] + share))
    return {key: round(value, 6) for key, value in bounded.items()}


def evaluate_and_adapt(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = load_model_state()
    rows = _prediction_rows()
    completed = [
        row
        for row in rows
        if row.get("actual_5d") is not None and row.get("factor_scores")
    ][-60:]
    metrics: dict[str, Any] = {
        "sample_count": len(completed),
        "minimum_required": MIN_SAMPLES,
        "ic": {},
        "hit_rate_5d": None,
        "directional_median_5d": None,
        "circuit_breaker": False,
    }
    if completed:
        actual = [float(row["actual_5d"]) for row in completed]
        directional = [
            (float(row.get("score") or 0), value)
            for row, value in zip(completed, actual)
            if abs(float(row.get("score") or 0)) > 0.01
        ]
        if directional:
            metrics["hit_rate_5d"] = (
                sum((score > 0) == (value > 0) for score, value in directional)
                / len(directional)
                * 100
            )
            directional_returns = [
                value if score > 0 else -value
                for score, value in directional
            ]
            metrics["directional_median_5d"] = statistics.median(
                directional_returns
            )
            metrics["circuit_breaker"] = (
                len(directional) >= MIN_SAMPLES
                and (
                    metrics["hit_rate_5d"] < 45
                    or metrics["directional_median_5d"] < 0
                )
            )
        for key in DEFAULT_WEIGHTS:
            factor_values = [
                float((row.get("factor_scores") or {}).get(key) or 0)
                for row in completed
            ]
            metrics["ic"][key] = _pearson(factor_values, actual)

    trade_date = str(snapshot.get("trade_date") or "")
    last = state.get("last_adapted")
    can_adapt = len(completed) >= MIN_SAMPLES
    if can_adapt and last:
        try:
            elapsed = (
                datetime.strptime(trade_date, "%Y-%m-%d")
                - datetime.strptime(str(last)[:10], "%Y-%m-%d")
            ).days
            can_adapt = elapsed >= 5
        except ValueError:
            can_adapt = True

    adapted = False
    if can_adapt:
        current = state["weights"]
        target_raw = {}
        for key, base in DEFAULT_WEIGHTS.items():
            ic = metrics["ic"].get(key)
            multiplier = 1.0 + (0.25 * float(ic) if ic is not None else 0.0)
            target_raw[key] = base * max(0.6, min(1.4, multiplier))
        target = _bounded_weights(target_raw)
        proposed = _bounded_weights(
            {
                key: current[key] * 0.80 + target[key] * 0.20
                for key in DEFAULT_WEIGHTS
            }
        )
        capped = {}
        for key, value in proposed.items():
            low, high = WEIGHT_BOUNDS[key]
            max_step = 2.0
            capped[key] = max(
                low,
                min(
                    high,
                    max(current[key] - max_step, min(current[key] + max_step, value)),
                ),
            )
        state["weights"] = _bounded_weights(capped)
        state["update_count"] = int(state.get("update_count") or 0) + 1
        state["last_adapted"] = trade_date
        adapted = True

    state["metrics"] = metrics
    save_model_state(state)
    return {
        "adapted": adapted,
        "weights": state["weights"],
        "update_count": state["update_count"],
        "last_adapted": state.get("last_adapted"),
        "metrics": metrics,
    }


def model_view(snapshot: dict[str, Any]) -> dict[str, Any]:
    state = load_model_state()
    rows = _prediction_rows()
    completed_5 = sum(row.get("actual_5d") is not None for row in rows)
    completed_20 = sum(row.get("actual_20d") is not None for row in rows)
    stock_rows_5 = [
        row for row in rows if row.get("candidate_portfolio_5d") is not None
    ]
    stock_rows_20 = [
        row for row in rows if row.get("candidate_portfolio_20d") is not None
    ]
    return {
        "version": state["version"],
        "weights": state["weights"],
        "baseline_weights": state["baseline_weights"],
        "update_count": state["update_count"],
        "last_adapted": state.get("last_adapted"),
        "prediction_count": len(rows),
        "completed_5d": completed_5,
        "completed_20d": completed_20,
        "stock_completed_5d": len(stock_rows_5),
        "stock_completed_20d": len(stock_rows_20),
        "average_candidate_return_5d": (
            statistics.fmean(
                float(row["candidate_portfolio_5d"]) for row in stock_rows_5
            )
            if stock_rows_5
            else None
        ),
        "average_excess_vs_csi_5d": (
            statistics.fmean(
                float(row["excess_vs_csi_5d"])
                for row in stock_rows_5
                if row.get("excess_vs_csi_5d") is not None
            )
            if any(row.get("excess_vs_csi_5d") is not None for row in stock_rows_5)
            else None
        ),
        "minimum_required": MIN_SAMPLES,
        "metrics": state.get("metrics") or {},
        "status": (
            "模型熔断，暂停新增交易"
            if (state.get("metrics") or {}).get("circuit_breaker")
            else "持续学习中"
            if completed_5 >= MIN_SAMPLES
            else f"积累样本 {completed_5}/{MIN_SAMPLES}"
        ),
        "circuit_breaker": bool(
            (state.get("metrics") or {}).get("circuit_breaker")
        ),
        "guardrails": [
            "单次权重调整不超过2个百分点",
            "趋势/资金/广度权重限制在15%—45%",
            "期指权重限制在5%—20%",
            "至少20个已完成5日预测才允许调权",
            "距上次调权至少5天",
        ],
    }
