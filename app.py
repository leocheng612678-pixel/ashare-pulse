from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import socket
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import market_data
import storage
import adaptive
from active_stock_selector import select_active_stocks
from asset_selector import build_asset_decision, choose_etf_candidates, evaluate_codes
from big_money import analyze_market_big_money, enrich_stocks_with_big_money
from company_fundamentals import enrich_stocks_with_fundamentals
from evidence import build_evidence, build_historical_validation
from framework import build_observation_framework
from portfolio_risk import candidate_portfolio_risk
from backtest import run_core_backtest
from public_tunnel import start_public_tunnel
from strategy import (
    RISK_PROFILES,
    build_strategy,
    personalize_plan,
)


CODE_DIR = Path(__file__).resolve().parent
BASE_DIR = Path(os.environ.get("ASHARE_PULSE_HOME", CODE_DIR)).resolve()
WEB_DIR = Path(getattr(sys, "_MEIPASS", CODE_DIR)) / "web"
REFRESH_LOCK = threading.Lock()
STOCK_SELECT_LOCK = threading.Lock()
REFRESH_STATE: dict[str, Any] = {
    "running": False,
    "last_attempt": None,
    "last_success": None,
    "last_error": None,
}
PUBLIC_TUNNEL_STATE: dict[str, Any] = {
    "running": False,
    "ready": False,
    "provider": None,
    "last_attempt": None,
    "last_success": None,
    "last_error": None,
}
PUBLIC_TUNNEL_THREAD: threading.Thread | None = None
PUBLIC_TUNNEL_STOP = threading.Event()
PUBLIC_TUNNEL_STATE_LOCK = threading.Lock()
MOBILE_TOKEN_PATH = storage.DATA_DIR / "mobile_access_token.txt"
MOBILE_AUTH_REQUIRED = os.environ.get(
    "ASHARE_PULSE_REQUIRE_MOBILE_TOKEN", "1"
) != "0"


def _mobile_token() -> str:
    configured = os.environ.get("ASHARE_PULSE_MOBILE_TOKEN")
    if configured:
        return configured
    try:
        value = MOBILE_TOKEN_PATH.read_text(encoding="ascii").strip()
        if len(value) >= 16:
            return value
    except OSError:
        pass
    value = secrets.token_urlsafe(15)
    try:
        storage.ensure_dirs()
        MOBILE_TOKEN_PATH.write_text(value, encoding="ascii")
    except OSError:
        pass
    return value


MOBILE_TOKEN = _mobile_token()


def start_public_tunnel_background(port: int) -> None:
    global PUBLIC_TUNNEL_THREAD
    with PUBLIC_TUNNEL_STATE_LOCK:
        if PUBLIC_TUNNEL_THREAD and PUBLIC_TUNNEL_THREAD.is_alive():
            return
        PUBLIC_TUNNEL_STOP.clear()
        PUBLIC_TUNNEL_THREAD = threading.Thread(
            target=_public_tunnel_worker,
            args=(port,),
            name="public-mobile-tunnel",
            daemon=True,
        )
        PUBLIC_TUNNEL_THREAD.start()


def stop_public_tunnel_background() -> None:
    PUBLIC_TUNNEL_STOP.set()


def _public_tunnel_worker(port: int) -> None:
    while not PUBLIC_TUNNEL_STOP.is_set():
        with PUBLIC_TUNNEL_STATE_LOCK:
            PUBLIC_TUNNEL_STATE["running"] = True
            PUBLIC_TUNNEL_STATE["last_attempt"] = (
                market_data.now_china().isoformat(timespec="seconds")
            )
        try:
            result = start_public_tunnel(
                BASE_DIR,
                port=port,
                token=MOBILE_TOKEN,
                timeout=45,
            )
            with PUBLIC_TUNNEL_STATE_LOCK:
                PUBLIC_TUNNEL_STATE.update(
                    {
                        "ready": True,
                        "provider": result.get("provider"),
                        "last_success": (
                            market_data.now_china().isoformat(
                                timespec="seconds"
                            )
                        ),
                        "last_error": None,
                    }
                )
            if PUBLIC_TUNNEL_STOP.wait(45):
                break
        except Exception as exc:
            with PUBLIC_TUNNEL_STATE_LOCK:
                PUBLIC_TUNNEL_STATE.update(
                    {"ready": False, "last_error": str(exc)[-1200:]}
                )
            if PUBLIC_TUNNEL_STOP.wait(20):
                break
    with PUBLIC_TUNNEL_STATE_LOCK:
        PUBLIC_TUNNEL_STATE["running"] = False


