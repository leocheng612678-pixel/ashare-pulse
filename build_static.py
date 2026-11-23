from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SITE = ROOT / "site"
os.environ["ASHARE_PULSE_HOME"] = str(ROOT)
os.environ["ASHARE_PULSE_REQUIRE_MOBILE_TOKEN"] = "0"
sys.path.insert(0, str(ROOT))

import app as web_app  # noqa: E402
import storage  # noqa: E402


def _public_value(value: object) -> object:
    if isinstance(value, dict):
        return {key: _public_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if isinstance(value, str):
        text = value.replace(str(ROOT), "[project]")
        text = re.sub(r"[A-Za-z]:\\Users\\[^\r\n\"']+", "[local-path]", text)
        text = text.split("Traceback (most recent call last):", 1)[0].strip()
        return text
    return value


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )


def _sanitize_data_state() -> None:
    data_dir = ROOT / "data"
    if not data_dir.is_dir():
        return
    for path in data_dir.rglob("*.json"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            _write_json(path, _public_value(payload))
        except (OSError, ValueError, TypeError):
            continue


def _static_index(version: str) -> str:
    source = (ROOT / "web" / "index.html").read_text(encoding="utf-8")
    source = source.replace(
        'href="/styles.css"', f'href="styles.css?v={version}"'
    )
    source = source.replace('src="/app.js"', f'src="app.js?v={version}"')
    source = source.replace('href="/favicon.svg"', 'href="favicon.svg"')
    source = source.replace(
        'href="/manifest.webmanifest"', 'href="manifest.webmanifest"'
    )
    source = source.replace('href="/"', 'href="./"')
    source = source.replace(
        '<script src="app.js',
        (
            "<script>window.ASHARE_STATIC=true;</script>\n"
            f'  <script src="static_mode.js?v={version}" defer></script>\n'
            '  <script src="app.js'
        ),
        1,
    )
    source = source.replace(
        'content="A股资金、成交额、期指升贴水、ARMS广度与仓位参考仪表盘"',
        'content="A股资金、成交额、期指升贴水、ARMS广度、主动选股与仓位参考云端日报"',
    )
    return source


def _static_manifest() -> str:
    manifest = json.loads(
        (ROOT / "web" / "manifest.webmanifest").read_text(encoding="utf-8")
    )
    manifest["start_url"] = "./"
    manifest["scope"] = "./"
    manifest["icons"] = [
        {
            "src": "favicon.svg",
            "sizes": "any",
            "type": "image/svg+xml",
            "purpose": "any maskable",
        }
    ]
    return json.dumps(manifest, ensure_ascii=False, indent=2)


def build(*, refresh: bool) -> dict[str, object]:
    refresh_result: dict[str, object] | None = None
    if refresh:
        refresh_result = web_app.refresh_market(force=True)

    snapshot = storage.load_latest()
    if not snapshot:
        raise RuntimeError("没有行情缓存，且本次抓取未生成数据。")

    dashboard = web_app._attach_strategy(snapshot)
    dashboard["ok"] = True
    generated_at = str(dashboard.get("generated_at") or "")
    version_match = re.search(r"\d{8}T\d{6}", generated_at)
    version = (
        version_match.group(0)
        if version_match
        else datetime.now().strftime("%Y%m%d%H%M%S")
    )
    dashboard["static_report"] = {
        "mode": "github-pages",
        "computer_not_required": True,
        "update_schedule": "交易日北京时间约16:10",
        "refresh_result": refresh_result,
        "built_at": datetime.now().astimezone().isoformat(
            timespec="seconds"
        ),
    }

    if SITE.exists():
        shutil.rmtree(SITE)
    SITE.mkdir(parents=True)
    for name in ("styles.css", "app.js", "static_mode.js", "favicon.svg"):
        shutil.copy2(ROOT / "web" / name, SITE / name)
    (SITE / "index.html").write_text(
        _static_index(version), encoding="utf-8"
    )
    (SITE / "manifest.webmanifest").write_text(
        _static_manifest(), encoding="utf-8"
    )
    (SITE / "404.html").write_text(
        (
            '<!doctype html><meta charset="utf-8">'
            '<meta http-equiv="refresh" content="0;url=./">'
            "<title>A股资金脉搏</title>"
        ),
        encoding="utf-8",
    )
    (SITE / ".nojekyll").write_text("", encoding="utf-8")
    _write_json(SITE / "data" / "dashboard.json", _public_value(dashboard))
    _sanitize_data_state()
    return {
        "ok": True,
        "trade_date": dashboard.get("trade_date"),
        "generated_at": generated_at,
        "action": dashboard.get("strategy", {}).get("action"),
        "refresh": refresh_result,
        "site": str(SITE),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="生成 GitHub Pages 云端日报")
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="仅使用本地缓存生成页面，不请求行情接口",
    )
    args = parser.parse_args()
    result = build(refresh=not args.no_refresh)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
