/* QH Trader historical market viewer. No trading or account endpoints. */
'use strict';
const $ = (id) => document.getElementById(id);
const readStore = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch { return fallback; } };
const store = (key, value) => { try { localStorage.setItem(key, JSON.stringify(value)); } catch { /* private mode */ } };
const initialFavorites = ['SHFE.RBL9', 'SHFE.AUL9', 'SHFE.CUL9', 'DCE.IL9', 'DCE.ML9', 'CZCE.MAL9'];
const savedFavorites = readStore('qh-favorites', initialFavorites);
const state = { symbols: [], symbol: readStore('qh-symbol', 'SHFE.RBL9'), interval: readStore('qh-interval', '30m'),
  favorites: new Set(Array.isArray(savedFavorites) ? savedFavorites : initialFavorites), favoriteOnly: false,
  bars: [], byTime: new Map(), request: 0, chinaColors: readStore('qh-china-colors', false), sort: 0, chart: null, series: {}, controller: null };
if (!['1d', '30m'].includes(state.interval)) state.interval = '30m';
const fmt = (value, digits = 2) => value == null || !Number.isFinite(Number(value)) ? '—' : Number(value).toLocaleString('en-US', { minimumFractionDigits: digits, maximumFractionDigits: digits });
const compact = (value) => value == null ? '—' : value >= 1e8 ? `${fmt(value / 1e8, 2)}亿` : value >= 1e4 ? `${fmt(value / 1e4, 2)}万` : fmt(value, 0);
const signed = (value, digits = 2) => `${value > 0 ? '+' : ''}${fmt(value, digits)}`;
const selected = () => state.symbols.find(s => s.id === state.symbol);
const colors = () => state.chinaColors ? { up: '#ef6a79', down: '#32bd9b' } : { up: '#32bd9b', down: '#ef6a79' };
const dateFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' });
const clockFormat = new Intl.DateTimeFormat('zh-CN', { timeZone: 'Asia/Shanghai', hour: '2-digit', minute: '2-digit', hourCycle: 'h23' });
function timeText(t, full = true) {
  if (typeof t === 'number') { const d = new Date(t * 1000); return `${dateFormat.format(d)}${full ? ' ' + clockFormat.format(d) : ''}`; }
  return typeof t === 'string' ? t : `${t.year}-${String(t.month).padStart(2, '0')}-${String(t.day).padStart(2, '0')}`;
}
let toastTimer;
function toast(text) { $('toast').textContent = text; $('toast').classList.remove('hidden'); clearTimeout(toastTimer); toastTimer = setTimeout(() => $('toast').classList.add('hidden'), 2600); }
function showError(message) { $('loading').classList.add('hidden'); $('chart-error').classList.remove('hidden'); $('error-message').textContent = message; $('status').textContent = '数据载入失败'; }
async function getJSON(url, signal) { const response = await fetch(url, { signal }); const data = await response.json(); if (!response.ok) throw new Error(data.error || `请求失败 ${response.status}`); return data; }
function makeChart() {
  if (!window.LightweightCharts) throw new Error('图表组件未载入，请检查 web/vendor 文件。');
  const L = window.LightweightCharts;
  const chart = L.createChart($('chart'), {
    autoSize: true,
    layout: { background: { type: L.ColorType.Solid, color: '#0b1019' }, textColor: '#74859d', fontSize: 10,
      fontFamily: 'Segoe UI, Microsoft YaHei, sans-serif', attributionLogo: true,
      panes: { separatorColor: '#243043', separatorHoverColor: '#40526d', enableResize: true } },
    grid: { vertLines: { color: '#17212f' }, horzLines: { color: '#17212f' } },
    crosshair: { mode: L.CrosshairMode.Normal,
      vertLine: { color: '#6e7b91', width: 1, style: 2, labelBackgroundColor: '#344258' },
      horzLine: { color: '#6e7b91', width: 1, style: 2, labelBackgroundColor: '#344258' } },
    rightPriceScale: { borderColor: '#253044', minimumWidth: 70, scaleMargins: { top: .12, bottom: .07 } },
    timeScale: { borderColor: '#253044', rightOffset: 8, barSpacing: 7, minBarSpacing: .4, timeVisible: true,
      tickMarkFormatter: (t, type) => {
        if (typeof t !== 'number') { const str = timeText(t); return type === 0 ? str.slice(0, 4) : type === 1 ? str.slice(0, 7) : str.slice(5); }
        return type >= 3 ? clockFormat.format(new Date(t * 1000)) : timeText(t, false).slice(type === 0 ? 0 : 5);
      } },
    localization: { locale: 'zh-CN', timeFormatter: t => timeText(t) },
    handleScroll: { mouseWheel: true, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
  });
  const c = colors();
  state.chart = chart;
  state.series.candle = chart.addSeries(L.CandlestickSeries, { upColor: c.up, downColor: c.down, wickUpColor: c.up,
    wickDownColor: c.down, borderVisible: false, priceLineColor: '#74829a' });
  for (const [key, color] of [['ema20', '#cdb16a'], ['ema50', '#72a1dc'], ['ema200', '#b78edd']]) {
    state.series[key] = chart.addSeries(L.LineSeries, { color, lineWidth: 1, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false });
  }
  state.series.volume = chart.addSeries(L.HistogramSeries, { priceFormat: { type: 'volume' }, priceLineVisible: false,
    lastValueVisible: false }, 1);
  state.series.volume.priceScale().applyOptions({ scaleMargins: { top: .28, bottom: .04 } });
  state.series.oi = chart.addSeries(L.AreaSeries, { lineColor: '#7898ca', topColor: '#30456955', bottomColor: '#18223a08',
    lineWidth: 1, priceLineVisible: false, lastValueVisible: false, priceFormat: { type: 'volume' } }, 2);
  state.series.oi.priceScale().applyOptions({ scaleMargins: { top: .3, bottom: .12 } });
  const panes = chart.panes(); panes[0].setStretchFactor(7); panes[1].setStretchFactor(1.6); panes[2].setStretchFactor(1.8);
  chart.subscribeCrosshairMove(param => { const bar = state.byTime.get(param.time); updateLegend(bar || state.bars.at(-1), Boolean(bar)); });
  chart.timeScale().subscribeVisibleTimeRangeChange(range => { if (range) $('visible-range').textContent = `${timeText(range.from, false)} — ${timeText(range.to, false)}`; });
  const syncLabels = () => { const p = chart.panes(); if (p.length < 3) return; $('volume-label').style.top = `${p[0].getHeight() + 9}px`; $('oi-label').style.top = `${p[0].getHeight() + p[1].getHeight() + 10}px`; };
  new ResizeObserver(() => requestAnimationFrame(syncLabels)).observe($('chart'));
  $('chart').addEventListener('pointerup', () => setTimeout(syncLabels, 30));
  state.drawings = new QHDrawingTools({ chart, series: state.series.candle, element: $('chart'), toolbar: $('drawing-toolbar'), hint: $('drawing-hint'), notify: toast });
}
function updateLegend(bar, hover = false) {
  if (!bar) return;
  const precision = selected()?.precision ?? 2;
  const ohlc = $('ohlc'); ohlc.replaceChildren();
  const label = document.createElement('span'); label.className = 'hover-date';
  label.textContent = state.interval === '1d' ? bar.tradingDay : `${bar.naturalTime.slice(0, 10)} ${bar.naturalTime.slice(11, 16)} · ${bar.session}`;
  label.title = `交易日 ${bar.tradingDay} · 来源标签 ${bar.sourceLabel}`; ohlc.append(label);
  for (const [name, key] of [['开', 'open'], ['高', 'high'], ['低', 'low'], ['收', 'close']]) {
    const cell = document.createElement('span'); cell.textContent = name; const number = document.createElement('b'); number.textContent = fmt(bar[key], precision); cell.append(number); ohlc.append(cell);
  }
  for (const key of ['ema20', 'ema50', 'ema200']) $(key + '-value').textContent = fmt(bar[key], precision);
  $('volume-value').textContent = compact(bar.volume); $('oi-value').textContent = compact(bar.openInterest);
  if (hover) $('status').textContent = `交易日 ${bar.tradingDay} · ${state.interval === '30m' ? bar.session : '日线'} · 通达信加权`;
  else $('status').textContent = '历史快照已载入 · 通达信';
}
function updateQuote() {
  const s = selected(); if (!s) return;
  $('instrument-name').textContent = s.name; $('toolbar-code').textContent = s.code; $('exchange-badge').textContent = s.exchange;
  $('last-price').textContent = fmt(s.last, s.precision); $('price-change').textContent = `${signed(s.change, s.precision)} (${signed(s.changePct)}%)`;
  $('last-price').className = s.change >= 0 ? 'up' : 'down'; $('price-change').className = s.change >= 0 ? 'up' : 'down';
  $('last-date').textContent = s.date; $('snapshot-date').textContent = s.date;
  $('favorite-current').classList.toggle('starred', state.favorites.has(s.id));
  $('favorite-current').setAttribute('aria-label', state.favorites.has(s.id) ? '移出自选' : '加入自选');
  document.title = `${s.name} ${s.code} · QH Trader`;
  document.querySelectorAll('[data-interval]').forEach(button => { button.classList.toggle('active', button.dataset.interval === state.interval); button.setAttribute('aria-pressed', button.dataset.interval === state.interval); });
}
function renderList() {
  const query = $('search').value.trim().toLowerCase(), exchange = $('exchange-filter').value;
  let list = state.symbols.filter(s => (!query || `${s.name} ${s.code} ${s.exchange}`.toLowerCase().includes(query)) &&
    (!exchange || s.exchange === exchange) && (!state.favoriteOnly || state.favorites.has(s.id)));
  if (state.sort) list.sort((a, b) => (b.changePct - a.changePct) * state.sort);
  else list.sort((a, b) => Number(state.favorites.has(b.id)) - Number(state.favorites.has(a.id)));
  $('filtered-count').textContent = `${list.length} 个序列`; $('favorites-count').textContent = state.favorites.size;
  $('symbol-count').textContent = state.symbols.length; $('symbol-list').replaceChildren();
  if (!list.length) { const empty = document.createElement('div'); empty.className = 'no-results'; empty.textContent = state.favoriteOnly ? '暂无匹配的自选品种\n点击图表标题旁的星标添加' : '没有找到匹配的品种'; $('symbol-list').append(empty); return; }
  const fragment = document.createDocumentFragment();
  for (const s of list) {
    const row = document.createElement('div'); row.className = 'symbol-row' + (s.id === state.symbol ? ' selected' : '');
    row.tabIndex = 0; row.setAttribute('role', 'button'); row.setAttribute('aria-label', `${s.name} ${s.code}`); row.setAttribute('aria-pressed', s.id === state.symbol);
    const identity = document.createElement('div'), name = document.createElement('div'), code = document.createElement('div');
    name.className = 'symbol-name'; name.textContent = s.name; code.className = 'symbol-code'; code.textContent = `${s.code} · ${s.exchange}${state.favorites.has(s.id) ? '  ★' : ''}`; identity.append(name, code);
    const price = document.createElement('span'); price.className = 'symbol-price'; price.textContent = fmt(s.last, s.precision);
    const change = document.createElement('span'); change.className = 'symbol-change ' + (s.changePct >= 0 ? 'up' : 'down'); change.textContent = `${signed(s.changePct)}%`;
    row.append(identity, price, change); const activate = () => { if (state.symbol !== s.id) { state.symbol = s.id; store('qh-symbol', s.id); loadBars(); } document.querySelector('.watchlist').classList.remove('mobile-open'); };
    row.addEventListener('click', activate); row.addEventListener('keydown', e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); activate(); } }); fragment.append(row);
  }
  $('symbol-list').append(fragment);
}
function colorData() {
  const c = colors(); document.documentElement.style.setProperty('--up', c.up); document.documentElement.style.setProperty('--down', c.down);
  $('color-mode').textContent = state.chinaColors ? '红涨绿跌' : '绿涨红跌';
  if (!state.chart) return;
  state.series.candle.applyOptions({ upColor: c.up, downColor: c.down, wickUpColor: c.up, wickDownColor: c.down });
  state.series.volume.setData(state.bars.map(b => ({ time: b.time, value: b.volume, color: (b.close >= b.open ? c.up : c.down) + '65' })));
}
async function loadBars() {
  state.drawings?.suspend();
  const request = ++state.request; state.controller?.abort(); state.controller = new AbortController();
  $('loading').classList.remove('hidden'); $('chart-error').classList.add('hidden'); $('date-error').textContent = '';
  updateQuote(); renderList(); $('screenshot').disabled = true;
  try {
    const data = await getJSON(`/api/bars?symbol=${encodeURIComponent(state.symbol)}&interval=${state.interval}`, state.controller.signal);
    if (request !== state.request) return;
    if (!data.bars.length) throw new Error('这个周期没有可显示的行情。');
    state.bars = data.bars; state.byTime = new Map(data.bars.map(b => [b.time, b]));
    // Remove the old time scale before changing between daily dates and intraday UNIX timestamps.
    for (const series of Object.values(state.series)) series.setData([]);
    const precision = selected()?.precision ?? 2;
    state.series.candle.applyOptions({ priceFormat: { type: 'price', precision, minMove: 10 ** -precision } });
    state.series.candle.setData(data.bars.map(({ time, open, high, low, close }) => ({ time, open, high, low, close })));
    for (const key of ['ema20', 'ema50', 'ema200']) state.series[key].setData(data.bars.filter(b => b[key] != null).map(b => ({ time: b.time, value: b[key] })));
    state.series.oi.setData(data.bars.map(b => ({ time: b.time, value: b.openInterest }))); colorData();
    state.chart.applyOptions({ timeScale: { timeVisible: state.interval === '30m' } });
    $('start-date').value = data.bars[0].tradingDay; $('end-date').value = data.bars.at(-1).tradingDay;
    for (const id of ['start-date', 'end-date']) { $(id).min = data.bars[0].tradingDay; $(id).max = data.bars.at(-1).tradingDay; }
    $('archive-coverage').textContent = `${data.bars[0].tradingDay.replaceAll('-', '.')} — ${data.bars.at(-1).tradingDay.replaceAll('-', '.')}`;
    $('bar-count').textContent = `${fmt(data.bars.length, 0)} 根 · ${state.interval === '30m' ? '30分钟' : '日线'}`;
    $('time-policy').textContent = data.timePolicy;
    $('current-quality').textContent = `来源 ${fmt(data.sourceCount, 0)} 根；显示 ${fmt(data.bars.length, 0)} 根。排除异常 ${data.excludedInvalid} 根、无法确定前序夜盘日期 ${data.excludedUnknownTime} 根。未补造缺失行情。`;
    if (data.missingTradingDays) {
      $('bar-count').textContent += ` · ${data.missingTradingDays} 个日线日期无分钟数据`;
      $('current-quality').textContent += `另有 ${data.missingTradingDays} 个日线日期没有分钟数据，缺口日期包络为 ${data.missingDayRange.join(' 至 ')}。`;
    }
    state.drawings.setContext(state.symbol, state.interval, data.bars, precision);
    resetView(); updateLegend(data.bars.at(-1)); $('loading').classList.add('hidden'); $('screenshot').disabled = false;
  } catch (error) { if (error.name !== 'AbortError' && request === state.request) showError(error.message); }
}
function setRange(from, to) { state.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, from), to }); }
function resetView() { if (!state.bars.length) return; $('date-error').textContent = ''; state.chart.priceScale('right').applyOptions({ autoScale: true }); setRange(state.bars.length - 150, state.bars.length + 5); document.querySelectorAll('[data-range]').forEach(b => b.classList.remove('active')); }
function bindControls() {
  const attribution = document.createElement('p'); attribution.className = 'attribution-notice'; attribution.append($('chart-attribution').content.cloneNode(true)); $('info-dialog').insertBefore(attribution, $('confirm-info'));
  document.querySelectorAll('[data-interval]').forEach(button => button.addEventListener('click', () => { if (state.interval === button.dataset.interval) return; state.interval = button.dataset.interval; store('qh-interval', state.interval); loadBars(); }));
  document.querySelectorAll('[data-indicator]').forEach(button => button.addEventListener('click', () => { const visible = !button.classList.contains('on'); button.classList.toggle('on', visible); button.setAttribute('aria-pressed', visible); state.series[button.dataset.indicator].applyOptions({ visible }); }));
  $('search').addEventListener('input', renderList); $('exchange-filter').addEventListener('change', renderList);
  const toggleTab = (favorites) => { state.favoriteOnly = favorites; $('all-tab').classList.toggle('active', !favorites); $('favorites-tab').classList.toggle('active', favorites); renderList(); };
  $('all-tab').onclick = () => toggleTab(false); $('favorites-tab').onclick = () => toggleTab(true);
  $('favorite-current').onclick = () => { const exists = state.favorites.has(state.symbol); exists ? state.favorites.delete(state.symbol) : state.favorites.add(state.symbol); store('qh-favorites', [...state.favorites]); updateQuote(); renderList(); toast(exists ? '已移出自选' : '已加入自选'); };
  $('sort-change').onclick = () => { state.sort = state.sort === 1 ? -1 : 1; $('sort-change').textContent = '涨跌幅 ' + (state.sort === 1 ? '↓' : '↑'); renderList(); };
  $('symbol-trigger').onclick = () => { document.querySelector('.watchlist').classList.toggle('mobile-open'); $('search').focus(); $('search').select(); };
  document.addEventListener('keydown', e => { if (e.key === '/' && !['INPUT', 'SELECT', 'TEXTAREA'].includes(document.activeElement.tagName)) { e.preventDefault(); $('symbol-trigger').click(); } });
  $('color-mode').onclick = () => { state.chinaColors = !state.chinaColors; store('qh-china-colors', state.chinaColors); colorData(); };
  $('reset-chart').onclick = resetView;
  document.querySelectorAll('[data-range]').forEach(button => button.addEventListener('click', () => {
    if (!state.bars.length) return;
    if (button.dataset.range === 'all') state.chart.timeScale().fitContent();
    else { const cut = new Date(state.bars.at(-1).tradingDay + 'T00:00:00Z'); cut.setUTCMonth(cut.getUTCMonth() - Number(button.dataset.range)); const start = cut.toISOString().slice(0, 10); const index = state.bars.findIndex(b => b.tradingDay >= start); setRange(index < 0 ? 0 : index, state.bars.length + 3); }
    document.querySelectorAll('[data-range]').forEach(b => b.classList.toggle('active', b === button));
  }));
  $('apply-dates').onclick = () => {
    const from = $('start-date').value, to = $('end-date').value;
    if (!from || !to || from > to) { $('date-error').textContent = '请输入有效的起止日期'; return; }
    const indices = state.bars.flatMap((b, i) => b.tradingDay >= from && b.tradingDay <= to ? [i] : []);
    if (!indices.length) { $('date-error').textContent = '该范围没有行情'; return; }
    $('date-error').textContent = ''; setRange(indices[0] - .5, indices.at(-1) + 1.5);
    document.querySelectorAll('[data-range]').forEach(b => b.classList.remove('active'));
  };
  $('fullscreen').onclick = async () => { try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.querySelector('.market').requestFullscreen(); } catch { toast('当前浏览器不支持全屏'); } };
  $('screenshot').onclick = () => { if (!state.chart || !state.bars.length) return; const link = document.createElement('a'); link.download = `${state.symbol}_${state.interval}_${state.bars.at(-1).tradingDay}.png`; link.href = state.chart.takeScreenshot(true, false).toDataURL('image/png'); link.click(); toast('图表已导出'); };
  for (const id of ['data-info', 'toggle-details']) $(id).onclick = () => $('info-dialog').showModal();
  for (const id of ['close-info', 'confirm-info']) $(id).onclick = () => $('info-dialog').close();
  $('retry').onclick = loadBars;
}
(async function init() {
  try { makeChart(); bindControls(); colorData(); const data = await getJSON('/api/symbols'); state.symbols = data.symbols;
    if (!state.symbols.some(s => s.id === state.symbol)) state.symbol = state.symbols[0]?.id;
    if (!state.symbol) throw new Error('归档中没有可显示的品种。'); await loadBars();
  } catch (error) { showError(error.message); }
})();