def _local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def _is_loopback(address: str) -> bool:
    return address in ("127.0.0.1", "::1", "localhost")


def _attach_strategy(snapshot: dict[str, Any]) -> dict[str, Any]:
    history = storage.load_history(30)
    output = dict(snapshot)
    model_state = adaptive.load_model_state()
    output["strategy"] = build_strategy(
        output,
        history,
        "balanced",
        factor_weights=model_state["weights"],
    )
    _attach_arms_detail(output, history)
    output["big_money"] = analyze_market_big_money(output)
    output["asset_decision"] = build_asset_decision(output["strategy"])
    output["historical_validation"] = build_historical_validation(output)
    output["evidence"] = build_evidence(
        output, output["strategy"], output["historical_validation"]
    )
    output["backtest"] = run_core_backtest(output)
    output["framework"] = build_observation_framework(output)
    output["adaptive_model"] = adaptive.model_view(output)
    output["history"] = [
        {
            "trade_date": item.get("trade_date"),
            "score": (item.get("strategy") or {}).get("score"),
            "turnover": ((item.get("technicals") or {}).get("turnover")),
            "main_net": (
                (((item.get("capital") or {}).get("history") or [{}])[-1]).get(
                    "main"
                )
            ),
            "arms": ((item.get("breadth") or {}).get("arms")),
        }
        for item in history[-20:]
    ]
    return output


def _attach_arms_detail(
    snapshot: dict[str, Any], history: list[dict[str, Any]]
) -> None:
    breadth = dict(snapshot.get("breadth") or {})
    arms = breadth.get("arms")
    up = int(breadth.get("up") or 0)
    down = int(breadth.get("down") or 0)
    up_ratio = float(breadth.get("advance_decline_ratio") or 0)
    volume_ratio = float(
        breadth.get("volume_advance_decline_ratio") or 0
    )
    if not up_ratio and down > 0:
        up_ratio = up / down
    if not volume_ratio:
        down_volume = float(breadth.get("down_volume") or 0)
        up_volume = float(breadth.get("up_volume") or 0)
        if down_volume > 0:
            volume_ratio = up_volume / down_volume
    advance_share = up / (up + down) if up + down else 0
    if arms is None:
        classification = "无法计算"
        interpretation = "上涨或下跌成交量不足，暂不计算 ARMS。"
    elif advance_share < 0.35 and arms < 0.8:
        classification = "窄幅吸量"
        interpretation = (
            "ARMS偏低并非全面强势，而是少数上涨股集中吸收成交量，"
            "同时市场宽度明显偏弱。"
        )
    elif arms < 0.5:
        classification = "极强/可能过热"
        interpretation = "上涨股成交效率很高，但需防止短线过热。"
    elif arms < 0.8:
        classification = "偏强"
        interpretation = "上涨方向成交量效率高于家数占比。"
    elif arms <= 1.2:
        classification = "中性"
        interpretation = "上涨和下跌方向的量价结构大致均衡。"
    elif arms <= 1.5:
        classification = "偏弱"
        interpretation = "下跌方向成交量效率较高。"
    else:
        classification = "卖压极端"
        interpretation = "下跌股集中吸收成交量，风险偏好显著走弱。"

    by_date: dict[str, float] = {}
    for item in history:
        item_arms = ((item.get("breadth") or {}).get("arms"))
        trade_date = item.get("trade_date")
        if trade_date and item_arms is not None:
            by_date[str(trade_date)] = float(item_arms)
    if snapshot.get("trade_date") and arms is not None:
        by_date[str(snapshot["trade_date"])] = float(arms)
    arms_history = [
        {"trade_date": day, "arms": by_date[day]}
        for day in sorted(by_date)
    ][-30:]

    def average(period: int) -> float | None:
        values = [item["arms"] for item in arms_history[-period:]]
        return sum(values) / len(values) if values else None

    breadth["arms_detail"] = {
        "formula": "(上涨家数 / 下跌家数) / (上涨股成交量 / 下跌股成交量)",
        "advance_decline_ratio": up_ratio,
        "volume_advance_decline_ratio": volume_ratio,
        "classification": classification,
        "interpretation": interpretation,
        "up_volume_share": breadth.get("up_volume_ratio"),
        "arms_5d": average(5),
        "arms_10d": average(10),
        "arms_20d": average(20),
        "history_count": len(arms_history),
    }
    breadth["arms_history"] = arms_history
    snapshot["breadth"] = breadth


