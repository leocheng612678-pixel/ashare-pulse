"use strict";

(() => {
  const nativeFetch = window.fetch.bind(window);
  const dataUrl = new URL("data/dashboard.json", document.baseURI).href;
  let dashboardPromise = null;

  function dashboardData() {
    if (!dashboardPromise) {
      dashboardPromise = nativeFetch(dataUrl, { cache: "no-store" }).then(
        async (response) => {
          const text = await response.text();
          if (!response.ok || !text.trim()) {
            throw new Error(`每日数据载入失败（HTTP ${response.status}）`);
          }
          return JSON.parse(text);
        }
      );
    }
    return dashboardPromise;
  }

  function jsonResponse(payload, status = 200) {
    return new Response(JSON.stringify(payload), {
      status,
      headers: {
        "Content-Type": "application/json; charset=utf-8",
        "Cache-Control": "no-store",
      },
    });
  }

  window.fetch = async (input) => {
    const rawUrl = typeof input === "string" ? input : input.url;
    const url = new URL(rawUrl, document.baseURI);
    const data = await dashboardData();

    if (url.pathname.endsWith("/api/dashboard")) {
      return jsonResponse(data);
    }

    if (url.pathname.endsWith("/api/assets")) {
      if (url.searchParams.has("codes")) {
        return jsonResponse({
          ok: false,
          message:
            "云端日报版提供每日主动选股，不支持输入任意代码做实时检查。",
        });
      }
      return jsonResponse({
        ok: true,
        decision: data.asset_decision,
        etf_candidates: data.asset_decision?.etf_candidates || [],
      });
    }

    if (url.pathname.endsWith("/api/active-stocks")) {
      return jsonResponse(
        data.active_stocks
          ? { ok: true, ...data.active_stocks }
          : { ok: false, message: "本日主动选股结果尚未生成。" }
      );
    }

    if (url.pathname.endsWith("/api/plan")) {
      const exposure = data.strategy?.exposure || {
        low: 0,
        high: 0,
        mid: 0,
      };
      const position = Number(url.searchParams.get("position")) || 0;
      const gap = Number(exposure.mid || 0) - position;
      return jsonResponse({
        ok: true,
        strategy: data.strategy,
        plan: {
          target_low: exposure.low,
          target_high: exposure.high,
          gap_pct: gap,
          action:
            Math.abs(gap) <= 3
              ? "维持现有仓位"
              : gap > 0
                ? "分批加仓"
                : "分批减仓",
        },
      });
    }

    if (url.pathname.endsWith("/api/refresh")) {
      return jsonResponse({
        ok: false,
        message: "云端日报每日收盘后自动更新，无需手动刷新。",
      });
    }

    if (url.pathname.endsWith("/api/mobile-link")) {
      return jsonResponse({
        ok: false,
        message: "当前已经是固定云端网址，不需要生成手机访问链接。",
      });
    }

    return jsonResponse(
      { ok: false, message: "云端日报版不提供该接口。" },
      404
    );
  };

  document.addEventListener("DOMContentLoaded", () => {
    const actions = document.querySelector(".topbar-actions");
    if (actions) {
      const badge = document.createElement("span");
      badge.className = "cloud-report-badge";
      badge.textContent = "云端日报";
      badge.title = "电脑关机后仍可访问，每个交易日收盘后自动更新";
      actions.prepend(badge);
    }

    const refresh = document.getElementById("refresh-button");
    if (refresh) {
      refresh.disabled = true;
      refresh.textContent = "每日更新";
      refresh.title = "每个交易日收盘后自动更新";
    }

    const mobile = document.getElementById("mobile-share-button");
    if (mobile) {
      mobile.hidden = true;
      mobile.style.display = "none";
    }

    const codes = document.getElementById("asset-codes");
    const check = document.getElementById("check-assets");
    if (codes) {
      codes.disabled = true;
      codes.value = "";
      codes.placeholder = "云端日报版不保存个人持仓，请查看下方每日主动选股";
    }
    if (check) {
      check.disabled = true;
      check.textContent = "每日候选已生成";
    }
  });
})();
