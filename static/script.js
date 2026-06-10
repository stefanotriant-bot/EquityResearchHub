// ---------- Formatting helpers ----------
const fmt = {
  money(n, opts = {}) {
    if (n == null) return '—';
    const abs = Math.abs(n);
    if (abs >= 1e12) return `$${(n / 1e12).toFixed(2)}T`;
    if (abs >= 1e9)  return `$${(n / 1e9).toFixed(2)}B`;
    if (abs >= 1e6)  return `$${(n / 1e6).toFixed(2)}M`;
    if (abs >= 1e3 && !opts.preserveSmall) return `$${(n / 1e3).toFixed(2)}K`;
    return `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  },
  num(n) {
    if (n == null) return '—';
    const abs = Math.abs(n);
    if (abs >= 1e12) return `${(n / 1e12).toFixed(2)}T`;
    if (abs >= 1e9)  return `${(n / 1e9).toFixed(2)}B`;
    if (abs >= 1e6)  return `${(n / 1e6).toFixed(2)}M`;
    if (abs >= 1e3)  return `${(n / 1e3).toFixed(2)}K`;
    return n.toLocaleString();
  },
  int(n) { return n == null ? '—' : Math.round(n).toLocaleString(); },
  ratio(n, digits = 2) { return n == null ? '—' : n.toFixed(digits); },
  pct(n, digits = 2) {
    if (n == null) return '—';
    // yfinance returns some pcts as fractions (0.21) and some as integers (21).
    const v = Math.abs(n) <= 2 ? n * 100 : n;
    return `${v.toFixed(digits)}%`;
  },
  pctRaw(n, digits = 2) { return n == null ? '—' : `${n.toFixed(digits)}%`; },
  date(s) {
    if (!s) return '—';
    const d = new Date(s);
    if (isNaN(d)) return s;
    return d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
  },
  rel(s) {
    if (!s) return '';
    const d = new Date(s);
    if (isNaN(d)) return '';
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return 'just now';
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    if (diff < 604800) return `${Math.floor(diff / 86400)}d ago`;
    return fmt.date(s);
  },
};

const escape = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

// ---------- DOM elements ----------
const el = {
  form: document.getElementById('searchForm'),
  input: document.getElementById('tickerInput'),
  loading: document.getElementById('loading'),
  error: document.getElementById('error'),
  results: document.getElementById('results'),
  hero: document.getElementById('hero'),
  headerMeta: document.getElementById('headerMeta'),
};

// ---------- Event wiring ----------
el.form.addEventListener('submit', (e) => {
  e.preventDefault();
  const t = el.input.value.trim().toUpperCase();
  if (t) runResearch(t);
});

document.querySelectorAll('.chip').forEach(btn => {
  btn.addEventListener('click', () => {
    const t = btn.dataset.ticker;
    el.input.value = t;
    runResearch(t);
  });
});

// Auto-run if URL has ?ticker= and user is logged in
if (el.input && el.input.value && window.APP_USER) {
  runResearch(el.input.value.trim().toUpperCase());
}

// ---------- Research flow ----------
async function runResearch(ticker) {
  showLoading();
  hideError();
  hideResults();
  try {
    const res = await fetch(`/api/research?ticker=${encodeURIComponent(ticker)}`);
    const data = await res.json();
    if (res.status === 401 || data.authRequired) {
      window.location.href = '/signup?next=' + encodeURIComponent('/?ticker=' + ticker);
      return;
    }
    if (!res.ok) {
      if (data.upgradeRequired) {
        showUpgradeError(data.error, data.upgradeUrl || '/subscribe');
        return;
      }
      throw new Error(data.error || `Request failed (${res.status})`);
    }
    render(data);
    showResults();
    setTimeout(() => el.results.scrollIntoView({ behavior: 'smooth', block: 'start' }), 50);
  } catch (e) {
    showError(e.message);
  } finally {
    hideLoading();
  }
}

function showUpgradeError(message, url) {
  el.error.innerHTML = `
    <strong>${escape(message)}</strong>
    <div style="margin-top:14px;">
      <a class="btn-primary" href="${url}" style="display:inline-flex;">View plans →</a>
    </div>
  `;
  el.error.classList.remove('hidden');
}

function showLoading() { el.loading.classList.remove('hidden'); }
function hideLoading() { el.loading.classList.add('hidden'); }
function showError(msg) {
  el.error.innerHTML = `<strong>Couldn't compile research.</strong><br>${escape(msg)}`;
  el.error.classList.remove('hidden');
}
function hideError() { el.error.classList.add('hidden'); }
function showResults() { el.results.classList.remove('hidden'); }
function hideResults() { el.results.classList.add('hidden'); }

// ---------- Rendering ----------
function render(d) {
  if (el.headerMeta) {
    el.headerMeta.innerHTML = `Last updated <strong>${fmt.rel(d.fetchedAt)}</strong>`;
  }

  // Group sections into 4 focused tabs so the page isn't a wall of data
  const overviewHTML = [
    renderAIBriefSection(d),
    renderHistorySection(d),
    renderNewsSection(d),
    renderEarningsCalendarSection(d),
  ].join('');
  const numbersHTML = [
    renderFinancialsSection(d),
    renderValuationSection(d),
    renderForecastsSection(d),
    renderDCFSection(d),
  ].join('');
  const marketHTML = [
    renderPeersSection(d),
    renderSectorSection(d),
    renderAnalystSection(d),
    renderHoldersSection(d),
    renderESGSection(d),
  ].join('');
  const filingsHTML = [
    renderFilingsSection(d),
    renderCapitalEventsSection(d),
    renderLegalSection(d),
  ].join('');

  const tabsHTML = `
    <div class="result-tabs">
      <nav class="result-tabnav" role="tablist" aria-label="Research sections">
        <button type="button" class="result-tab active" data-tab="overview" role="tab" aria-selected="true">
          <span class="result-tab-label">Overview</span>
          <span class="result-tab-sub">What's happening</span>
        </button>
        <button type="button" class="result-tab" data-tab="numbers" role="tab" aria-selected="false">
          <span class="result-tab-label">Numbers</span>
          <span class="result-tab-sub">Financials &amp; valuation</span>
        </button>
        <button type="button" class="result-tab" data-tab="market" role="tab" aria-selected="false">
          <span class="result-tab-label">Market view</span>
          <span class="result-tab-sub">Peers, analysts, holders</span>
        </button>
        <button type="button" class="result-tab" data-tab="filings" role="tab" aria-selected="false">
          <span class="result-tab-label">Filings</span>
          <span class="result-tab-sub">SEC &amp; events</span>
        </button>
      </nav>
      <div class="result-tabpanel active" data-tab="overview" role="tabpanel">${overviewHTML}</div>
      <div class="result-tabpanel" data-tab="numbers" role="tabpanel">${numbersHTML}</div>
      <div class="result-tabpanel" data-tab="market" role="tabpanel">${marketHTML}</div>
      <div class="result-tabpanel" data-tab="filings" role="tabpanel">${filingsHTML}</div>
    </div>
  `;

  el.results.innerHTML = [
    renderCompanyHero(d),
    renderQuoteSection(d),
    tabsHTML,
    renderExportSection(d),
    renderLockedFeatures(d),
  ].join('');

  // Wire tab switching
  const tabs = el.results.querySelectorAll('.result-tab');
  const panels = el.results.querySelectorAll('.result-tabpanel');
  tabs.forEach(tab => {
    tab.addEventListener('click', () => {
      const target = tab.dataset.tab;
      tabs.forEach(t => {
        const isActive = t === tab;
        t.classList.toggle('active', isActive);
        t.setAttribute('aria-selected', isActive ? 'true' : 'false');
      });
      panels.forEach(p => p.classList.toggle('active', p.dataset.tab === target));
      // Keep the tab nav visible after switching (in case user was scrolled down)
      const nav = el.results.querySelector('.result-tabnav');
      if (nav) {
        const navTop = nav.getBoundingClientRect().top + window.scrollY;
        if (window.scrollY > navTop - 20) {
          window.scrollTo({ top: navTop - 80, behavior: 'smooth' });
        }
      }
    });
  });

  // Wire summary expand toggle
  const toggle = el.results.querySelector('.summary-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const summary = el.results.querySelector('.summary-text');
      const collapsed = summary.classList.toggle('collapsed');
      toggle.textContent = collapsed ? 'Read more →' : 'Show less ↑';
    });
  }
  // Wire filing tabs
  el.results.querySelectorAll('.filing-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      const cat = tab.dataset.category;
      tab.parentElement.querySelectorAll('.filing-tab').forEach(t => t.classList.remove('active'));
      tab.classList.add('active');
      const list = tab.closest('.section').querySelectorAll('.filings-list');
      list.forEach(l => l.classList.toggle('hidden', l.dataset.category !== cat));
    });
  });
  // Wire interactive price chart hover
  setupPriceChartHover(d.history);
  // Wire bar chart hover tooltips
  setupBarChartHover();
  // Wire save-to-watchlist button
  wireSaveButton(d);
  // Wire DCF widget
  wireDCFWidget(d);
  // Wire SMA toggle on price chart
  wireSMAToggle(d);
  // Wire export
  wireExportButton(d);
  // Wire AI brief button
  wireAIBriefButton(d);
  // Wire PDF print button
  wirePDFButton(d);
}