def select_stocks_for_snapshot(
    snapshot: dict[str, Any], *, force_universe: bool = False
) -> dict[str, Any]:
    breadth = snapshot.get("breadth") or {}
    seeds = breadth.get("candidate_seeds") or []
    if not seeds and force_universe:
        breadth = market_data.fetch_market_breadth()
        snapshot["breadth"] = {**(snapshot.get("breadth") or {}), **breadth}
        seeds = breadth.get("candidate_seeds") or []
    strategy = snapshot.get("strategy") or build_strategy(
        snapshot, storage.load_history(30), "balanced"
    )
    trend = (snapshot.get("technicals") or {}).get("trend") or {}
    stocks = select_active_stocks(
        seeds,
        market_score=float(strategy.get("score") or 0),
        csi_return_20d=trend.get("return_20d"),
    )
    stocks = enrich_stocks_with_fundamentals(stocks)
    stocks = enrich_stocks_with_big_money(
        stocks, trade_date=snapshot.get("trade_date")
    )
    snapshot["portfolio_risk"] = candidate_portfolio_risk(stocks)
    snapshot["active_stocks"] = {
        "trade_date": snapshot.get("trade_date"),
        "generated_at": market_data.now_china().isoformat(timespec="seconds"),
        "universe_size": len(seeds),
        "stocks": stocks,
        "policy": (
            "市场防守时只形成观察名单，不执行买入。"
            if float(strategy.get("score") or 0) < 15
            else "市场转强后按触发条件分批执行。"
        ),
    }
    snapshot["framework"] = build_observation_framework(snapshot)
    return {
        "trade_date": snapshot.get("trade_date"),
        "generated_at": market_data.now_china().isoformat(timespec="seconds"),
        "universe_size": len(seeds),
        "stocks": stocks,
        "policy": (
            "市场防守时只形成观察名单，不执行买入。"
            if float(strategy.get("score") or 0) < 15
            else "市场转强后按触发条件分批执行。"
        ),
    }


def refresh_market(force: bool = False) -> dict[str, Any]:
    if not REFRESH_LOCK.acquire(blocking=False):
        return {"ok": False, "message": "已有一次刷新正在进行"}
    try:
        REFRESH_STATE.update(
            running=True,
            last_attempt=market_data.now_china().isoformat(timespec="seconds"),
            last_error=None,
        )
        cached = storage.load_latest()
        if not force and cached:
            generated_at = cached.get("generated_at")
            if generated_at:
                try:
                    generated = datetime.fromisoformat(generated_at)
                    age = (
                        market_data.now_china() - generated
                    ).total_seconds()
                    if age < 240:
                        snapshot = _attach_strategy(cached)
                        storage.save_snapshot(snapshot)
                        REFRESH_STATE.update(
                            running=False,
                            last_success=snapshot["generated_at"],
                        )
                        return {
                            "ok": True,
                            "cached": True,
                            "message": "缓存仍然新鲜",
                        }
                except ValueError:
                    pass

        fresh, errors = market_data.fetch_market_snapshot()
        merged = storage.merge_with_cache(fresh, cached)
        outcome_update = adaptive.attach_outcomes(merged)
        adaptation = adaptive.evaluate_and_adapt(merged)
        merged = _attach_strategy(merged)
        try:
            merged["active_stocks"] = select_stocks_for_snapshot(merged)
        except Exception as exc:
            merged["active_stocks_error"] = str(exc)
        prediction_result = adaptive.record_prediction(merged)
        merged["adaptive_update"] = {
            "outcomes": outcome_update,
            "adaptation": adaptation,
            "prediction": prediction_result,
        }
        merged["adaptive_model"] = adaptive.model_view(merged)
        storage.save_snapshot(merged)
        storage.prune_history()
        REFRESH_STATE.update(
            running=False,
            last_success=merged["generated_at"],
            last_error=(
                "; ".join(item["message"] for item in errors) if errors else None
            ),
        )
        return {
            "ok": True,
            "cached": False,
            "errors": errors,
            "message": "行情已刷新",
        }
    except Exception as exc:
        REFRESH_STATE.update(
            running=False,
            last_error=str(exc),
        )
        return {
            "ok": False,
            "message": f"刷新失败：{exc}",
            "trace": traceback.format_exc(limit=4),
        }
    finally:
        REFRESH_LOCK.release()


