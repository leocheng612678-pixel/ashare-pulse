"use strict";

const state = {
  dashboard: null,
  risk: "balanced",
  assetRole: "holdings",
  stockLoading: false,
  loading: false,
};

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function finite(value) {
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatMoney(value, signed = false) {
  const number = finite(value);
  if (number === null) return "--";
  const sign = signed && number > 0 ? "+" : "";
  const absolute = Math.abs(number);
  if (absolute >= 1e12) return `${sign}${(number / 1e12).toFixed(2)} 万亿`;
  if (absolute >= 1e8) return `${sign}${(number / 1e8).toFixed(1)} 亿`;
  if (absolute >= 1e4) return `${sign}${(number / 1e4).toFixed(1)} 万`;
  return `${sign}${number.toFixed(0)}`;
}

function formatPct(value, signed = true) {
  const number = finite(value);
  if (number === null) return "--";
  return `${signed && number > 0 ? "+" : ""}${number.toFixed(2)}%`;
}

function formatPrice(value) {
  const number = finite(value);
  return number === null ? "--" : number.toLocaleString("zh-CN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
}

function directionClass(value) {
  const number = finite(value);
  if (number === null || Math.abs(number) < 0.0001) return "";
  return number > 0 ? "positive" : "negative";
}

function showToast(message, isError = false) {
  const toast = $("toast");
  toast.textContent = message;
  toast.classList.toggle("error", isError);
  toast.classList.add("show");
  window.clearTimeout(showToast.timer);
  showToast.timer = window.setTimeout(() => toast.classList.remove("show"), 4200);
}

async function requestJson(url, options = {}, attempts = 2) {
  let lastError = null;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    try {
      const response = await fetch(url, options);
      const text = await response.text();
      if (!text.trim()) {
        throw new Error("后台数据为空，手机服务可能正在恢复");
      }
      try {
        return JSON.parse(text);
      } catch {
        throw new Error(`后台返回格式异常（HTTP ${response.status}）`);
      }
    } catch (error) {
      lastError = error;
      if (attempt + 1 < attempts) {
        await new Promise((resolve) =>
          window.setTimeout(resolve, 450 * (attempt + 1))
        );
      }
    }
  }
  throw new Error(
    `手机服务暂时无响应：${lastError?.message || "未知错误"}。` +
      "请保持电脑和 A股资金脉搏运行，然后点击“重新连接”。"
  );
}

function setConnectionState(ok, message = "") {
  const banner = $("connection-banner");
  if (!banner) return;
  banner.hidden = ok;
  if (!ok) {
    $("connection-message").textContent =
      message || "后台服务暂时离线，行情恢复后会自动重新载入。";
  }
}

function updateClockText(snapshot) {
  const date = snapshot.trade_date || "--";
  $("trade-date").textContent = `${date} 收盘快照`;
  $("generated-at").textContent = snapshot.generated_at
    ? `抓取于 ${snapshot.generated_at.slice(5, 16).replace("T", " ")}`
    : "本地数据缓存";
  const dot = $("status-dot");
  dot.className = "status-dot";
  const age = finite((snapshot.strategy || {}).metrics?.trade_age_days);
  if (age !== null && age > 5) {
    dot.classList.add("stale");
  } else {
    dot.classList.add("live");
  }
}

function renderScore(strategy) {
  const score = finite(strategy.score) ?? 0;
  $("market-score").textContent = `${score > 0 ? "+" : ""}${score.toFixed(0)}`;
  $("market-regime").textContent = strategy.regime || "待判断";
  $("confidence-badge").textContent = `置信度 ${strategy.confidence ?? "--"}%`;
  $("action-title").textContent = strategy.action || "暂无判断";
  $("action-summary").textContent = strategy.summary || "";

  const gauge = $("gauge-value");
  gauge.classList.remove("positive", "negative");
  if (score > 0) gauge.classList.add("positive");
  if (score < 0) gauge.classList.add("negative");
  const ratio = Math.min(1, Math.abs(score) / 100);
  gauge.style.strokeDashoffset = String(339.292 * (1 - ratio));
}

function renderMetrics(data) {
  const strategy = data.strategy || {};
  const metrics = strategy.metrics || {};
  const breadth = data.breadth || {};
  const futures = data.futures || [];
  const ifFuture = futures.find((item) => item.code === "IF");

  $("metric-turnover").textContent = formatMoney(metrics.turnover);
  const turnoverVs = finite(metrics.turnover_vs_20d);
  $("metric-turnover-note").textContent = turnoverVs === null
    ? "较20日均额 --"
    : `较20日均额 ${((turnoverVs - 1) * 100).toFixed(0)}%`;
  $("metric-turnover-note").className = `metric-note ${directionClass(turnoverVs - 1)}`;

  $("metric-flow").textContent = formatMoney(metrics.main_net_today, true);
  $("metric-flow").className = `metric-value ${directionClass(metrics.main_net_today)}`;
  $("metric-flow-note").textContent = finite(metrics.main_net_ratio) === null
    ? "沪深指数口径"
    : `占成交额 ${formatPct(metrics.main_net_ratio)}`;

  const arms = finite(breadth.arms);
  const up = finite(breadth.up) || 0;
  const down = finite(breadth.down) || 0;
  const advanceRatio = up + down > 0 ? up / (up + down) : 0;
  $("metric-arms").textContent = arms === null ? "--" : arms.toFixed(2);
  $("metric-arms-note").textContent = arms === null
    ? "全A涨跌量能结构"
    : arms > 1.2
      ? "卖压占优"
      : arms < 0.8 && advanceRatio < 0.35
        ? "局部成交集中"
        : arms < 0.8 ? "买盘集中" : "量能结构中性";

  const basis = finite(ifFuture?.basis_pct);
  $("metric-basis").textContent = basis === null ? "--" : formatPct(basis);
  $("metric-basis").className = `metric-value ${directionClass(basis)}`;
  $("metric-basis-note").textContent = ifFuture
    ? `IF ${formatPrice(ifFuture.future_price)} / 现货 ${formatPrice(ifFuture.spot)}`
    : "相对沪深300";
}

function renderFlowChart(data) {
  const history = ((data.capital || {}).history || []).slice(-20);
  const container = $("flow-chart");
  if (!history.length) {
    container.innerHTML = '<div class="empty-state">资金流历史不足</div>';
    return;
  }
  const width = 760;
  const height = 250;
  const padding = { left: 52, right: 10, top: 16, bottom: 34 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;
  const values = history.map((row) => finite(row.main) || 0);
  const maxAbs = Math.max(...values.map((v) => Math.abs(v)), 1);
  const zeroY = padding.top + plotHeight / 2;
  const gap = plotWidth / values.length;
  const barWidth = Math.max(4, gap * 0.54);
  const gridLines = [-1, -0.5, 0, 0.5, 1];

  const grid = gridLines.map((ratio) => {
    const y = zeroY - ratio * (plotHeight / 2);
    const label = ratio === 0 ? "0" : formatMoney(ratio * maxAbs).replace(" ", "");
    const lineClass = ratio === 0 ? "chart-zero" : "chart-grid";
    return `<line class="${lineClass}" x1="${padding.left}" x2="${width - padding.right}" y1="${y}" y2="${y}"></line>
      <text class="chart-axis" x="${padding.left - 9}" y="${y + 4}" text-anchor="end">${escapeHtml(label)}</text>`;
  }).join("");

  const bars = history.map((row, index) => {
    const value = finite(row.main) || 0;
    const barHeight = Math.max(1, Math.abs(value) / maxAbs * (plotHeight / 2 - 2));
    const x = padding.left + index * gap + (gap - barWidth) / 2;
    const y = value >= 0 ? zeroY - barHeight : zeroY;
    const color = value >= 0 ? "var(--positive)" : "var(--negative)";
    const title = `${row.date} 主力 ${formatMoney(value, true)}`;
    return `<rect class="flow-bar" x="${x}" y="${y}" width="${barWidth}" height="${barHeight}" rx="2" fill="${color}">
      <title>${escapeHtml(title)}</title>
    </rect>`;
  }).join("");

  const labelEvery = history.length > 14 ? 4 : 3;
  const dates = history.map((row, index) => {
    if (index % labelEvery !== 0 && index !== history.length - 1) return "";
    const x = padding.left + index * gap + gap / 2;
    const label = String(row.date || "").slice(5);
    return `<text class="chart-axis" x="${x}" y="${height - 9}" text-anchor="middle">${escapeHtml(label)}</text>`;
  }).join("");

  container.innerHTML = `<svg viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="近20日主力资金净流入柱状图">
    ${grid}${bars}${dates}
  </svg>`;
}

function renderFactors(factors) {
  const list = $("factor-list");
  if (!factors?.length) {
    list.innerHTML = '<div class="empty-state">暂无因子数据</div>';
    return;
  }
  list.innerHTML = factors.map((factor) => {
    const score = finite(factor.score) || 0;
    const magnitude = Math.min(50, Math.abs(score) * 50);
    const left = score >= 0 ? 50 : 50 - magnitude;
    const status = factor.status || "";
    return `<div class="factor-item">
      <div class="factor-top">
        <span class="factor-name"><i class="${status}"></i>${escapeHtml(factor.name)}</span>
        <span class="factor-score">${score > 0 ? "+" : ""}${score.toFixed(0)} · 权重 ${factor.weight}%</span>
      </div>
      <div class="factor-track"><span class="factor-fill ${status}" style="left:${left}%;width:${magnitude}%"></span></div>
      <div class="factor-detail">${escapeHtml(factor.detail)}</div>
    </div>`;
  }).join("");
}

function renderBreadth(data) {
  const breadth = data.breadth || {};
  const up = finite(breadth.up) || 0;
  const down = finite(breadth.down) || 0;
  const flat = finite(breadth.flat) || 0;
  const total = Math.max(1, up + down + flat);
  const advanceRatio = up + down > 0 ? up / (up + down) : 0;
  $("breadth-up").textContent = up.toLocaleString("zh-CN");
  $("breadth-down").textContent = down.toLocaleString("zh-CN");
  $("breadth-flat").textContent = flat.toLocaleString("zh-CN");
  $("breadth-up-bar").style.width = `${up / total * 100}%`;
  $("breadth-flat-bar").style.width = `${flat / total * 100}%`;
  $("breadth-down-bar").style.width = `${down / total * 100}%`;
  $("breadth-coverage").textContent = `覆盖 ${breadth.coverage ?? "--"} 只`;
  $("limit-count").textContent = `${breadth.limit_up ?? "--"} / ${breadth.limit_down ?? "--"}`;
  $("up-amount-ratio").textContent = formatPct((finite(breadth.up_amount_ratio) || 0) * 100, false);
  const arms = finite(breadth.arms);
  const armsDetail = breadth.arms_detail || {};
  $("arms-reading").textContent = arms === null
    ? "--"
    : arms > 1.35
      ? "明显偏弱"
      : arms > 1.05
        ? "略偏弱"
        : arms < 0.75 && advanceRatio < 0.35
          ? "少数强势股吸量"
          : arms < 0.75 ? "偏强 / 防过热" : "中性";
  $("advance-decline-ratio").textContent = finite(
    armsDetail.advance_decline_ratio
  )?.toFixed(3) ?? "--";
  $("volume-ad-ratio").textContent = finite(
    armsDetail.volume_advance_decline_ratio
  )?.toFixed(3) ?? "--";
  $("arms-average").textContent = finite(armsDetail.arms_5d)?.toFixed(2) ?? "--";
  $("arms-reading").textContent = armsDetail.classification || $("arms-reading").textContent;
}

function renderFutures(futures) {
  const body = $("futures-body");
  if (!futures?.length) {
    body.innerHTML = '<tr><td colspan="5"><div class="empty-state">期指数据暂不可用</div></td></tr>';
    return;
  }
  body.innerHTML = futures.map((item) => {
    const basisPct = finite(item.basis_pct);
    const cls = directionClass(basisPct);
    return `<tr>
      <td><span class="contract-code">${escapeHtml(item.code)}</span>${escapeHtml(item.spot_name || item.name)}</td>
      <td>${formatPrice(item.future_price)}</td>
      <td>${formatPrice(item.spot)}</td>
      <td class="${cls}">${formatPrice(item.basis)}</td>
      <td><span class="basis-pill ${cls}">${basisPct === null ? "--" : formatPct(basisPct)}</span></td>
    </tr>`;
  }).join("");
}

function renderSectors(sectors) {
  const renderList = (items) => {
    if (!items?.length) return '<div class="empty-state">暂无数据</div>';
    return items.map((item) => `<div class="sector-row">
      <span class="sector-name">${escapeHtml(item.name)}</span>
      <span class="sector-flow ${directionClass(item.main_net)}">${formatMoney(item.main_net, true)}</span>
    </div>`).join("");
  };
  $("sector-inflow").innerHTML = renderList((sectors || {}).inflow);
  $("sector-outflow").innerHTML = renderList((sectors || {}).outflow);
}

function renderIndices(indices) {
  const list = $("indices-list");
  const chosen = ["000300", "000001", "399006", "000905", "000852"];
  const rows = chosen
    .map((code) => (indices || []).find((item) => item.code === code))
    .filter(Boolean);
  if (!rows.length) {
    list.innerHTML = '<div class="empty-state">暂无指数数据</div>';
    return;
  }
  list.innerHTML = rows.map((item) => `<div class="index-row">
    <span class="index-name">${escapeHtml(item.name)}</span>
    <span class="index-close">${formatPrice(item.close)}</span>
    <span class="index-change ${directionClass(item.change_pct)}">${formatPct(item.change_pct)}</span>
  </div>`).join("");
}

function renderRules(rules) {
  const groups = [
    ["加仓条件", rules?.add || []],
    ["减仓条件", rules?.reduce || []],
    ["风险边界", rules?.risk || []],
  ];
  $("execution-rules").innerHTML = groups.map(([title, items]) => `<div class="rule-column">
    <h3>${escapeHtml(title)}</h3>
    <ol>${items.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>
  </div>`).join("");
}

function renderRiskFlags(flags) {
  const list = $("risk-flags");
  if (!flags?.length) {
    list.innerHTML = '<div class="risk-empty">当前未触发显著风险阈值<br>仍需执行仓位与回撤纪律</div>';
    return;
  }
  list.innerHTML = flags.map((flag) => `<div class="risk-item ${escapeHtml(flag.level)}">
    <span class="risk-marker"></span>
    <div><strong>${escapeHtml(flag.title)}</strong><p>${escapeHtml(flag.detail)}</p></div>
  </div>`).join("");
}

function renderSources(data) {
  const sources = data.sources || {};
  const statusNames = { ok: "正常", cached: "缓存", error: "异常" };
  const entries = Object.entries(sources);
  $("quality-label").textContent = `模型置信度 ${data.strategy?.confidence ?? "--"}%`;
  $("source-list").innerHTML = entries.length
    ? entries.map(([name, source]) => {
        const status = source.status || "error";
        return `<div class="source-item">
          <span class="source-name"><i class="${escapeHtml(status)}"></i>${escapeHtml(name)}</span>
          <span class="source-detail">${escapeHtml(statusNames[status] || status)} · ${escapeHtml(source.detail || "")}</span>
        </div>`;
      }).join("")
    : '<div class="empty-state">暂无数据源状态</div>';
}

function renderAssetDecision(decision, etfCandidates = []) {
  if (!decision) return;
  const badge = $("asset-choice-badge");
  badge.textContent = decision.choice || "--";
  badge.className = "decision-badge";
  const isCashChoice = ["空仓", "现金为主", "持有观察"].includes(decision.choice);
  badge.classList.add(isCashChoice ? "cash" : "buy");

  $("asset-choice").textContent = decision.choice || "--";
  $("asset-reason").textContent = decision.reason || "";
  $("asset-target").textContent =
    decision.target_low === null || decision.target_high === null
      ? "--"
      : `${decision.target_low}% — ${decision.target_high}%`;
  $("asset-cash").innerHTML = `<strong>现金也是有效仓位</strong>${escapeHtml(
    decision.cash_instruction || ""
  )}`;

  if (!decision.buy_now) {
    $("asset-buy").innerHTML = `<strong>股票 / ETF：暂不执行买入</strong>
      ${escapeHtml(decision.stock_policy || "等待市场评分回到偏多区间。")}
      <ul><li>主动选股仍在运行，候选显示在下方观察名单。</li><li>ETF 暂不抢反弹，先等收盘确认。</li></ul>`;
  } else if (etfCandidates?.length) {
    $("asset-buy").innerHTML = `<strong>ETF 候选</strong><ul>${etfCandidates
      .map(
        (item) =>
          `<li>${escapeHtml(item.name)}（${escapeHtml(item.code)}） · 5日 ${formatPct(
            item.return_5d
          )}</li>`
      )
      .join("")}</ul><span>${escapeHtml(decision.stock_policy || "")}</span>`;
  } else {
    $("asset-buy").innerHTML = `<strong>暂无合格资产</strong>
      市场转强但 ETF 尚未形成“价格 > 20日线 > 60日线”的条件，继续持币等待。`;
  }

  $("asset-sell").innerHTML = `<strong>未录入持仓，无法指定卖出标的</strong>
    ${escapeHtml(decision.sell_policy || "")}
    <ul><li>不会凭空猜测你持有什么。</li><li>录入代码后按趋势规则给出减仓或清仓状态。</li></ul>`;
}

function assetStatusClass(status) {
  if (["卖出/清仓", "减仓"].includes(status)) return "sell";
  if (["候选"].includes(status)) return "buy";
  return "";
}

function renderAssetResults(assets, role) {
  const container = $("asset-results");
  if (!assets?.length) {
    container.innerHTML = `<div class="empty-state">没有取得有效代码结果。${
      role === "holdings"
        ? "请录入实际持仓代码后再判断卖出标的。"
        : "请录入观察池代码。"
    }</div>`;
    return;
  }
  container.innerHTML = `<table class="asset-result-table">
    <thead><tr>
      <th>代码</th><th>名称</th><th>现价</th><th>日涨跌</th>
      <th>20日线偏离</th><th>规则状态</th><th>依据</th>
    </tr></thead>
    <tbody>${assets
      .map((item) => {
        const statusClass = assetStatusClass(item.status);
        return `<tr>
          <td><span class="asset-code">${escapeHtml(item.code)}</span><span class="asset-type">${
            item.is_etf ? "ETF" : "股票"
          }</span></td>
          <td>${escapeHtml(item.name)}</td>
          <td>${formatPrice(item.price)}</td>
          <td class="${directionClass(item.change_pct)}">${formatPct(item.change_pct)}</td>
          <td class="${directionClass(item.distance_ma20_pct)}">${formatPct(
            item.distance_ma20_pct
          )}</td>
          <td><span class="asset-status ${statusClass}">${escapeHtml(item.status)}</span></td>
          <td class="asset-reason-cell">${escapeHtml(item.reason)}</td>
        </tr>`;
      })
      .join("")}</tbody>
  </table>`;

  if (role === "holdings") {
    const exitItems = assets.filter((item) =>
      ["卖出/清仓", "减仓"].includes(item.status)
    );
    if (exitItems.length) {
      $("asset-sell").innerHTML = `<strong>${exitItems.length} 项触发卖出 / 减仓规则</strong>
        <ul>${exitItems
          .map(
            (item) =>
              `<li>${escapeHtml(item.name)}（${escapeHtml(item.code)}） · ${escapeHtml(
                item.status
              )}</li>`
          )
          .join("")}</ul>`;
    } else {
      $("asset-sell").innerHTML = `<strong>当前持仓未触发卖出规则</strong>
        已检查 ${assets.length} 项；继续按最近一次收盘确认趋势与风险边界。`;
    }
  } else {
    const buyItems = assets.filter((item) => item.status === "候选");
    if (buyItems.length) {
      $("asset-buy").innerHTML = `<strong>${buyItems.length} 项进入买入候选</strong>
        <ul>${buyItems
          .map(
            (item) =>
              `<li>${escapeHtml(item.name)}（${escapeHtml(item.code)}） · 只在回踩20日线附近分档执行</li>`
          )
          .join("")}</ul>`;
    } else {
      $("asset-buy").innerHTML = `<strong>观察池没有合格买入项</strong>
        市场评分或个股自身趋势尚未同时满足条件，继续空仓或持有现金。`;
    }
  }
}

function renderActiveStocks(data) {
  if (!data) return;
  $("active-stock-count").textContent = `初筛 ${data.universe_size ?? 0} 只 · 入选 ${
    data.stocks?.length ?? 0
  } 只`;
  $("active-stock-policy").textContent = data.policy || "";
  const list = $("active-stock-list");
  if (!data.stocks?.length) {
    list.innerHTML = `<div class="empty-state">当前没有股票同时满足趋势、动量和流动性条件。市场防守时，空仓等待也是主动选择。</div>`;
    return;
  }
  list.innerHTML = data.stocks
    .map((stock) => {
      const statusClass = stock.status === "分批候选" ? "buy" : "";
      const plan = stock.trade_plan || {};
      return `<article class="stock-card">
        <div class="stock-card-head">
          <div>
            <h3>${escapeHtml(stock.name)}</h3>
            <span class="stock-card-code">${escapeHtml(stock.code)}</span>
          </div>
          <div class="stock-score">
            <strong>${finite(stock.score)?.toFixed(0) ?? "--"}</strong>
            <span>趋势评分</span>
          </div>
        </div>
        <div class="stock-price-row">
          <span class="stock-price">${formatPrice(stock.price)}</span>
          <span class="stock-change ${directionClass(stock.change_pct)}">${formatPct(
            stock.change_pct
          )}</span>
          <span class="stock-status ${statusClass}">${escapeHtml(stock.status)}</span>
        </div>
        <div class="stock-metrics">
          <div class="stock-metric"><span>5日动量</span><strong class="${directionClass(
            stock.return_5d
          )}">${formatPct(stock.return_5d)}</strong></div>
          <div class="stock-metric"><span>20日动量</span><strong class="${directionClass(
            stock.return_20d
          )}">${formatPct(stock.return_20d)}</strong></div>
          <div class="stock-metric"><span>20日波动</span><strong>${finite(
            stock.volatility_20d
          )?.toFixed(0) ?? "--"}%</strong></div>
        </div>
        <div class="stock-plan">
          <div><b>动作</b><span>${escapeHtml(stock.action)}</span></div>
          <div><b>回踩</b><span>${escapeHtml(formatPrice(plan.pullback_entry_low))}—${escapeHtml(
            formatPrice(plan.pullback_entry_high)
          )}，收盘不破20日线</span></div>
          <div><b>突破</b><span>${escapeHtml(formatPrice(plan.breakout_trigger))} 且量能确认</span></div>
          <div><b>止损</b><span>${escapeHtml(formatPrice(plan.stop_price))}；${escapeHtml(
            plan.execution || ""
          )}</span></div>
          <div><b>目标</b><span>${escapeHtml(formatPrice(plan.target_1))} / ${escapeHtml(
            formatPrice(plan.target_2)
          )}</span></div>
        </div>
      </article>`;
    })
    .join("");
}

function renderBigMoney(data, money) {
  if (!money) return;
  $("money-confidence").textContent = `置信度 ${money.confidence || "低"}`;
  $("money-state").textContent = money.state || "数据不足";
  $("money-score").textContent =
    finite(money.score) === null
      ? "--"
      : `${money.score > 0 ? "+" : ""}${money.score.toFixed(0)}`;
  $("money-evidence-list").innerHTML = (money.evidence || [])
    .map((item) => `<li>${escapeHtml(item)}</li>`)
    .join("");
  const stocks = data.active_stocks?.stocks || [];
  $("money-stock-list").innerHTML = stocks
    .map((stock) => {
      const behavior = stock.capital_behavior || {};
      return `<div class="money-stock-row">
        <span>${escapeHtml(stock.name)} · ${escapeHtml(behavior.state || "数据不足")}</span>
        <span>${finite(behavior.score) === null ? "--" : behavior.score.toFixed(0)}</span>
      </div>`;
    })
    .join("");
}

function renderFramework(framework) {
  if (!framework) return;
  $("framework-state").textContent = framework.state || "--";
  $("framework-posture").textContent = framework.posture || "--";
  const consistency = framework.consistency || {};
  $("framework-consistency").textContent = `${
    consistency.description || "--"
  }（${finite(consistency.consensus_pct)?.toFixed(0) ?? "--"}%）`;
  const funnel = framework.candidate_funnel || {};
  $("framework-funnel").textContent = `${funnel.market_universe ?? "--"} → ${
    funnel.liquid_screen ?? "--"
  } → ${funnel.technical_pass ?? "--"} → ${funnel.trade_ready ?? "--"} 可执行`;
  $("framework-gates").innerHTML = (framework.gates || [])
    .map(
      (gate) => `<div class="framework-gate ${escapeHtml(gate.status)}">
        <b>${escapeHtml(gate.name)}</b>
        <span>${escapeHtml(gate.detail)}</span>
        <span>${escapeHtml(gate.implication)}</span>
      </div>`
    )
    .join("");
  $("framework-scenarios").innerHTML = (framework.scenarios || [])
    .map(
      (scenario) => `<div class="framework-scenario">
        <strong>${escapeHtml(scenario.name)}</strong><br>
        ${(scenario.triggers || []).map(escapeHtml).join("；")}<br>
        动作：${escapeHtml(scenario.action)}
      </div>`
    )
    .join("");
}

function renderAdaptive(model) {
  if (!model) return;
  $("adaptive-status").textContent = model.status || "积累样本";
  $("adaptive-predictions").textContent = model.prediction_count ?? "--";
  $("adaptive-completed").textContent = `${model.completed_5d ?? "--"} / ${
    model.completed_20d ?? "--"
  }`;
  $("adaptive-hit-rate").textContent = formatPct(model.metrics?.hit_rate_5d, false);
  $("adaptive-updates").textContent = model.update_count ?? "0";
  const labels = { trend: "趋势", flow: "资金", breadth: "广度", futures: "期指" };
  $("adaptive-weights").innerHTML = Object.keys(labels)
    .map((key) => {
      const weight = finite(model.weights?.[key])?.toFixed(1) ?? "--";
      return `<div class="adaptive-weight"><span>${labels[key]}</span><b>${weight}%</b></div>`;
    })
    .join("");
}

async function loadActiveStocks(force = false) {
  if (state.stockLoading) return;
  state.stockLoading = true;
  const button = $("refresh-stocks");
  button.disabled = true;
  button.textContent = force ? "正在扫描全市场" : "正在载入";
  try {
    const data = await requestJson(
      `/api/active-stocks${force ? "?refresh=1" : ""}`,
      { cache: "no-store" },
      1
    );
    if (!data.ok) throw new Error(data.message || "主动选股失败");
    renderActiveStocks(data);
    if (force) {
      showToast(`主动选股完成：${data.stocks.length} 只进入候选`);
    }
  } catch (error) {
    $("active-stock-list").innerHTML = `<div class="empty-state">${escapeHtml(
      error.message
    )}</div>`;
    showToast(error.message, true);
  } finally {
    state.stockLoading = false;
    button.disabled = false;
    button.textContent = "重新选股";
  }
}

function renderPlanner(plan) {
  if (!plan) return;
  $("target-range").textContent = `${plan.target_low}% — ${plan.target_high}%`;
  const gap = finite(plan.gap_pct);
  $("position-gap").textContent = gap === null
    ? "--"
    : `${gap > 0 ? "+" : ""}${gap.toFixed(1)}%`;
  $("position-gap").className = directionClass(gap);
  $("first-action").textContent = plan.action || "--";
}

function renderDashboard(data) {
  state.dashboard = data;
  updateClockText(data);
  renderScore(data.strategy || {});
  renderMetrics(data);
  renderFlowChart(data);
  renderFactors(data.strategy?.factors);
  renderBreadth(data);
  renderFutures(data.futures);
  renderSectors(data.sectors);
  renderIndices(data.indices);
  renderRules(data.strategy?.rules);
  renderRiskFlags(data.strategy?.risk_flags);
  renderSources(data);
  renderAssetDecision(data.asset_decision);
  renderFramework(data.framework);
  renderAdaptive(data.adaptive_model);
  renderBigMoney(data, data.big_money);
  if (data.active_stocks) {
    renderActiveStocks(data.active_stocks);
  } else {
    loadActiveStocks();
  }
}

async function loadDashboard() {
  try {
    const data = await requestJson(
      "/api/dashboard",
      { cache: "no-store" },
      3
    );
    if (!data.ok) {
      showToast(data.message || "暂无数据", true);
      return;
    }
    setConnectionState(true);
    renderDashboard(data);
    await loadDefaultAssets();
    await updatePlanner();
  } catch (error) {
    setConnectionState(false, error.message);
    showToast(error.message, true);
  }
}

async function loadDefaultAssets() {
  try {
    const data = await requestJson(
      "/api/assets",
      { cache: "no-store" },
      1
    );
    if (!data.ok) return;
    renderAssetDecision(data.decision, data.etf_candidates);
  } catch {
    // The cached dashboard already contains the core asset decision.
  }
}

async function checkAssets() {
  const raw = $("asset-codes").value;
  const codes = raw
    .split(/[\s,，;；]+/)
    .map((item) => item.replace(/\D/g, ""))
    .filter((item) => item.length === 6)
    .slice(0, 10);
  if (!codes.length) {
    showToast("请输入至少一个 6 位证券代码。", true);
    $("asset-codes").focus();
    return;
  }
  const button = $("check-assets");
  button.disabled = true;
  button.textContent = "检查中";
  try {
    const params = new URLSearchParams({
      codes: codes.join(","),
      role: state.assetRole,
    });
    const data = await requestJson(
      `/api/assets?${params.toString()}`,
      { cache: "no-store" },
      1
    );
    if (!data.ok) throw new Error(data.message || "检查失败");
    renderAssetDecision(data.decision);
    renderAssetResults(data.assets, state.assetRole);
    showToast(`已完成 ${data.assets.length} 个标的的趋势检查`);
  } catch (error) {
    showToast(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "开始检查";
  }
}

async function updatePlanner() {
  const capital = Number($("planner-capital").value) || 0;
  const position = Number($("planner-position").value) || 0;
  $("planner-position-value").textContent = `${position}%`;
  try {
    const params = new URLSearchParams({
      risk: state.risk,
      capital: String(capital),
      position: String(position),
    });
    const data = await requestJson(
      `/api/plan?${params.toString()}`,
      { cache: "no-store" },
      1
    );
    if (!data.ok) return;
    renderPlanner(data.plan);
    if (state.dashboard) {
      state.dashboard.strategy = data.strategy;
    }
  } catch {
    const exposure = state.dashboard?.strategy?.exposure || { low: 0, high: 0, mid: 0 };
    const gap = exposure.mid - position;
    renderPlanner({
      target_low: exposure.low,
      target_high: exposure.high,
      gap_pct: gap,
      action: Math.abs(gap) <= 3 ? "维持现有仓位" : gap > 0 ? "分批加仓" : "分批减仓",
    });
  }
}

async function refreshData() {
  if (state.loading) return;
  state.loading = true;
  const button = $("refresh-button");
  button.disabled = true;
  button.classList.add("loading");
  button.lastChild.textContent = " 正在抓取";
  showToast("正在抓取公开行情，通常需要 10—30 秒。");
  try {
    const result = await requestJson(
      "/api/refresh",
      { method: "POST" },
      1
    );
    if (!result.ok) throw new Error(result.message || "刷新失败");
    showToast(result.message || "行情已刷新");
    await loadDashboard();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    state.loading = false;
    button.disabled = false;
    button.classList.remove("loading");
    button.lastChild.textContent = " 刷新数据";
  }
}

async function openMobileAccess() {
  const dialog = $("mobile-dialog");
  const button = $("mobile-share-button");
  const input = $("mobile-link");
  const preview = $("preview-mobile-link");
  dialog.hidden = false;
  button.disabled = true;
  input.value = "正在建立加密公网通道，通常需要 5—20 秒…";
  preview.setAttribute("aria-disabled", "true");
  preview.removeAttribute("href");
  try {
    const data = await requestJson(
      "/api/mobile-link?public=1",
      { cache: "no-store" },
      1
    );
    if (!data.ok || !data.url) {
      throw new Error(data.message || "公网链接创建失败");
    }
    input.value = data.url;
    preview.href = data.url;
    preview.removeAttribute("aria-disabled");
    input.focus();
    input.select();
    showToast(`公网链接已就绪（${data.provider || "加密隧道"}）`);
  } catch (error) {
    input.value = `创建失败：${error.message}`;
    showToast(error.message, true);
  } finally {
    button.disabled = false;
  }
}

function closeMobileAccess() {
  $("mobile-dialog").hidden = true;
}

async function copyMobileLink() {
  const input = $("mobile-link");
  if (!input.value || input.value.startsWith("创建失败")) {
    showToast("公网链接还没有生成完成。", true);
    return;
  }
  try {
    await navigator.clipboard.writeText(input.value);
  } catch {
    input.focus();
    input.select();
    document.execCommand("copy");
  }
  showToast("手机访问链接已复制");
}

let plannerTimer = null;
$("retry-connection").addEventListener("click", loadDashboard);
$("mobile-share-button").addEventListener("click", openMobileAccess);
$("copy-mobile-link").addEventListener("click", copyMobileLink);
document.querySelectorAll("[data-close-mobile-dialog]").forEach((element) => {
  element.addEventListener("click", closeMobileAccess);
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !$("mobile-dialog").hidden) {
    closeMobileAccess();
  }
});
$("refresh-button").addEventListener("click", refreshData);
$("planner-capital").addEventListener("input", () => {
  window.clearTimeout(plannerTimer);
  plannerTimer = window.setTimeout(updatePlanner, 220);
});
$("planner-position").addEventListener("input", () => {
  window.clearTimeout(plannerTimer);
  plannerTimer = window.setTimeout(updatePlanner, 100);
});
document.querySelectorAll("[data-risk]").forEach((button) => {
  button.addEventListener("click", () => {
    state.risk = button.dataset.risk;
    document.querySelectorAll("[data-risk]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    updatePlanner();
  });
});
document.querySelectorAll("[data-asset-role]").forEach((button) => {
  button.addEventListener("click", () => {
    state.assetRole = button.dataset.assetRole;
    document.querySelectorAll("[data-asset-role]").forEach((item) => {
      item.classList.toggle("active", item === button);
    });
    $("asset-results").innerHTML =
      state.assetRole === "holdings"
        ? '<div class="empty-state">录入实际持仓后，软件才能明确给出“卖出哪只”。</div>'
        : '<div class="empty-state">录入观察池代码，软件会筛掉跌破20日线或市场偏弱时不具备买入条件的标的。</div>';
  });
});
$("check-assets").addEventListener("click", checkAssets);
$("refresh-stocks").addEventListener("click", () => loadActiveStocks(true));
$("asset-codes").addEventListener("keydown", (event) => {
  if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
    checkAssets();
  }
});

loadDashboard();

if (!window.ASHARE_STATIC && "serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {});
  });
}