function wireAIBriefButton(d) {
  const btn = el.results.querySelector('#aiBriefBtn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    const out = el.results.querySelector('#aiBriefContent');
    btn.disabled = true;
    btn.innerHTML = '<span>Generating brief, this takes 10-30 seconds…</span>';
    try {
      const res = await fetch(`/api/ai-brief/${d.ticker}`);
      const data = await res.json();
      if (data.brief) {
        out.innerHTML = `
          <div class="ai-brief-output">
            ${data.brief.split('\n').map(p => p.trim() ? `<p>${escape(p)}</p>` : '').join('')}
          </div>
          <p class="ai-brief-meta muted">
            Generated ${data.cached ? '(cached)' : 'fresh'} · ${escape(data.generatedAt.slice(0,10))} · AI summaries can contain errors, always verify in primary sources.
          </p>
        `;
        btn.style.display = 'none';
      } else if (data.upgradeRequired) {
        btn.disabled = false;
        btn.innerHTML = '<span>Generate AI brief →</span>';
        if (confirm(data.error + '\n\nView plans?')) {
          window.location.href = data.upgradeUrl || '/subscribe';
        }
      } else if (data.configMissing) {
        out.innerHTML = `<p class="error-text">${escape(data.error)}</p>`;
        btn.style.display = 'none';
      } else {
        btn.disabled = false;
        btn.innerHTML = '<span>Try again</span>';
        alert(data.error || 'Could not generate brief.');
      }
    } catch (e) {
      btn.disabled = false;
      btn.innerHTML = '<span>Try again</span>';
      alert('Network error: ' + e.message);
    }
  });
}

function wirePDFButton(d) {
  const btn = el.results.querySelector('#pdfBtn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    window.open(`/research/${d.ticker}/print`, '_blank');
  });
}

function wireDCFWidget(d) {
  const form = el.results.querySelector('#dcfForm');
  if (!form) return;
  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const out = el.results.querySelector('#dcfResult');
    out.innerHTML = '<p class="muted">Calculating…</p>';
    const payload = {
      fcf: parseFloat(form.fcf.value),
      growthHigh: parseFloat(form.growthHigh.value),
      growthTerm: parseFloat(form.growthTerm.value),
      yearsHigh: parseInt(form.yearsHigh.value, 10),
      discount: parseFloat(form.discount.value),
      shares: parseFloat(form.shares.value),
      netDebt: parseFloat(form.netDebt.value || 0),
    };
    try {
      const res = await fetch('/api/dcf', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(payload),
      });
      const r = await res.json();
      if (!res.ok) { out.innerHTML = `<p class="error-text">${escape(r.error)}</p>`; return; }
      const cur = d.quote && d.quote.price;
      const intrinsic = r.intrinsicPerShare;
      const upside = (cur && intrinsic) ? ((intrinsic - cur) / cur * 100) : null;
      out.innerHTML = `
        <div class="dcf-result-grid">
          <div class="dcf-result-tile">
            <div class="stat-label">Intrinsic value / share</div>
            <div class="stat-value">${intrinsic ? '$' + intrinsic.toFixed(2) : '—'}</div>
          </div>
          <div class="dcf-result-tile">
            <div class="stat-label">Current price</div>
            <div class="stat-value">${cur ? '$' + cur.toFixed(2) : '—'}</div>
          </div>
          <div class="dcf-result-tile" style="background: ${upside > 0 ? 'var(--success-bg)' : upside < 0 ? 'var(--danger-bg)' : 'var(--bg-card-warm)'}">
            <div class="stat-label">Upside / downside</div>
            <div class="stat-value" style="color: ${upside > 0 ? 'var(--success)' : upside < 0 ? 'var(--danger)' : 'var(--text-primary)'}">${upside != null ? (upside > 0 ? '+' : '') + upside.toFixed(1) + '%' : '—'}</div>
          </div>
        </div>
        <p class="dcf-meta muted">Enterprise value ${fmt.money(r.enterpriseValue)} · Equity value ${fmt.money(r.equityValue)} · Terminal value ${fmt.money(r.terminalValue)}</p>
      `;
    } catch (e) {
      out.innerHTML = `<p class="error-text">Network error: ${escape(e.message)}</p>`;
    }
  });
}

function wireSMAToggle(d) {
  const toggle = el.results.querySelector('#smaToggle');
  if (!toggle) return;
  toggle.addEventListener('change', () => {
    const showSMA = toggle.checked;
    el.results.querySelectorAll('.sma-line').forEach(line => {
      line.style.display = showSMA ? '' : 'none';
    });
    el.results.querySelector('.sma-legend')?.classList.toggle('hidden', !showSMA);
  });
}

function wireExportButton(d) {
  const btn = el.results.querySelector('#exportBtn');
  if (!btn) return;
  btn.addEventListener('click', () => {
    window.location.href = `/api/research/export/${d.ticker}`;
  });
}

function wireSaveButton(d) {
  const btn = el.results.querySelector('.save-watchlist-btn');
  if (!btn) return;
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    const orig = btn.innerHTML;
    btn.textContent = 'Saving…';
    try {
      const res = await fetch('/api/watchlist/add', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ticker: d.ticker}),
      });
      const result = await res.json();
      if (result.success) {
        btn.classList.add('saved');
        btn.innerHTML = `
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M20 6 9 17l-5-5"/></svg>
          <span>Saved to Watchlist</span>
        `;
        btn.disabled = false;
      } else if (result.upgradeRequired) {
        btn.innerHTML = orig;
        btn.disabled = false;
        if (confirm(result.error + '\n\nView plans?')) {
          window.location.href = result.upgradeUrl || '/subscribe';
        }
      } else {
        btn.innerHTML = orig;
        btn.disabled = false;
        alert(result.error || 'Could not save.');
      }
    } catch (e) {
      btn.innerHTML = orig;
      btn.disabled = false;
      alert('Network error: ' + e.message);
    }
  });
}