def _needs_background_refresh(snapshot: dict[str, Any] | None) -> bool:
    now = market_data.now_china()
    if now.weekday() >= 5:
        return False
    minute = now.hour * 60 + now.minute
    in_session = (9 * 60 + 5 <= minute <= 11 * 60 + 40) or (
        12 * 60 + 55 <= minute <= 15 * 60 + 15
    )
    if not snapshot:
        return True
    if in_session:
        try:
            generated = datetime.fromisoformat(
                snapshot.get("generated_at", "")
            )
            return (now - generated).total_seconds() >= 300
        except ValueError:
            return True
    if minute >= 15 * 60 + 5:
        return snapshot.get("trade_date") != now.date().isoformat()
    return False


def scheduler_loop(stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        try:
            cached = storage.load_latest()
            if _needs_background_refresh(cached):
                refresh_market(force=True)
        except Exception:
            pass
        stop_event.wait(60)


class AppHandler(BaseHTTPRequestHandler):
    server_version = "ASharePulse/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        stamp = market_data.now_china().strftime("%H:%M:%S")
        line = f"[{stamp}] {self.address_string()} {fmt % args}"
        print(line)
        if os.environ.get("ASHARE_PULSE_ACCESS_LOG") == "1":
            try:
                with (storage.DATA_DIR / "access.log").open(
                    "a", encoding="utf-8"
                ) as handle:
                    handle.write(line + "\n")
            except OSError:
                pass

    def _cookie_token(self) -> str:
        cookie = self.headers.get("Cookie", "")
        for item in cookie.split(";"):
            key, _, value = item.strip().partition("=")
            if key == "ashare_access":
                return value
        return ""

    def _authorized(self) -> bool:
        if not MOBILE_AUTH_REQUIRED:
            return True
        return _is_loopback(self.client_address[0]) or hmac.compare_digest(
            self._cookie_token(), MOBILE_TOKEN
        )

    def _send_mobile_gate(self) -> None:
        body = (
            "<!doctype html><html lang='zh-CN'><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            "<title>A股资金脉搏</title>"
            "<body style='margin:0;background:#07100e;color:#e9f1ed;"
            "font-family:Microsoft YaHei,sans-serif;display:grid;"
            "place-items:center;min-height:100vh'>"
            "<div style='max-width:420px;padding:28px;text-align:center'>"
            "<h1 style='color:#e4be71'>需要手机访问链接</h1>"
            "<p style='color:#8ba49b;line-height:1.8'>"
            "请在电脑端程序点击“手机访问”，使用生成的完整链接打开。"
            "</p></div></body></html>"
        ).encode("utf-8")
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json(
        self, payload: Any, status: int = HTTPStatus.OK
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_static(self, path: Path) -> None:
        if not path.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        try:
            body = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        mime, _ = mimetypes.guess_type(path.name)
        content_type = mime or "application/octet-stream"
        if content_type.startswith("text/") or content_type in (
            "application/javascript",
            "application/json",
        ):
            content_type += "; charset=utf-8"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        mobile_path = f"/m/{MOBILE_TOKEN}"
        if parsed.path.rstrip("/") == mobile_path:
            secure = (
                "; Secure"
                if self.headers.get("X-Forwarded-Proto", "").lower() == "https"
                else ""
            )
            self.send_response(HTTPStatus.FOUND)
            self.send_header("Location", "/")
            self.send_header(
                "Set-Cookie",
                f"ashare_access={MOBILE_TOKEN}; Path=/; HttpOnly; "
                f"SameSite=Lax; Max-Age=31536000{secure}",
            )
            self.end_headers()
            return
        if parsed.path.startswith("/m/") and not self._authorized():
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not self._authorized():
            if parsed.path.startswith("/api/"):
                self._send_json(
                    {"ok": False, "message": "需要手机访问链接"},
                    HTTPStatus.UNAUTHORIZED,
                )
            else:
                self._send_mobile_gate()
            return
        if parsed.path == "/api/dashboard":
            snapshot = storage.load_latest()
            if not snapshot:
                payload = {
                    "ok": False,
                    "message": "尚无行情缓存，请点击刷新。",
                    "strategy": None,
                }
            else:
                payload = _attach_strategy(snapshot)
                payload["ok"] = True
            payload["refresh_state"] = REFRESH_STATE
            payload["risk_profiles"] = RISK_PROFILES
            self._send_json(payload)
            return
        if parsed.path == "/api/health":
            self._send_json(
                {
                    "ok": True,
                    "time": market_data.now_china().isoformat(timespec="seconds"),
                    "refresh": REFRESH_STATE,
                    "public_tunnel": {
                        "running": PUBLIC_TUNNEL_STATE["running"],
                        "ready": PUBLIC_TUNNEL_STATE["ready"],
                        "provider": PUBLIC_TUNNEL_STATE["provider"],
                    },
                }
            )
            return
        if parsed.path == "/api/mobile-link":
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("public", ["0"])[0] == "1":
                try:
                    result = start_public_tunnel(
                        BASE_DIR,
                        port=self.server.server_port,
                        token=MOBILE_TOKEN,
                    )
                except Exception as exc:
                    self._send_json(
                        {"ok": False, "message": str(exc)},
                        HTTPStatus.BAD_GATEWAY,
                    )
                    return
                self._send_json(result)
                return
            link = f"http://{_local_ip()}:{self.server.server_port}{mobile_path}"
            self._send_json(
                {
                    "ok": True,
                    "url": link,
                    "public": False,
                    "note": "手机和电脑需连接同一局域网；首次访问后浏览器会保存访问凭证。",
                }
            )
            return
        if parsed.path == "/api/plan":
            query = urllib.parse.parse_qs(parsed.query)
            snapshot = storage.load_latest()
            if not snapshot:
                self._send_json(
                    {"ok": False, "message": "尚无行情数据"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            history = storage.load_history(30)
            risk_profile = query.get("risk", ["balanced"])[0]
            if risk_profile not in RISK_PROFILES:
                risk_profile = "balanced"
            model_state = adaptive.load_model_state()
            strategy = build_strategy(
                snapshot,
                history,
                risk_profile,
                factor_weights=model_state["weights"],
            )
            try:
                capital = float(query.get("capital", ["0"])[0])
                position = float(query.get("position", ["0"])[0])
            except ValueError:
                capital, position = 0.0, 0.0
            plan = personalize_plan(strategy, capital, position)
            self._send_json(
                {
                    "ok": True,
                    "strategy": strategy,
                    "plan": plan,
                }
            )
            return
        if parsed.path == "/api/assets":
            snapshot = storage.load_latest()
            if not snapshot:
                self._send_json(
                    {"ok": False, "message": "尚无行情数据"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            history = storage.load_history(30)
            model_state = adaptive.load_model_state()
            strategy = build_strategy(
                snapshot,
                history,
                "balanced",
                factor_weights=model_state["weights"],
            )
            decision = build_asset_decision(strategy)
            query = urllib.parse.parse_qs(parsed.query)
            raw_codes = query.get("codes", [""])[0]
            role = query.get("role", ["holdings"])[0]
            if role not in ("holdings", "watch"):
                role = "holdings"
            codes = [
                item.strip()
                for item in raw_codes.replace("，", ",").split(",")
                if item.strip()
            ]
            try:
                if codes:
                    assets = evaluate_codes(
                        codes,
                        market_score=float(strategy.get("score") or 0),
                        role=role,
                    )
                else:
                    assets = []
                etf_candidates = (
                    choose_etf_candidates(float(strategy.get("score") or 0))
                    if not codes
                    else []
                )
                self._send_json(
                    {
                        "ok": True,
                        "decision": decision,
                        "assets": assets,
                        "etf_candidates": etf_candidates,
                    }
                )
            except Exception as exc:
                self._send_json(
                    {"ok": False, "message": f"资产分析失败：{exc}"},
                    HTTPStatus.BAD_GATEWAY,
                )
            return
        if parsed.path == "/api/active-stocks":
            snapshot = storage.load_latest()
            if not snapshot:
                self._send_json(
                    {"ok": False, "message": "尚无行情数据"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            query = urllib.parse.parse_qs(parsed.query)
            force = query.get("refresh", ["0"])[0] == "1"
            cached = snapshot.get("active_stocks")
            if (
                cached
                and not force
                and cached.get("trade_date") == snapshot.get("trade_date")
            ):
                self._send_json({"ok": True, "cached": True, **cached})
                return
            if not STOCK_SELECT_LOCK.acquire(blocking=False):
                self._send_json(
                    {"ok": False, "message": "主动选股正在进行，请稍候"},
                    HTTPStatus.CONFLICT,
                )
                return
            try:
                snapshot = _attach_strategy(snapshot)
                selected = select_stocks_for_snapshot(
                    snapshot, force_universe=True
                )
                snapshot["active_stocks"] = selected
                snapshot.pop("active_stocks_error", None)
                storage.save_snapshot(snapshot)
                self._send_json({"ok": True, "cached": False, **selected})
            except Exception as exc:
                self._send_json(
                    {"ok": False, "message": f"主动选股失败：{exc}"},
                    HTTPStatus.BAD_GATEWAY,
                )
            finally:
                STOCK_SELECT_LOCK.release()
            return
        if parsed.path in ("/", "/index.html"):
            self._send_static(WEB_DIR / "index.html")
            return
        relative = parsed.path.lstrip("/")
        candidate = (WEB_DIR / relative).resolve()
        try:
            candidate.relative_to(WEB_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        self._send_static(candidate)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        if not self._authorized():
            self._send_json(
                {"ok": False, "message": "需要手机访问链接"},
                HTTPStatus.UNAUTHORIZED,
            )
            return
        if parsed.path == "/api/shutdown":
            self._send_json({"ok": True, "message": "服务正在停止"})
            threading.Thread(
                target=self.server.shutdown,
                name="http-shutdown",
                daemon=True,
            ).start()
            return
        if parsed.path != "/api/refresh":
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length:
            self.rfile.read(min(length, 4096))
        result = refresh_market(force=True)
        status = HTTPStatus.OK if result.get("ok") else HTTPStatus.BAD_GATEWAY
        self._send_json(result, status)


def run_server(host: str, port: int, open_browser: bool) -> None:
    storage.ensure_dirs()
    latest = storage.load_latest()
    if latest and not latest.get("strategy"):
        latest = _attach_strategy(latest)
        storage.save_snapshot(latest)

    stop_event = threading.Event()
    worker = threading.Thread(
        target=scheduler_loop,
        args=(stop_event,),
        name="market-refresh",
        daemon=True,
    )
    worker.start()

    server = ThreadingHTTPServer((host, port), AppHandler)
    pid_path = storage.DATA_DIR / "server.pid"
    pid_path.write_text(str(os.getpid()), encoding="ascii")
    start_public_tunnel_background(port)
    url = f"http://{host}:{port}"
    print(f"\nA股资金脉搏已启动：{url}")
    if host in ("0.0.0.0", "::"):
        print(
            "手机访问："
            f"http://{_local_ip()}:{port}/m/{MOBILE_TOKEN}"
        )
    print("按 Ctrl+C 停止服务。数据仅作研究参考，不构成投资建议。\n")
    if open_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止服务...")
    finally:
        stop_event.set()
        stop_public_tunnel_background()
        server.server_close()
        try:
            if pid_path.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="A股资金脉搏本地仪表盘")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--refresh-once", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    if args.refresh_once:
        result = refresh_market(force=True)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    run_server(args.host, args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
