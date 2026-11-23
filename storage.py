from __future__ import annotations

import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from market_data import now_china


BASE_DIR = Path(__file__).resolve().parent
HOME_OVERRIDE = os.environ.get("ASHARE_PULSE_HOME")
if HOME_OVERRIDE:
    BASE_DIR = Path(HOME_OVERRIDE).resolve()
DATA_DIR = BASE_DIR / "data"
HISTORY_DIR = DATA_DIR / "history"
LATEST_PATH = DATA_DIR / "latest.json"


def ensure_dirs() -> None:
    HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _write_json_atomic(path: Path, payload: Any) -> None:
    ensure_dirs()
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp_path, path)


def save_snapshot(snapshot: dict[str, Any]) -> None:
    ensure_dirs()
    _write_json_atomic(LATEST_PATH, snapshot)
    trade_date = snapshot.get("trade_date") or now_china().date().isoformat()
    _write_json_atomic(HISTORY_DIR / f"{trade_date}.json", snapshot)


def load_latest() -> dict[str, Any] | None:
    if not LATEST_PATH.exists():
        return None
    try:
        with LATEST_PATH.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def load_history(days: int = 120) -> list[dict[str, Any]]:
    ensure_dirs()
    cutoff = now_china().date() - timedelta(days=days * 2)
    result: list[dict[str, Any]] = []
    for path in sorted(HISTORY_DIR.glob("*.json")):
        try:
            trade_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if trade_date < cutoff:
            continue
        try:
            with path.open("r", encoding="utf-8") as handle:
                result.append(json.load(handle))
        except (OSError, json.JSONDecodeError):
            continue
    return result[-days:]


def merge_with_cache(
    fresh: dict[str, Any], cached: dict[str, Any] | None
) -> dict[str, Any]:
    if not cached:
        return fresh
    merged = dict(cached)
    merged.update(fresh)
    for key in (
        "indices",
        "futures",
        "capital",
        "breadth",
        "technicals",
        "historical_csi",
        "sectors",
    ):
        value = fresh.get(key)
        is_empty = value in (None, [], {})
        if key == "capital":
            is_empty = not bool((value or {}).get("history"))
        elif key == "breadth":
            is_empty = not bool((value or {}).get("coverage"))
        elif key == "technicals":
            is_empty = not bool((value or {}).get("trend"))
        elif key == "sectors":
            is_empty = not bool((value or {}).get("inflow")) and not bool(
                (value or {}).get("outflow")
            )
        if is_empty and cached.get(key) not in (None, [], {}):
            merged[key] = cached[key]
            source_name = {
                "indices": "指数行情",
                "futures": "期指升贴水",
                "capital": "资金流",
                "breadth": "涨跌家数",
                "technicals": "市场技术面",
                "historical_csi": "历史样本",
                "sectors": "行业资金",
            }[key]
            merged.setdefault("sources", {})[source_name] = {
                "status": "cached",
                "detail": "本次抓取失败，沿用上次缓存",
                "fetched_at": cached.get("generated_at"),
            }
    fresh_market_available = any(
        [
            fresh.get("indices"),
            fresh.get("futures"),
            (fresh.get("capital") or {}).get("history"),
            (fresh.get("breadth") or {}).get("coverage"),
        ]
    )
    if not fresh_market_available and cached.get("trade_date"):
        merged["trade_date"] = cached["trade_date"]
    return merged


def prune_history(keep_days: int = 730) -> None:
    ensure_dirs()
    cutoff = now_china().date() - timedelta(days=keep_days)
    for path in HISTORY_DIR.glob("*.json"):
        try:
            trade_date = datetime.strptime(path.stem, "%Y-%m-%d").date()
        except ValueError:
            continue
        if trade_date < cutoff:
            try:
                path.unlink()
            except OSError:
                pass