function renderLockedFeatures(d) {
  if (!d.locked || !d.locked.length) return '';
  const labels = {
    aiBrief: 'AI-generated company brief (flagship Pro feature)',
    researchJournal: 'Research journal with thesis tracking',
    alerts: 'Custom price and metric alerts',
    pdfExport: 'PDF research note export',
    history: '12-month interactive price chart',
    fullFinancials: 'Full income, balance sheet & cash flow statements',
    fullValuation: 'Complete valuation & quality metrics',
    filings: 'SEC filings (10-K, 10-Q, 8-K, proxies)',
    fullFilings: 'All SEC filings (10-Q, 8-K, proxies, insider forms)',
    news: 'Recent news feed',
    holders: 'Top institutional holders (13F)',
    analyst: 'Analyst targets & buy/sell consensus',
    legal: 'Legal & material events feed',
    forecasts: 'Analyst forecasts and consensus estimates',
    earnings: 'Earnings calendar with surprise history',
    capitalEvents: 'Buybacks, M&A, debt issuance timeline',
  };
  const items = d.locked.map(k => labels[k]).filter(Boolean);
  if (!items.length) return '';
  const tier = d.tier || 'free';
  return `
    <div class="section locked-section">
      <div class="locked-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect width="18" height="11" x="3" y="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
      </div>
      <h3>${tier === 'apprentice' ? 'Patron-only research' : 'Premium research locked'}</h3>
      <p class="locked-subtitle">Subscribe to unlock the rest of the file:</p>
      <ul class="locked-list">${items.map(x => `<li>${x}</li>`).join('')}</ul>
      <a class="btn-primary" href="/subscribe" style="display:inline-flex;">${tier === 'apprentice' ? 'Upgrade to Patron' : 'View plans'} →</a>
    </div>
  `;
}

// ---------- Sections ----------
function renderCompanyHero(d) {
  const o = d.overview;
  const q = d.quote;
  const summary = o.summary ? `
    <p class="summary-text collapsed">${escape(o.summary)}</p>
    <button class="summary-toggle" type="button">Read more →</button>
  ` : `<p class="muted">No business summary available.</p>`;

  const tags = [
    o.sector ? `<span class="tag tag-deep">${escape(o.sector)}</span>` : '',
    o.industry ? `<span class="tag">${escape(o.industry)}</span>` : '',
    o.country ? `<span class="tag">${escape(o.country)}</span>` : '',
  ].filter(Boolean).join('');

  const meta = [
    o.ceo ? ['CEO', escape(o.ceo)] : null,
    o.employees ? ['Employees', fmt.int(o.employees)] : null,
    (o.city || o.state) ? ['Headquarters', escape([o.city, o.state].filter(Boolean).join(', '))] : null,
    o.website ? ['Website', `<a href="${escape(o.website)}" target="_blank" rel="noopener">${escape(o.website.replace(/^https?:\/\//, ''))}</a>`] : null,
    q.marketCap ? ['Market cap', fmt.money(q.marketCap)] : null,
  ].filter(Boolean).map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join('');

  return `
    <div class="company-hero">
      <div class="company-hero-grid">
        <div>
          <div class="ticker-row">
            <span class="ticker-symbol">${escape(o.ticker)}</span>
            ${o.exchange ? `<span class="ticker-exchange">${escape(o.exchange)}</span>` : ''}
          </div>
          <div class="company-name">${escape(o.name)}</div>
          ${tags ? `<div class="tag-row">${tags}</div>` : ''}
          ${summary}
          ${window.APP_USER ? `
            <button type="button" class="save-watchlist-btn">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M19 21V5a2 2 0 0 0-2-2H7a2 2 0 0 0-2 2v16l7-3 7 3z"/></svg>
              <span>Save to Watchlist</span>
            </button>
          ` : ''}
        </div>
        <dl class="company-meta-list">${meta}</dl>
      </div>
    </div>
  `;
}

function renderQuoteSection(d) {
  const q = d.quote;
  if (q.price == null) return '';
  const cls = q.change > 0 ? 'up' : q.change < 0 ? 'down' : 'flat';
  const sign = q.change > 0 ? '+' : '';
  const changeStr = q.change != null ? `${sign}${q.change.toFixed(2)} (${sign}${q.changePct.toFixed(2)}%)` : '';

  const stats = [
    ['Open', q.open != null ? `$${q.open.toFixed(2)}` : '—'],
    ['Prev Close', q.previousClose != null ? `$${q.previousClose.toFixed(2)}` : '—'],
    ['Day Range', q.dayLow && q.dayHigh ? `$${q.dayLow.toFixed(2)} – $${q.dayHigh.toFixed(2)}` : '—'],
    ['52-Week Range', q.fiftyTwoWeekLow && q.fiftyTwoWeekHigh ? `$${q.fiftyTwoWeekLow.toFixed(2)} – $${q.fiftyTwoWeekHigh.toFixed(2)}` : '—'],
    ['Volume', fmt.num(q.volume)],
    ['Avg Volume', fmt.num(q.averageVolume)],
    ['Market Cap', fmt.money(q.marketCap)],
    ['Shares Out', fmt.num(q.sharesOutstanding)],
  ].map(([k, v]) => `<div class="quote-stat"><dt>${k}</dt><dd>${v}</dd></div>`).join('');

  return `
    <div class="section quote-card">
      <div class="section-header">
        <h3 class="section-title">Market Quote</h3>
        <span class="section-subtitle">${escape(q.currency || 'USD')}</span>
      </div>
      <div class="quote-price-row">
        <span class="quote-price">$${q.price.toFixed(2)}</span>
        ${q.change != null ? `<span class="quote-change ${cls}">${changeStr}</span>` : ''}
      </div>
      <dl class="quote-grid">${stats}</dl>
    </div>
  `;
}

