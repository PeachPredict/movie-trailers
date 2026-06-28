"use strict";

// Fetch a JSON file under ./data; resolve null on any failure so a missing or
// empty section degrades gracefully instead of breaking the page.
async function getJSON(name) {
  try {
    const r = await fetch("./data/" + name, { cache: "no-cache" });
    if (!r.ok) return null;
    return await r.json();
  } catch (_e) {
    return null;
  }
}

function fmtNum(n) {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e9) return (n / 1e9).toFixed(1) + "B";
  if (a >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (a >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function hideSection(node) {
  if (node) node.style.display = "none";
}

// --- Coverage stats (hero) ------------------------------------------------ //
function renderCoverage(cov) {
  const host = document.getElementById("coverage-stats");
  if (!cov || !host) return hideSection(host);
  const items = [
    [fmtNum(cov.unique_trailers), "trailers tracked"],
    [String(cov.countries_covered ?? "—"), "countries"],
    [fmtNum(cov.active_trailers), "actively tracked"],
    [fmtNum(cov.with_comments), "with comments"],
  ];
  for (const [num, lbl] of items) {
    const box = el("div", "stat");
    box.appendChild(el("div", "num", num));
    box.appendChild(el("div", "lbl", lbl));
    host.appendChild(box);
  }
}

// --- Carousel ------------------------------------------------------------- //
function renderCarousel(rows) {
  const host = document.getElementById("carousel");
  if (!rows || !rows.length || !host) return hideSection(document.getElementById("trending-section"));
  for (const m of rows) {
    const card = el("div", "poster");
    card.setAttribute("role", "listitem");
    const a = el("a");
    a.href = "https://www.youtube.com/watch?v=" + encodeURIComponent(m.youtube_video_id);
    a.target = "_blank";
    a.rel = "noopener";
    const img = el("img");
    img.src = m.poster_url;
    img.alt = m.title || "poster";
    img.loading = "lazy";
    a.appendChild(img);
    a.appendChild(el("div", "t", m.title || ""));
    a.appendChild(el("div", "v", "+" + fmtNum(m.views_gained) + " views · 30d"));
    card.appendChild(a);
    host.appendChild(card);
  }
}

// --- Time-series chart (views over time, launches starred) ---------------- //
const PALETTE = ["#ff7a59", "#58a6ff", "#3fb950", "#d2a8ff", "#e3b341"];

function renderViewsChart(data) {
  const canvas = document.getElementById("views-chart");
  const cap = document.getElementById("views-caption");
  const movies = (data && data.movies) || [];
  if (!canvas || !movies.length) {
    if (cap) cap.textContent = "View time-series will appear here once stats are available.";
    return;
  }
  const color = (i) => PALETTE[i % PALETTE.length];

  const datasets = movies.map((m, i) => {
    const launches = new Set(m.launches || []);
    const c = color(i);
    return {
      label: `${m.title} — ${m.n_trailers} trailers`,
      // log scale can't plot ≤ 0; null those points and span the gaps.
      data: m.series.map((p) => ({ x: p.day, y: p.dv > 0 ? p.dv : null })),
      borderColor: c,
      backgroundColor: c,
      tension: 0.3,
      spanGaps: true,
      pointStyle: m.series.map((p) => (launches.has(p.day) ? "star" : "circle")),
      pointRadius: m.series.map((p) => (launches.has(p.day) ? 12 : 0)),
      pointHoverRadius: m.series.map((p) => (launches.has(p.day) ? 14 : 3)),
      pointBorderColor: c,
      pointBackgroundColor: m.series.map((p) => (launches.has(p.day) ? "#fff" : c)),
    };
  });

  new Chart(canvas, {
    type: "line",
    data: { datasets },
    options: {
      maintainAspectRatio: false,
      interaction: { intersect: false, mode: "nearest", axis: "x" },
      scales: {
        x: { type: "linear", title: { display: true, text: "Days tracked" },
             ticks: { precision: 0 } },
        y: { type: "logarithmic", title: { display: true, text: "Daily new views (log scale)" },
             ticks: { callback: (v) => fmtNum(v) } },
      },
      plugins: {
        legend: { position: "bottom" },
        tooltip: { callbacks: {
          title: (it) => "Day " + it[0].parsed.x,
          label: (it) => it.dataset.label.split(" — ")[0] + ": " + fmtNum(it.parsed.y) + " views/day",
        } },
      },
    },
  });
  if (cap) {
    cap.textContent =
      "Absolute daily new views on a log scale, so both audiences and the decay tails stay visible. " +
      "★ marks a new-trailer launch: the many-trailer film keeps re-spiking at each star while the " +
      "fewer-trailer film fades.";
  }
}

function renderEngagementStats(dec) {
  const host = document.getElementById("engagement-stats");
  if (!dec || !dec.units || !host) return hideSection(host);
  const pct = (x) => (x == null ? "—" : (x * 100).toFixed(0) + "%");
  const add = (label, value) => {
    const s = el("span");
    s.appendChild(document.createTextNode(label + " "));
    s.appendChild(el("b", null, value));
    host.appendChild(s);
  };
  add("Movies losing engagement:", dec.losing + "/" + dec.units + " (" + pct(dec.losing_share) + ")");
  add("· median change:", (dec.median_pct_change * 100).toFixed(1) + "%");
  add("· window:", dec.days + "d");
}

// --- MCP explorer --------------------------------------------------------- //
function renderMcpExplorer(data) {
  const list = document.getElementById("mcp-tools");
  const detail = document.getElementById("mcp-detail");
  const tools = (data && data.tools) || [];
  if (!tools.length || !list) return hideSection(document.getElementById("mcp-explorer"));

  function show(tool, btn) {
    for (const b of list.querySelectorAll("button")) b.classList.remove("active");
    if (btn) btn.classList.add("active");
    detail.textContent = "";
    detail.appendChild(el("div", "sig", tool.signature || tool.name));
    detail.appendChild(el("div", "desc", tool.description || ""));
    detail.appendChild(el("div", "lbl", "Example request"));
    detail.appendChild(makePre(tool.example_args ?? {}));
    detail.appendChild(el("div", "lbl", "Example response"));
    if (tool.example_response == null) {
      detail.appendChild(el("p", "desc", "(not available on the current dataset)"));
    } else {
      detail.appendChild(makePre(tool.example_response));
    }
  }
  function makePre(obj) {
    const pre = el("pre");
    pre.textContent = JSON.stringify(obj, null, 2);
    return pre;
  }

  tools.forEach((tool, i) => {
    const li = el("li");
    const btn = el("button", null, tool.name);
    btn.setAttribute("role", "tab");
    btn.addEventListener("click", () => show(tool, btn));
    li.appendChild(btn);
    list.appendChild(li);
    if (i === 0) show(tool, btn);
  });
}

// --- Distillation validation facts ---------------------------------------- //
function renderDistillation(stat) {
  const facts = document.getElementById("dose-facts");
  if (!stat || !stat.validation || !facts) return;
  const v = stat.validation;
  const dr = v.dose_response_pts || {};
  const add = (label, value) => {
    const s = el("span");
    s.appendChild(document.createTextNode(label + " "));
    s.appendChild(el("b", null, value));
    facts.appendChild(s);
  };
  add("Teacher labels:", fmtNum(stat.teacher && stat.teacher.labels));
  add("· student↔teacher ρ:", String(v.spearman_rho));
  add("· movies declining:", Math.round(v.declining_share * 100) + "%");
  add("· lexicon proxy (blind):", Math.round(v.lexicon_proxy_share * 100) + "%");
  if (dr["2"] != null) add("· decay by trailers (2 / 3–4 / 5+):", `${dr["2"]} / ${dr["3-4"]} / ${dr["5+"]} pts`);
}

// --- Footer meta ---------------------------------------------------------- //
function renderMeta(meta) {
  const line = document.getElementById("meta-line");
  if (!line) return;
  if (!meta || !meta.generated_at) { line.textContent = ""; return; }
  const when = new Date(meta.generated_at);
  const nice = isNaN(when) ? meta.generated_at : when.toUTCString();
  line.textContent = "Data refreshed " + nice + (meta.model_version ? " · model " + meta.model_version : "");
}

// --- Boot ----------------------------------------------------------------- //
(async function main() {
  if (window.Chart) {
    Chart.defaults.color = "#9aa7b4";
    Chart.defaults.borderColor = "#2a313c";
    Chart.defaults.font.family = getComputedStyle(document.body).fontFamily;
  }
  const [cov, carousel, views, engage, mcp, distill, meta] = await Promise.all([
    getJSON("coverage.json"),
    getJSON("carousel.json"),
    getJSON("views_timeseries.json"),
    getJSON("engagement_decay.json"),
    getJSON("mcp_examples.json"),
    getJSON("distillation_static.json"),
    getJSON("meta.json"),
  ]);
  renderCoverage(cov);
  renderCarousel(carousel);
  renderViewsChart(views);
  renderEngagementStats(engage);
  renderMcpExplorer(mcp);
  renderDistillation(distill);
  renderMeta(meta);
})();