function renderHistorySection(d) {
  if (!d.history || d.history.length < 2) return '';
  const v = d.volumeAnalysis || {};
  const stats = [
    v.totalReturn1Y != null ? ['1Y Return', (v.totalReturn1Y > 0 ? '+' : '') + v.totalReturn1Y.toFixed(2) + '%'] : null,
    v.pctOffHigh != null ? ['Off 52W High', '-' + v.pctOffHigh.toFixed(2) + '%'] : null,
    v.pctOffLow != null ? ['Off 52W Low', '+' + v.pctOffLow.toFixed(2) + '%'] : null,
    v.annualizedVolatility != null ? ['Annualized Volatility', v.annualizedVolatility.toFixed(1) + '%'] : null,
  ].filter(Boolean).map(([k, val]) => `
    <div class="stat-tile"><div class="stat-label">${k}</div><div class="stat-value">${val}</div></div>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">12-Month Price History</h3>
        <span class="section-subtitle">
          <label class="sma-toggle-label">
            <input id="smaToggle" type="checkbox"> Show 50/200-day moving averages
          </label>
        </span>
      </div>
      ${linePriceChart(d.history, d.movingAverages)}
      ${stats ? `<div class="stat-grid" style="margin-top: 18px;">${stats}</div>` : ''}
    </div>
  `;
}

function renderFinancialsSection(d) {
  const f = d.financials;
  const charts = [
    barChart(f.income.revenue, 'Revenue', 'Annual'),
    barChart(f.income.netIncome, 'Net Income', 'Annual'),
    barChart(f.cash.freeCashFlow.length ? f.cash.freeCashFlow : f.cash.operating, f.cash.freeCashFlow.length ? 'Free Cash Flow' : 'Operating Cash Flow', 'Annual'),
    barChart(f.income.operatingIncome, 'Operating Income', 'Annual'),
    barChart(f.balance.totalAssets, 'Total Assets', 'Year-End'),
    barChart(f.balance.totalDebt, 'Total Debt', 'Year-End'),
  ].join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Financial Performance</h3>
        <span class="section-subtitle">From Reported Statements</span>
      </div>
      <div class="chart-grid">${charts}</div>
    </div>
  `;
}

function renderValuationSection(d) {
  const v = d.valuation;
  const tiles = [
    ['P/E (TTM)', fmt.ratio(v.peRatio), 'trailing earnings'],
    ['Forward P/E', fmt.ratio(v.forwardPE), 'expected earnings'],
    ['PEG Ratio', fmt.ratio(v.pegRatio), 'P/E vs growth'],
    ['Price / Book', fmt.ratio(v.priceToBook), 'vs book value'],
    ['Price / Sales', fmt.ratio(v.priceToSales), 'TTM revenue'],
    ['EV / EBITDA', fmt.ratio(v.evToEbitda), 'enterprise value'],
    ['Profit Margin', fmt.pct(v.profitMargin), 'net'],
    ['Operating Margin', fmt.pct(v.operatingMargin), ''],
    ['Gross Margin', fmt.pct(v.grossMargin), ''],
    ['Return on Equity', fmt.pct(v.returnOnEquity), 'profitability'],
    ['Return on Assets', fmt.pct(v.returnOnAssets), ''],
    ['ROIC', fmt.pct(v.returnOnInvestedCapital), 'return on invested capital'],
    ['FCF Yield', fmt.pct(v.fcfYield), 'free cash flow / market cap'],
    ['Debt / Equity', fmt.ratio(v.debtToEquity, 0), 'leverage'],
    ['Current Ratio', fmt.ratio(v.currentRatio), 'liquidity'],
    ['Beta', fmt.ratio(v.beta), 'volatility vs market'],
    ['Dividend Yield', fmt.pct(v.dividendYield), v.payoutRatio ? `payout ${fmt.pct(v.payoutRatio)}` : ''],
    ['EPS (TTM)', v.eps != null ? `$${v.eps.toFixed(2)}` : '—', v.forwardEps ? `fwd $${v.forwardEps.toFixed(2)}` : ''],
    ['Revenue Growth', fmt.pct(v.revenueGrowth), 'YoY'],
    ['Earnings Growth', fmt.pct(v.earningsGrowth), 'YoY'],
  ].map(([label, value, sub]) => `
    <div class="stat-tile">
      <div class="stat-label">${label}</div>
      <div class="stat-value">${value}</div>
      ${sub ? `<div class="stat-sub">${sub}</div>` : ''}
    </div>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Valuation &amp; Quality Metrics</h3>
        <span class="section-subtitle">Trailing 12 Months</span>
      </div>
      <div class="stat-grid">${tiles}</div>
    </div>
  `;
}

function renderAnalystSection(d) {
  const t = d.analystTargets || {};
  const r = d.recommendations;
  if (t.targetMean == null && !r) return '';

  const targetRows = [
    ['Mean Target', t.targetMean != null ? `$${t.targetMean.toFixed(2)}` : '—'],
    ['Median Target', t.targetMedian != null ? `$${t.targetMedian.toFixed(2)}` : '—'],
    ['High Target', t.targetHigh != null ? `$${t.targetHigh.toFixed(2)}` : '—'],
    ['Low Target', t.targetLow != null ? `$${t.targetLow.toFixed(2)}` : '—'],
    ['Analysts Covering', t.numAnalysts ?? '—'],
  ].map(([k, v]) => `<div class="target-row"><span class="label">${k}</span><span class="value">${v}</span></div>`).join('');

  let recBlock = '';
  if (r) {
    const total = r.strongBuy + r.buy + r.hold + r.sell + r.strongSell;
    if (total > 0) {
      const seg = (label, cls, n) => `<div class="rec-segment rec-${cls}" style="flex: ${n};" data-zero="${n === 0}">${n}</div>`;
      const dot = (cls, label, n) => `<div class="rec-legend-item"><span class="rec-dot rec-${cls}"></span>${label} <span class="mono muted">(${n})</span></div>`;
      recBlock = `
        ${t.recommendationKey ? `<span class="rec-key">${escape(t.recommendationKey.replace(/_/g, ' '))}</span>` : ''}
        <div class="recommendation-bar">
          ${seg('Strong Buy','strongBuy', r.strongBuy)}
          ${seg('Buy','buy', r.buy)}
          ${seg('Hold','hold', r.hold)}
          ${seg('Sell','sell', r.sell)}
          ${seg('Strong Sell','strongSell', r.strongSell)}
        </div>
        <div class="rec-legend">
          ${dot('strongBuy', 'Strong Buy', r.strongBuy)}
          ${dot('buy', 'Buy', r.buy)}
          ${dot('hold', 'Hold', r.hold)}
          ${dot('sell', 'Sell', r.sell)}
          ${dot('strongSell', 'Strong Sell', r.strongSell)}
        </div>
      `;
    }
  }

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Analyst View</h3>
        <span class="section-subtitle">Sell-Side Coverage</span>
      </div>
      <div class="analyst-grid">
        <div class="target-card">${targetRows}</div>
        <div>${recBlock || '<p class="muted">No analyst recommendation breakdown available.</p>'}</div>
      </div>
    </div>
  `;
}

function renderFilingsSection(d) {
  const f = d.filings;
  if (!f.available) {
    return `
      <div class="section">
        <div class="section-header"><h3 class="section-title">SEC Filings</h3></div>
        <p class="no-data">${escape(f.reason || 'SEC filings unavailable for this ticker.')}</p>
      </div>
    `;
  }

  const cats = [
    ['10-K', '10-K · Annual'],
    ['10-Q', '10-Q · Quarterly'],
    ['8-K', '8-K · Material Events'],
    ['DEF 14A', 'Proxy'],
    ['other', 'Other'],
  ];

  const tabs = cats.map(([key, label], i) => {
    const count = (f.categorized[key] || []).length;
    if (count === 0 && key !== '10-K') return '';
    return `<button type="button" class="filing-tab ${i === 0 ? 'active' : ''}" data-category="${key}">${label}<span class="count">${count}</span></button>`;
  }).filter(Boolean).join('');

  const lists = cats.map(([key], i) => {
    const items = f.categorized[key] || [];
    const rows = items.length === 0
      ? `<p class="no-data">No filings of this type in the recent index.</p>`
      : items.map(item => `
        <div class="filing-row">
          <div class="filing-info">
            <span class="filing-form-badge">${escape(item.form)}</span>
            <div class="filing-text">
              <div class="filing-description">${escape(item.description || `${item.form} filing`)}</div>
              <div class="filing-date">Filed ${fmt.date(item.filingDate)} · ${escape(item.accession)}</div>
            </div>
          </div>
          <a class="filing-link" href="${escape(item.documentUrl)}" target="_blank" rel="noopener">
            Open document
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 7h10v10"/><path d="M7 17 17 7"/></svg>
          </a>
        </div>
      `).join('');
    return `<div class="filings-list ${i === 0 ? '' : 'hidden'}" data-category="${key}">${rows}</div>`;
  }).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">SEC Filings</h3>
        <span class="section-subtitle">EDGAR · CIK ${escape(f.cik)}</span>
      </div>
      <div class="filings-tabs">${tabs}</div>
      ${lists}
      <div class="filings-footer">
        <a href="${escape(f.edgarUrl)}" target="_blank" rel="noopener">View full EDGAR profile →</a>
      </div>
    </div>
  `;
}

function renderLegalSection(d) {
  const items = d.legalSignals || [];
  if (items.length === 0) return '';
  const rows = items.map(item => `
    <a class="legal-item" href="${escape(item.url)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">
      <div class="legal-icon">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <path d="M12 9v4"/><path d="M12 17h.01"/><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/>
        </svg>
      </div>
      <div class="legal-content">
        <div class="legal-type">${escape(item.type)}</div>
        <div class="legal-meta">${fmt.date(item.date)}</div>
        ${item.description ? `<div class="legal-desc">${escape(item.description)}</div>` : ''}
      </div>
    </a>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Legal &amp; Material Events</h3>
        <span class="section-subtitle">From SEC Disclosures</span>
      </div>
      <div class="legal-callout">
        <strong>Where to look.</strong> Companies disclose lawsuits, regulatory actions, and other material events in <strong>8-K filings</strong> and detail ongoing legal matters in the <strong>10-K (Item 3 · Legal Proceedings)</strong> and <strong>Item 1A · Risk Factors</strong>. Open the filings below to read primary source language.
      </div>
      <div class="legal-list">${rows}</div>
    </div>
  `;
}

function renderHoldersSection(d) {
  if (!d.holders || d.holders.length === 0) return '';
  const rows = d.holders.map(h => `
    <tr>
      <td class="holder-name">${escape(h.holder || '—')}</td>
      <td class="right">${fmt.num(h.shares)}</td>
      <td class="right">${fmt.money(h.value)}</td>
      <td class="right">${h.pctOut != null ? fmt.pct(h.pctOut) : '—'}</td>
      <td class="right muted">${h.dateReported ? fmt.date(h.dateReported) : '—'}</td>
    </tr>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Top Institutional Holders</h3>
        <span class="section-subtitle">13F-Reported Positions</span>
      </div>
      <table class="holders-table">
        <thead>
          <tr><th>Holder</th><th class="right">Shares</th><th class="right">Value</th><th class="right">% Out</th><th class="right">Reported</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
  `;
}

function renderNewsSection(d) {
  if (!d.news || d.news.length === 0) return '';
  const items = d.news.map(n => `
    <a class="news-item" href="${escape(n.url)}" target="_blank" rel="noopener">
      <div class="news-title">${escape(n.title)}</div>
      <div class="news-meta">
        ${n.publisher ? `<span class="news-publisher">${escape(n.publisher)}</span>` : ''}
        ${n.published ? `<span>${fmt.rel(n.published)}</span>` : ''}
      </div>
    </a>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Recent News</h3>
        <span class="section-subtitle">Yahoo Finance Aggregation</span>
      </div>
      <div class="news-list">${items}</div>
    </div>
  `;
}

// ---------- Charts (inline SVG) ----------
function barChart(series, title, sub) {
  const width = 320;
  const height = 180;
  const padTop = 24, padBottom = 28, padLeft = 8, padRight = 8;

  if (!series || series.length === 0) {
    return `
      <div class="chart-block">
        <div class="chart-title">${title}</div>
        <div class="chart-sub">${sub}</div>
        <div class="chart-empty">Not reported.</div>
      </div>
    `;
  }

  const data = series.slice(-5);
  const values = data.map(p => p.value);
  const minV = Math.min(0, ...values);
  const maxV = Math.max(0, ...values);
  const range = (maxV - minV) || 1;

  const innerH = height - padTop - padBottom;
  const innerW = width - padLeft - padRight;
  const barWidth = innerW / data.length * 0.62;
  const slot = innerW / data.length;

  const yFor = v => padTop + ((maxV - v) / range) * innerH;
  const zeroY = yFor(0);

  const bars = data.map((p, i) => {
    const x = padLeft + slot * i + (slot - barWidth) / 2;
    const y = yFor(p.value);
    const h = Math.abs(zeroY - y);
    const top = p.value >= 0 ? y : zeroY;
    const negative = p.value < 0;
    const year = p.date.slice(0, 4);
    return `
      <g class="bar-group" data-year="${year}" data-value="${formatBarLabel(p.value)}" data-full="${p.value.toLocaleString(undefined, { maximumFractionDigits: 0 })}">
        <rect class="bar-hit" x="${padLeft + slot * i}" y="${padTop}" width="${slot}" height="${height - padTop - padBottom}" fill="transparent"/>
        <rect class="bar ${negative ? 'negative' : ''}" x="${x}" y="${top}" width="${barWidth}" height="${h}" rx="2"/>
        <text class="bar-label" x="${x + barWidth / 2}" y="${p.value >= 0 ? y - 6 : top + h + 12}" text-anchor="middle">${formatBarLabel(p.value)}</text>
        <text class="bar-axis" x="${x + barWidth / 2}" y="${height - 8}" text-anchor="middle">${year}</text>
      </g>
    `;
  }).join('');

  return `
    <div class="chart-block bar-chart-container" data-title="${escape(title)}">
      <div class="chart-title">${title}</div>
      <div class="chart-sub">${sub}</div>
      <svg class="chart-svg bar-chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none">
        ${bars}
      </svg>
      <div class="bar-tooltip">
        <div class="bar-tooltip-year"></div>
        <div class="bar-tooltip-value"></div>
        <div class="bar-tooltip-full"></div>
      </div>
    </div>
  `;
}

function formatBarLabel(v) {
  const abs = Math.abs(v);
  if (abs >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
  if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(0)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(0)}K`;
  return v.toFixed(0);
}

function linePriceChart(history, smas) {
  const width = 1000;
  const height = 220;
  const padLeft = 50, padRight = 18, padTop = 18, padBottom = 28;

  const values = history.map(p => p.close);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const range = (maxV - minV) || 1;
  const padded = range * 0.08;
  const yMin = minV - padded;
  const yMax = maxV + padded;
  const yRange = yMax - yMin;

  const innerW = width - padLeft - padRight;
  const innerH = height - padTop - padBottom;

  const xFor = i => padLeft + (i / (history.length - 1)) * innerW;
  const yFor = v => padTop + ((yMax - v) / yRange) * innerH;

  const points = history.map((p, i) => `${xFor(i).toFixed(1)},${yFor(p.close).toFixed(1)}`).join(' ');
  const fillPath = `M ${xFor(0).toFixed(1)},${(padTop + innerH).toFixed(1)} L ${points.split(' ').join(' L ')} L ${xFor(history.length - 1).toFixed(1)},${(padTop + innerH).toFixed(1)} Z`;

  // y-axis ticks
  const tickValues = [yMax, (yMax + yMin) / 2, yMin];
  const ticks = tickValues.map(v => `
    <line x1="${padLeft}" y1="${yFor(v)}" x2="${width - padRight}" y2="${yFor(v)}" stroke="#efe6d4" stroke-width="1" stroke-dasharray="2 4"/>
    <text class="bar-axis" x="${padLeft - 8}" y="${yFor(v) + 3}" text-anchor="end">$${v.toFixed(0)}</text>
  `).join('');

  // x-axis date labels (start, mid, end)
  const xLabels = [0, Math.floor(history.length / 2), history.length - 1].map(i => {
    const date = new Date(history[i].date);
    const label = date.toLocaleDateString(undefined, { month: 'short', year: '2-digit' });
    return `<text class="bar-axis" x="${xFor(i)}" y="${height - 8}" text-anchor="middle">${label}</text>`;
  }).join('');

  // start vs end change
  const start = history[0].close;
  const end = history[history.length - 1].close;
  const change = ((end - start) / start) * 100;
  const changeColor = change >= 0 ? 'var(--success)' : 'var(--danger)';
  const changeText = `${change >= 0 ? '+' : ''}${change.toFixed(2)}% over period`;

  // SMA overlay polylines
  const dateToIndex = new Map(history.map((p, i) => [p.date, i]));
  function smaPoints(series) {
    if (!series || !series.length) return '';
    return series
      .map(p => {
        const i = dateToIndex.get(p.date);
        if (i == null) return null;
        const value = p.value;
        if (value < yMin || value > yMax) return null;
        return `${xFor(i).toFixed(1)},${yFor(value).toFixed(1)}`;
      })
      .filter(Boolean)
      .join(' ');
  }
  const sma50Points = smas ? smaPoints(smas.sma50) : '';
  const sma200Points = smas ? smaPoints(smas.sma200) : '';

  return `
    <div class="price-chart-container">
      <svg class="chart-svg price-chart-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" style="height:240px;">
        <defs>
          <linearGradient id="priceGradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="#1f3a36" stop-opacity="0.18"/>
            <stop offset="100%" stop-color="#1f3a36" stop-opacity="0"/>
          </linearGradient>
        </defs>
        ${ticks}
        <path class="price-fill" d="${fillPath}"/>
        <polyline class="price-line" points="${points}"/>
        ${sma50Points ? `<polyline class="sma-line sma-50" points="${sma50Points}" style="display:none;"/>` : ''}
        ${sma200Points ? `<polyline class="sma-line sma-200" points="${sma200Points}" style="display:none;"/>` : ''}
        ${xLabels}
        <text class="bar-axis" x="${width - padRight}" y="${padTop - 4}" text-anchor="end" style="font-weight:600; fill:${changeColor};">${changeText}</text>
        <line class="hover-line" x1="0" y1="${padTop}" x2="0" y2="${padTop + innerH}"/>
        <circle class="hover-dot" cx="0" cy="0" r="5"/>
        <rect class="hover-overlay" x="${padLeft}" y="${padTop}" width="${innerW}" height="${innerH}" fill="transparent"/>
      </svg>
      ${(sma50Points || sma200Points) ? `
        <div class="sma-legend hidden">
          ${sma50Points ? '<span class="sma-legend-item"><span class="sma-swatch sma-50-swatch"></span>50-day SMA</span>' : ''}
          ${sma200Points ? '<span class="sma-legend-item"><span class="sma-swatch sma-200-swatch"></span>200-day SMA</span>' : ''}
        </div>
      ` : ''}
      <div class="price-tooltip">
        <div class="price-tooltip-date"></div>
        <div class="price-tooltip-price"></div>
      </div>
    </div>
  `;
}

// ---------- Hover interactions ----------
function setupPriceChartHover(history) {
  if (!history || history.length < 2) return;
  const container = el.results.querySelector('.price-chart-container');
  if (!container) return;

  const svg = container.querySelector('svg');
  const overlay = container.querySelector('.hover-overlay');
  const hoverLine = container.querySelector('.hover-line');
  const hoverDot = container.querySelector('.hover-dot');
  const tooltip = container.querySelector('.price-tooltip');
  const tipDate = container.querySelector('.price-tooltip-date');
  const tipPrice = container.querySelector('.price-tooltip-price');

  // Match constants used in linePriceChart
  const width = 1000, height = 220;
  const padLeft = 50, padRight = 18, padTop = 18, padBottom = 28;
  const innerW = width - padLeft - padRight;
  const innerH = height - padTop - padBottom;

  const values = history.map(p => p.close);
  const minV = Math.min(...values);
  const maxV = Math.max(...values);
  const range = (maxV - minV) || 1;
  const padded = range * 0.08;
  const yMin = minV - padded;
  const yMax = maxV + padded;
  const yRange = yMax - yMin;

  const xFor = i => padLeft + (i / (history.length - 1)) * innerW;
  const yFor = v => padTop + ((yMax - v) / yRange) * innerH;

  const handleMove = (e) => {
    const svgRect = svg.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    const mouseX = e.clientX - svgRect.left;
    const viewboxX = (mouseX / svgRect.width) * width;

    let i = Math.round((viewboxX - padLeft) / innerW * (history.length - 1));
    i = Math.max(0, Math.min(history.length - 1, i));

    const point = history[i];
    const xVb = xFor(i);
    const yVb = yFor(point.close);

    hoverLine.setAttribute('x1', xVb);
    hoverLine.setAttribute('x2', xVb);
    hoverDot.setAttribute('cx', xVb);
    hoverDot.setAttribute('cy', yVb);
    container.classList.add('hover-active');

    const pxX = (xVb / width) * svgRect.width + (svgRect.left - containerRect.left);
    const pxY = (yVb / height) * svgRect.height + (svgRect.top - containerRect.top);

    tipDate.textContent = new Date(point.date).toLocaleDateString(undefined, {
      month: 'short', day: 'numeric', year: 'numeric',
    });
    tipPrice.textContent = `$${point.close.toFixed(2)}`;

    const flipLeft = pxX > containerRect.width * 0.6;
    tooltip.style.left = `${pxX}px`;
    tooltip.style.top = `${pxY}px`;
    tooltip.style.transform = flipLeft
      ? 'translate(calc(-100% - 14px), -50%)'
      : 'translate(14px, -50%)';
  };

  const handleLeave = () => container.classList.remove('hover-active');

  overlay.addEventListener('mousemove', handleMove);
  overlay.addEventListener('mouseleave', handleLeave);
}

function setupBarChartHover() {
  el.results.querySelectorAll('.bar-chart-container').forEach(container => {
    const tooltip = container.querySelector('.bar-tooltip');
    const tipYear = container.querySelector('.bar-tooltip-year');
    const tipValue = container.querySelector('.bar-tooltip-value');
    const tipFull = container.querySelector('.bar-tooltip-full');
    const groups = container.querySelectorAll('.bar-group');

    groups.forEach(g => {
      const hit = g.querySelector('.bar-hit');
      const bar = g.querySelector('.bar');

      const onEnter = () => {
        bar.classList.add('active');
        tipYear.textContent = g.dataset.year;
        tipValue.textContent = g.dataset.value;
        tipFull.textContent = g.dataset.full;
        container.classList.add('hover-active');
      };
      const onMove = (e) => {
        const rect = container.getBoundingClientRect();
        const x = e.clientX - rect.left;
        const y = e.clientY - rect.top;
        const flipLeft = x > rect.width * 0.6;
        tooltip.style.left = `${x}px`;
        tooltip.style.top = `${y}px`;
        tooltip.style.transform = flipLeft
          ? 'translate(calc(-100% - 12px), -110%)'
          : 'translate(12px, -110%)';
      };
      const onLeave = () => {
        bar.classList.remove('active');
        container.classList.remove('hover-active');
      };

      hit.addEventListener('mouseenter', onEnter);
      hit.addEventListener('mousemove', onMove);
      hit.addEventListener('mouseleave', onLeave);
    });
  });
}

// ---------- New render sections ----------
function renderForecastsSection(d) {
  const f = d.forecasts;
  if (!f || (!f.items?.length && f.targetMeanPrice == null && f.forwardEps == null)) return '';
  const tiles = [
    f.targetMeanPrice ? ['Mean Target Price', '$' + f.targetMeanPrice.toFixed(2)] : null,
    f.forwardEps ? ['Forward EPS', '$' + f.forwardEps.toFixed(2)] : null,
    f.forwardPE ? ['Forward P/E', f.forwardPE.toFixed(2)] : null,
    f.revenueGrowth != null ? ['Revenue Growth (forecast)', fmt.pct(f.revenueGrowth)] : null,
    f.earningsGrowth != null ? ['Earnings Growth (forecast)', fmt.pct(f.earningsGrowth)] : null,
  ].filter(Boolean).map(([k, v]) => `
    <div class="stat-tile">
      <div class="stat-label">${k}</div>
      <div class="stat-value">${v}</div>
    </div>
  `).join('');

  const items = (f.items || []).map(item => `
    <tr>
      <td class="mono">${escape(item.period)}</td>
      <td>${escape(item.type)}</td>
      <td class="right mono">${item.value != null ? (item.type.includes('Revenue') ? fmt.money(item.value) : '$' + item.value.toFixed(2)) : '—'}</td>
      <td class="right mono muted">${item.low != null && item.high != null ? (item.type.includes('Revenue') ? fmt.money(item.low) + ' – ' + fmt.money(item.high) : '$' + item.low.toFixed(2) + ' – $' + item.high.toFixed(2)) : '—'}</td>
      <td class="right mono muted">${item.analysts ? item.analysts : '—'}</td>
    </tr>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Forecasts &amp; Estimates</h3>
        <span class="section-subtitle">Forward Looking</span>
      </div>
      ${tiles ? `<div class="stat-grid">${tiles}</div>` : ''}
      ${items ? `
        <table class="affiliate-table" style="margin-top: 18px;">
          <thead><tr><th>Period</th><th>Estimate</th><th class="right">Average</th><th class="right">Range</th><th class="right">Analysts</th></tr></thead>
          <tbody>${items}</tbody>
        </table>
      ` : ''}
    </div>
  `;
}

function renderEarningsCalendarSection(d) {
  const ec = d.earningsCalendar;
  if (!ec || (!ec.nextDate && !ec.history?.length)) return '';

  const next = ec.nextDate ? `
    <div class="earnings-next-card">
      <div class="earnings-next-label">Next earnings</div>
      <div class="earnings-next-date">${fmt.date(ec.nextDate)}</div>
      ${ec.estEps ? `<div class="earnings-next-meta">Estimate: $${ec.estEps.toFixed(2)} EPS${ec.estRevenue ? ` · ${fmt.money(ec.estRevenue)} revenue` : ''}</div>` : ''}
    </div>
  ` : '';

  const history = (ec.history || []).map(h => {
    const surprise = h.surprisePct;
    const cls = surprise > 0 ? 'beat' : surprise < 0 ? 'miss' : '';
    return `
      <tr>
        <td class="mono">${fmt.date(h.date)}</td>
        <td class="right mono">${h.epsEstimate != null ? '$' + h.epsEstimate.toFixed(2) : '—'}</td>
        <td class="right mono"><strong>${h.epsActual != null ? '$' + h.epsActual.toFixed(2) : '—'}</strong></td>
        <td class="right mono surprise-${cls}">${surprise != null ? (surprise > 0 ? '+' : '') + surprise.toFixed(1) + '%' : '—'}</td>
      </tr>
    `;
  }).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Earnings Calendar &amp; History</h3>
        <span class="section-subtitle">Estimates vs. Actuals</span>
      </div>
      ${next}
      ${history ? `
        <table class="affiliate-table" style="margin-top: 18px;">
          <thead><tr><th>Quarter Ended</th><th class="right">Estimate</th><th class="right">Actual</th><th class="right">Surprise</th></tr></thead>
          <tbody>${history}</tbody>
        </table>
      ` : ''}
    </div>
  `;
}

function renderPeersSection(d) {
  if (!d.peers || d.peers.length === 0) return '';
  const subjectMetrics = d.valuation || {};
  const allRows = [
    {
      ticker: d.ticker,
      name: (d.overview && d.overview.name) || d.ticker,
      price: d.quote ? d.quote.price : null,
      marketCap: d.quote ? d.quote.marketCap : null,
      peRatio: subjectMetrics.peRatio,
      priceToBook: subjectMetrics.priceToBook,
      profitMargin: subjectMetrics.profitMargin,
      returnOnEquity: subjectMetrics.returnOnEquity,
      revenueGrowth: subjectMetrics.revenueGrowth,
      dividendYield: subjectMetrics.dividendYield,
      isSubject: true,
    },
    ...d.peers,
  ];
  const rows = allRows.map(p => `
    <tr ${p.isSubject ? 'class="peer-subject"' : ''}>
      <td><strong>${escape(p.ticker)}</strong>${p.isSubject ? ' <span class="peer-self-tag">You</span>' : ''}</td>
      <td class="muted">${escape((p.name || '').substring(0, 28))}</td>
      <td class="right mono">${p.price != null ? '$' + p.price.toFixed(2) : '—'}</td>
      <td class="right mono">${fmt.money(p.marketCap)}</td>
      <td class="right mono">${p.peRatio != null ? p.peRatio.toFixed(1) : '—'}</td>
      <td class="right mono">${p.priceToBook != null ? p.priceToBook.toFixed(2) : '—'}</td>
      <td class="right mono">${fmt.pct(p.profitMargin)}</td>
      <td class="right mono">${fmt.pct(p.returnOnEquity)}</td>
      <td class="right mono">${fmt.pct(p.revenueGrowth)}</td>
      <td class="right mono">${fmt.pct(p.dividendYield)}</td>
    </tr>
  `).join('');

  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Peer Comparison</h3>
        <span class="section-subtitle">Same Sector</span>
      </div>
      <div style="overflow-x: auto;">
        <table class="affiliate-table peers-table">
          <thead>
            <tr>
              <th>Ticker</th><th>Name</th><th class="right">Price</th><th class="right">Mkt Cap</th>
              <th class="right">P/E</th><th class="right">P/B</th><th class="right">Profit Margin</th>
              <th class="right">ROE</th><th class="right">Rev Growth</th><th class="right">Div Yield</th>
            </tr>
          </thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
    </div>
  `;
}

function renderSectorSection(d) {
  if (!d.sectorPerformance) return '';
  const s = d.sectorPerformance;
  const cls1y = s.return1Y > 0 ? 'up' : s.return1Y < 0 ? 'down' : '';
  const cls1m = s.return1M > 0 ? 'up' : s.return1M < 0 ? 'down' : '';
  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Sector Performance</h3>
        <span class="section-subtitle">Reference ETF: ${escape(s.etf)}</span>
      </div>
      <div class="stat-grid">
        <div class="stat-tile"><div class="stat-label">${escape(s.etf)} Price</div><div class="stat-value">$${s.price ? s.price.toFixed(2) : '—'}</div></div>
        <div class="stat-tile"><div class="stat-label">1-Month Return</div><div class="stat-value sector-${cls1m}">${s.return1M != null ? (s.return1M > 0 ? '+' : '') + s.return1M.toFixed(2) + '%' : '—'}</div></div>
        <div class="stat-tile"><div class="stat-label">1-Year Return</div><div class="stat-value sector-${cls1y}">${s.return1Y != null ? (s.return1Y > 0 ? '+' : '') + s.return1Y.toFixed(2) + '%' : '—'}</div></div>
      </div>
    </div>
  `;
}

function renderESGSection(d) {
  if (!d.esg) return '';
  const e = d.esg;
  if (e.totalEsg == null && e.environmentScore == null) return '';
  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">ESG Scores</h3>
        <span class="section-subtitle">Environmental, Social, Governance</span>
      </div>
      <div class="stat-grid">
        <div class="stat-tile"><div class="stat-label">Total ESG Risk</div><div class="stat-value">${e.totalEsg != null ? e.totalEsg.toFixed(1) : '—'}</div><div class="stat-sub">${escape(e.esgPerformance || '')}</div></div>
        <div class="stat-tile"><div class="stat-label">Environment</div><div class="stat-value">${e.environmentScore != null ? e.environmentScore.toFixed(1) : '—'}</div></div>
        <div class="stat-tile"><div class="stat-label">Social</div><div class="stat-value">${e.socialScore != null ? e.socialScore.toFixed(1) : '—'}</div></div>
        <div class="stat-tile"><div class="stat-label">Governance</div><div class="stat-value">${e.governanceScore != null ? e.governanceScore.toFixed(1) : '—'}</div></div>
        ${e.controversyLevel != null ? `<div class="stat-tile"><div class="stat-label">Controversy Level</div><div class="stat-value">${e.controversyLevel}/5</div></div>` : ''}
      </div>
    </div>
  `;
}

function renderCapitalEventsSection(d) {
  if (!d.capitalEvents || d.capitalEvents.length === 0) return '';
  const items = d.capitalEvents.slice(0, 12).map(ev => `
    <a class="legal-item" href="${escape(ev.url)}" target="_blank" rel="noopener" style="text-decoration:none;color:inherit;">
      <div class="legal-icon" style="background: var(--accent-warm-bg); color: var(--accent-warm);">
        ${ev.type === 'Buyback' ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 12a9 9 0 1 0 9-9"/><path d="M3 4v5h5"/></svg>' :
          ev.type === 'Acquisition' ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M16 16v1a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h2"/><path d="M21 12V7a2 2 0 0 0-2-2h-9a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h7"/><circle cx="12" cy="12" r="2"/></svg>' :
          '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg>'}
      </div>
      <div class="legal-content">
        <div class="legal-type">${escape(ev.type)}</div>
        <div class="legal-meta">${fmt.date(ev.date)}</div>
        ${ev.description ? `<div class="legal-desc">${escape(ev.description)}</div>` : ''}
      </div>
    </a>
  `).join('');
  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">Capital Events Timeline</h3>
        <span class="section-subtitle">Buybacks, M&amp;A, Debt</span>
      </div>
      <div class="legal-list">${items}</div>
    </div>
  `;
}

function renderDCFSection(d) {
  if (!d.valuation) return '';
  const fcfDefault = d.valuation.freeCashFlowTtm || 1e9;
  const sharesDefault = d.quote && d.quote.sharesOutstanding ? d.quote.sharesOutstanding : 1e9;
  const debt = (d.valuation.totalDebtTtm || 0) - (d.valuation.totalCashTtm || 0);
  return `
    <div class="section">
      <div class="section-header">
        <h3 class="section-title">DCF Valuation Calculator</h3>
        <span class="section-subtitle">Two-Stage Discounted Cash Flow</span>
      </div>
      <p class="muted" style="font-size: 13px; margin-bottom: 16px;">Estimate intrinsic value by discounting projected cash flows. Defaults are pre-filled from this company's data, change them based on your own assumptions.</p>
      <form id="dcfForm" class="dcf-form">
        <div class="dcf-grid">
          <div class="auth-field">
            <label>Starting FCF ($)</label>
            <input name="fcf" type="number" step="any" value="${fcfDefault}" required>
          </div>
          <div class="auth-field">
            <label>High Growth Rate (%)</label>
            <input name="growthHigh" type="number" step="0.1" value="10" required>
          </div>
          <div class="auth-field">
            <label>Years of High Growth</label>
            <input name="yearsHigh" type="number" step="1" min="1" max="20" value="5" required>
          </div>
          <div class="auth-field">
            <label>Terminal Growth (%)</label>
            <input name="growthTerm" type="number" step="0.1" value="3" required>
          </div>
          <div class="auth-field">
            <label>Discount Rate (%)</label>
            <input name="discount" type="number" step="0.1" value="9" required>
          </div>
          <div class="auth-field">
            <label>Shares Outstanding</label>
            <input name="shares" type="number" step="any" value="${sharesDefault}" required>
          </div>
          <div class="auth-field">
            <label>Net Debt ($)</label>
            <input name="netDebt" type="number" step="any" value="${debt}">
          </div>
        </div>
        <button type="submit" class="btn-primary">Calculate intrinsic value</button>
      </form>
      <div id="dcfResult" class="dcf-result"></div>
    </div>
  `;
}

function renderExportSection(d) {
  if (d.tier === 'free') return '';
  const ticker = d.ticker;
  return `
    <div class="section export-section">
      <div class="section-header">
        <h3 class="section-title">Save &amp; Export</h3>
        <span class="section-subtitle">Your work, your files</span>
      </div>
      <p class="muted" style="font-size: 13px; margin-bottom: 16px;">Capture what you've researched and built around this stock.</p>
      <div class="export-actions">
        <a href="/journal/${ticker}" class="btn-secondary">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M9 13h6"/><path d="M9 17h6"/></svg>
          <span>Open thesis in journal</span>
        </a>
        <button id="pdfBtn" type="button" class="btn-secondary">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;"><rect width="14" height="20" x="5" y="2" rx="2"/><path d="M9 13h6"/><path d="M9 17h3"/></svg>
          <span>Generate PDF note</span>
        </button>
        <button id="exportBtn" type="button" class="btn-secondary">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>
          <span>Download CSV</span>
        </button>
      </div>
    </div>
  `;
}

function renderAIBriefSection(d) {
  if (d.tier === 'free') {
    return `
      <div class="section ai-brief-section ai-brief-locked">
        <div class="section-header">
          <h3 class="section-title">AI Company Brief</h3>
          <span class="section-subtitle">Pro Feature</span>
        </div>
        <div class="ai-brief-promo">
          <div class="ai-brief-icon">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>
          </div>
          <div>
            <p style="margin-bottom:8px; font-weight:500;">Get a 3-paragraph plain-English analyst brief on ${escape(d.ticker)}.</p>
            <p class="muted" style="font-size:13px;">Grounded on the actual 10-K and recent 8-K filings. What the business does, what the numbers say, what's happening lately. Upgrade to unlock.</p>
            <a href="/subscribe" class="btn-primary" style="margin-top:14px; display:inline-flex;">Unlock AI briefs →</a>
          </div>
        </div>
      </div>
    `;
  }
  return `
    <div class="section ai-brief-section">
      <div class="section-header">
        <h3 class="section-title">AI Company Brief</h3>
        <span class="section-subtitle">Grounded on this company's filings</span>
      </div>
      <button id="aiBriefBtn" class="btn-primary ai-brief-btn">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="width:16px;height:16px;"><path d="M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96.44 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z"/><path d="M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96.44 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z"/></svg>
        <span>Generate AI brief for ${escape(d.ticker)}</span>
      </button>
      <div id="aiBriefContent" class="ai-brief-content"></div>
    </div>
  `;
}
