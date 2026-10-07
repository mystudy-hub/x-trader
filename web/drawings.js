/* Chart primitives and pointer tools. Anchors are time + bar fraction + price, never screen pixels. */
(function (root) {
  'use strict';
  const clone = value => JSON.parse(JSON.stringify(value));
  const finite = Number.isFinite;
  const clamp = (v, low, high) => Math.min(high, Math.max(low, v));
  const names = { select: '选择', line: '趋势线', ray: '射线', channel: '平行通道', measure: '度量' };

  // Clip p + t(q-p) to the pane. A ray starts at t=0; a channel spans both directions.
  function clipLine(p, q, width, height, kind = 'line') {
    const dx = q.x - p.x, dy = q.y - p.y;
    if (Math.hypot(dx, dy) < 1e-8) return null;
    let lo = kind === 'infinite' ? -Infinity : 0, hi = kind === 'line' ? 1 : Infinity;
    for (const [origin, delta, limit] of [[p.x, dx, width], [p.y, dy, height]]) {
      if (Math.abs(delta) < 1e-8) { if (origin < 0 || origin > limit) return null; }
      else { const a = -origin / delta, b = (limit - origin) / delta; lo = Math.max(lo, Math.min(a, b)); hi = Math.min(hi, Math.max(a, b)); }
    }
    return lo <= hi ? [{ x: p.x + lo * dx, y: p.y + lo * dy }, { x: p.x + hi * dx, y: p.y + hi * dy }] : null;
  }
  function distanceToSegment(p, a, b) {
    const dx = b.x - a.x, dy = b.y - a.y, length = dx * dx + dy * dy;
    const t = length ? clamp(((p.x - a.x) * dx + (p.y - a.y) * dy) / length, 0, 1) : 0;
    return Math.hypot(p.x - a.x - t * dx, p.y - a.y - t * dy);
  }
  function channelShift(a, b, c) {
    if (Math.abs(b.logical - a.logical) < 1e-8) return null;
    return c.price - (a.price + (b.price - a.price) * (c.logical - a.logical) / (b.logical - a.logical));
  }
  function validDrawing(item) {
    return item && typeof item.id === 'string' && ['line', 'ray', 'channel', 'measure'].includes(item.type)
      && /^#[0-9a-f]{6}$/i.test(item.color) && Array.isArray(item.points)
      && item.points.length === (item.type === 'channel' ? 3 : 2)
      && item.points.every(p => p && (finite(p.time) || /^\d{4}-\d{2}-\d{2}$/.test(p.time))
        && finite(p.offset) && Math.abs(p.offset) < 1e6 && finite(p.price));
  }

  class DrawingTools {
    constructor({ chart, series, element, toolbar, hint, notify }) {
      Object.assign(this, { chart, series, element, toolbar, hint, notify });
      this.items = []; this.bars = []; this.index = new Map(); this.mode = 'select'; this.color = '#83acff';
      this.selected = null; this.draft = null; this.hover = null; this.drag = null; this.undoStack = []; this.redoStack = [];
      this.active = false; this.hidden = false; this.key = ''; this.precision = 2; this.labels = new Map();
      this.navigation = { handleScroll: clone(chart.options().handleScroll), handleScale: clone(chart.options().handleScale) };
      const renderer = { draw: target => target.useMediaCoordinateSpace(scope => this.paint(scope.context, scope.mediaSize.width, scope.mediaSize.height)) };
      const views = [{ zOrder: () => 'top', renderer: () => renderer }];
      this.primitive = {
        attached: ({ requestUpdate }) => { this.redraw = requestUpdate; },
        detached: () => { this.redraw = () => {}; },
        paneViews: () => views,
        hitTest: (x, y) => { const hit = this.hit({ x, y }); return hit ? { externalId: hit.id, cursorStyle: hit.handle >= 0 ? 'crosshair' : 'move', zOrder: 'top' } : null; },
      };
      series.attachPrimitive(this.primitive);
      toolbar.querySelectorAll('[data-draw-tool]').forEach(button => button.addEventListener('click', () => this.setMode(button.dataset.drawTool)));
      toolbar.querySelector('[data-draw-action="undo"]').onclick = () => this.undo();
      toolbar.querySelector('[data-draw-action="redo"]').onclick = () => this.redo();
      toolbar.querySelector('[data-draw-action="delete"]').onclick = () => this.remove();
      toolbar.querySelector('[data-draw-action="hide"]').onclick = () => { this.cancel(); this.hidden = !this.hidden; this.refresh(); };
      toolbar.querySelector('input[type=color]').onchange = event => {
        this.color = event.target.value;
        if (this.selected) this.commit(this.items.map(item => item.id === this.selected ? { ...item, color: this.color } : item));
      };
      element.addEventListener('pointerdown', event => this.pointerDown(event), true);
      element.addEventListener('pointermove', event => this.pointerMove(event), true);
      element.addEventListener('pointerup', event => this.pointerUp(event), true);
      element.addEventListener('pointercancel', () => this.cancel(), true);
      element.addEventListener('contextmenu', event => { if (this.mode !== 'select' || this.drag) { event.preventDefault(); this.cancel(); } });
      document.addEventListener('keydown', event => this.keyDown(event));
      this.refresh();
    }
    setContext(symbol, interval, bars, precision) {
      this.cancel(); this.key = `qh-drawings:v1:${symbol}:${interval}`; this.interval = interval;
      this.bars = bars; this.index = new Map(bars.map((b, i) => [b.time, i])); this.precision = precision;
      this.items = []; this.undoStack = []; this.redoStack = []; this.selected = null; this.hidden = false;
      try {
        const saved = JSON.parse(localStorage.getItem(this.key) || 'null');
        if (saved != null) {
          if (saved.version !== 1 || !Array.isArray(saved.items) || saved.items.length > 200 || !saved.items.every(validDrawing)) throw new Error('invalid drawings');
          this.items = saved.items;
        }
      } catch { this.notify('此品种的绘图记录无法读取；原记录未自动覆盖'); }
      this.active = true; this.refresh();
    }
    suspend() { this.cancel(); this.active = false; this.items = []; this.selected = null; this.refresh(); }
    save() {
      try { localStorage.setItem(this.key, JSON.stringify({ version: 1, items: this.items })); }
      catch { this.notify('浏览器无法保存绘图，请勿关闭当前页面'); }
    }
    remember(before) { this.undoStack.push(clone(before)); if (this.undoStack.length > 60) this.undoStack.shift(); this.redoStack = []; }
    commit(items) { this.remember(this.items); this.items = items; this.save(); this.refresh(); }
    undo() { this.cancel(); if (!this.undoStack.length) return; this.redoStack.push(clone(this.items)); this.items = this.undoStack.pop(); this.selected = null; this.save(); this.refresh(); }
    redo() { this.cancel(); if (!this.redoStack.length) return; this.undoStack.push(clone(this.items)); this.items = this.redoStack.pop(); this.selected = null; this.save(); this.refresh(); }
    remove() { if (!this.selected) return; const next = this.items.filter(d => d.id !== this.selected); this.selected = null; this.commit(next); }
    lockNavigation(lock) {
      this.chart.applyOptions(lock ? { handleScroll: false, handleScale: false } : this.navigation);
      this.element.style.cursor = lock ? 'crosshair' : '';
    }
    cancel() {
      if (this.drag) this.items = this.drag.before;
      this.drag = null; this.draft = null; this.hover = null; this.mode = 'select'; this.lockNavigation(false); this.refresh();
    }
    setMode(mode) {
      if (!this.active) return;
      this.cancel(); this.mode = mode; this.selected = null; this.hidden = false; this.lockNavigation(mode !== 'select'); this.refresh();
    }
    refresh() {
      this.toolbar.querySelectorAll('[data-draw-tool]').forEach(button => {
        const active = button.dataset.drawTool === this.mode; button.classList.toggle('active', active); button.setAttribute('aria-pressed', active); button.disabled = !this.active;
      });
      this.toolbar.querySelector('[data-draw-action="undo"]').disabled = !this.active || !this.undoStack.length;
      this.toolbar.querySelector('[data-draw-action="redo"]').disabled = !this.active || !this.redoStack.length;
      this.toolbar.querySelector('[data-draw-action="delete"]').disabled = !this.active || !this.selected;
      const hide = this.toolbar.querySelector('[data-draw-action="hide"]'); hide.classList.toggle('active', this.hidden); hide.setAttribute('aria-pressed', this.hidden);
      const n = this.draft?.points.length || 0;
      let message = '';
      if (this.mode !== 'select') {
        message = this.mode === 'channel' ? ['平行通道：点击基线起点', '点击基线终点', '点击第三点，确定通道宽度'][n] : `${names[this.mode]}：点击${n ? '终点' : '起点'}`;
        message += ' · Esc 取消';
      } else if (this.selected) message = `${names[this.items.find(d => d.id === this.selected)?.type] || '绘图'} · 拖动端点调整，拖动线条平移 · Del 删除`;
      this.hint.textContent = message; this.hint.classList.toggle('hidden', !message || !this.active);
      this.redraw?.();
    }
    limits() { return { width: this.chart.timeScale().width(), height: this.chart.panes()[0].getHeight() }; }
    eventPoint(event) { const rect = this.element.getBoundingClientRect(); return { x: event.clientX - rect.left, y: event.clientY - rect.top }; }
    inside(p) { const s = this.limits(); return p.x >= 0 && p.x <= s.width && p.y >= 0 && p.y <= s.height; }
    logical(anchor) { const index = this.index.get(anchor.time); return index == null ? null : index + anchor.offset; }
    anchor(logical, price) {
      if (!finite(logical) || !finite(price) || !this.bars.length) return null;
      const index = clamp(Math.round(logical), 0, this.bars.length - 1);
      return { time: this.bars[index].time, offset: logical - index, price };
    }
    fromPixel(p) { return this.anchor(this.chart.timeScale().coordinateToLogical(p.x), this.series.coordinateToPrice(p.y)); }
    toPixel(p) {
      const logical = this.logical(p); if (logical == null) return null;
      const x = this.chart.timeScale().logicalToCoordinate(logical), y = this.series.priceToCoordinate(p.price);
      return finite(x) && finite(y) ? { x, y, logical, price: p.price } : null;
    }
    geometry(item, width, height) {
      const points = item.points.map(p => this.toPixel(p)); if (points.some(p => p == null) || points.length < 2) return { points: [], segments: [] };
      const [a, b, c] = points, segments = []; let polygon = null;
      const add = (p, q, type) => { const segment = clipLine(p, q, width, height, type); if (segment) segments.push(segment); };
      if (item.type === 'measure') {
        const u = { x: b.x, y: a.y }, v = { x: a.x, y: b.y };
        for (const [p, q] of [[a, u], [u, b], [b, v], [v, a]]) add(p, q, 'line');
        polygon = [a, u, b, v];
      } else if (item.type === 'channel' && c) {
        const shift = channelShift(a, b, c); if (shift == null) return { points, segments };
        const parallelA = { x: a.x, y: this.series.priceToCoordinate(a.price + shift) };
        const parallelB = { x: b.x, y: this.series.priceToCoordinate(b.price + shift) };
        add(a, b, 'infinite'); add(parallelA, parallelB, 'infinite');
        const slope = (b.y - a.y) / (b.x - a.x), offset = parallelA.y - a.y;
        polygon = [{ x: 0, y: a.y - slope * a.x }, { x: width, y: a.y + slope * (width - a.x) },
          { x: width, y: a.y + slope * (width - a.x) + offset }, { x: 0, y: a.y - slope * a.x + offset }];
      } else add(a, b, item.type === 'ray' ? 'ray' : 'line');
      return { points, segments, polygon };
    }
    hit(p) {
      if (!this.active || this.hidden || this.mode !== 'select' || !this.inside(p)) return null;
      const { width, height } = this.limits();
      const ordered = [...this.items].reverse().sort((a, b) => Number(b.id === this.selected) - Number(a.id === this.selected));
      let best = null, nearest = 6;
      for (const item of ordered) {
        const shape = this.geometry(item, width, height);
        if (item.id === this.selected) {
          const handle = shape.points.findIndex(q => Math.hypot(p.x - q.x, p.y - q.y) < 9);
          if (handle >= 0) return { id: item.id, handle };
        }
        const distance = Math.min(...shape.segments.map(([a, b]) => distanceToSegment(p, a, b)));
        if (distance < nearest) { nearest = distance; best = { id: item.id, handle: -1 }; }
        const label = this.labels.get(item.id);
        if (label && p.x >= label.x && p.x <= label.x + label.w && p.y >= label.y && p.y <= label.y + label.h) return { id: item.id, handle: -1 };
      }
      return best;
    }
    pointerDown(event) {
      if (!this.active || event.button !== 0) return;
      const p = this.eventPoint(event); if (!this.inside(p)) return;
      if (this.mode !== 'select') {
        event.preventDefault(); event.stopImmediatePropagation();
        const anchor = this.fromPixel(p); if (!anchor) return;
        if (!this.draft) this.draft = { id: crypto.randomUUID(), type: this.mode, color: this.color, points: [] };
        const first = this.draft.points[0];
        if (this.draft.points.length === 1) {
          const a = this.toPixel(first);
          if (Math.hypot(a.x - p.x, a.y - p.y) < 5) return;
          if (this.mode === 'channel' && Math.abs(this.logical(anchor) - this.logical(first)) < .25) { this.notify('通道基线需沿时间方向展开'); return; }
        }
        this.draft.points.push(anchor); this.hover = anchor;
        if (this.draft.points.length === (this.mode === 'channel' ? 3 : 2)) {
          if (this.items.length >= 200) { this.notify('当前图表已保存200个绘图，请先删除部分绘图'); this.cancel(); return; }
          const finished = this.draft; this.draft = null; this.mode = 'select'; this.selected = finished.id; this.lockNavigation(false); this.commit([...this.items, finished]);
        } else this.refresh();
      } else {
        const hit = this.hit(p);
        if (!hit) { this.selected = null; this.refresh(); return; }
        event.preventDefault(); event.stopImmediatePropagation();
        this.selected = hit.id; const item = this.items.find(d => d.id === hit.id);
        this.color = item.color; this.toolbar.querySelector('input[type=color]').value = item.color;
        this.drag = { ...hit, start: this.fromPixel(p), pixel: p, original: clone(item), before: clone(this.items), moved: false, pointerId: event.pointerId };
        this.lockNavigation(true); this.element.setPointerCapture(event.pointerId); this.refresh();
      }
    }
    pointerMove(event) {
      if (!this.active) return;
      const p = this.eventPoint(event);
      if (this.drag) {
        event.preventDefault(); event.stopImmediatePropagation();
        if (Math.hypot(p.x - this.drag.pixel.x, p.y - this.drag.pixel.y) < 3 && !this.drag.moved) return;
        const current = this.fromPixel(p); if (!current) return;
        this.drag.moved = true;
        const edited = clone(this.drag.original);
        if (this.drag.handle >= 0) edited.points[this.drag.handle] = current;
        else {
          const dx = this.logical(current) - this.logical(this.drag.start), dy = current.price - this.drag.start.price;
          edited.points = edited.points.map(a => this.anchor(this.logical(a) + dx, a.price + dy));
        }
        if (edited.type === 'channel' && Math.abs(this.logical(edited.points[1]) - this.logical(edited.points[0])) < .25) return;
        this.items = this.items.map(d => d.id === edited.id ? edited : d); this.redraw?.();
      } else if (this.mode !== 'select' && this.inside(p)) { this.hover = this.fromPixel(p); this.redraw?.(); }
    }
    pointerUp(event) {
      if (!this.drag || event.pointerId !== this.drag.pointerId) return;
      event.preventDefault(); event.stopImmediatePropagation();
      if (this.drag.moved) { this.remember(this.drag.before); this.save(); }
      this.drag = null; this.lockNavigation(false); this.refresh();
    }
    keyDown(event) {
      if (!this.active || event.target.isContentEditable || /^(INPUT|SELECT|TEXTAREA)$/.test(event.target.tagName) || document.querySelector('dialog[open]')) return;
      const key = event.key.toLowerCase();
      if (key === 'escape') { this.cancel(); this.selected = null; this.refresh(); return; }
      if (event.ctrlKey || event.metaKey) {
        if (key === 'z') { event.preventDefault(); event.shiftKey ? this.redo() : this.undo(); }
        if (key === 'y') { event.preventDefault(); this.redo(); }
        return;
      }
      if ((key === 'delete' || key === 'backspace') && this.selected) { event.preventDefault(); this.remove(); }
      const shortcuts = { v: 'select', t: 'line', r: 'ray', p: 'channel', m: 'measure' };
      if (shortcuts[key] && !event.altKey) { event.preventDefault(); this.setMode(shortcuts[key]); }
    }
    timeValue(anchor) {
      const logical = this.logical(anchor), last = this.bars.length - 1;
      const stamp = i => { const t = this.bars[i].time; return typeof t === 'number' ? t : Date.parse(t + 'T00:00:00Z') / 1000; };
      if (logical < 0 || logical > last) return { value: stamp(logical < 0 ? 0 : last) + (logical - (logical < 0 ? 0 : last)) * (this.interval === '1d' ? 86400 : 1800), estimated: true };
      const lo = Math.floor(logical), hi = Math.min(last, lo + 1);
      return { value: stamp(lo) + (stamp(hi) - stamp(lo)) * (logical - lo), estimated: false };
    }
    measureText(item) {
      const [a, b] = item.points, delta = b.price - a.price;
      const number = n => (n > 0 ? '+' : '') + n.toFixed(this.precision);
      const percentage = a.price ? `${delta >= 0 ? '+' : ''}${(delta / a.price * 100).toFixed(2)}%` : '—';
      const from = this.timeValue(a), to = this.timeValue(b), minutes = Math.round(Math.abs(to.value - from.value) / 60);
      const days = Math.floor(minutes / 1440), hours = Math.floor(minutes % 1440 / 60), rest = minutes % 60;
      const duration = [days && `${days}天`, hours && `${hours}小时`, (rest || !minutes) && `${rest}分`].filter(Boolean).join('');
      return [`Δ ${number(delta)}  (${percentage})`, `${Math.abs(Math.round(this.logical(b) - this.logical(a)))} 根K线间隔 · ${duration}${from.estimated || to.estimated ? '（估算）' : ''}`];
    }
    paint(ctx, width, height) {
      this.labels.clear(); if (!this.active || this.hidden || !this.bars.length) return;
      ctx.save(); ctx.beginPath(); ctx.rect(0, 0, width, height); ctx.clip();
      const entries = [...this.items];
      if (this.draft) { const preview = clone(this.draft); if (this.hover && preview.points.length < (preview.type === 'channel' ? 3 : 2)) preview.points.push(this.hover); entries.push(preview); }
      for (const item of entries) {
        const selected = item.id === this.selected, preview = item.id === this.draft?.id;
        const shape = this.geometry(item, width, height), color = item.color;
        ctx.strokeStyle = color; ctx.lineWidth = selected ? 2.2 : 1.6; ctx.setLineDash(preview || item.type === 'measure' ? [5, 4] : []);
        if (shape.polygon) { ctx.fillStyle = color + '16'; ctx.beginPath(); shape.polygon.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)); ctx.closePath(); ctx.fill(); }
        for (const [a, b] of shape.segments) { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); }
        if (item.type === 'ray' && shape.segments.length) {
          const [a, b] = shape.segments[0], angle = Math.atan2(b.y - a.y, b.x - a.x);
          ctx.beginPath(); ctx.moveTo(b.x - 9 * Math.cos(angle - .4), b.y - 9 * Math.sin(angle - .4)); ctx.lineTo(b.x, b.y); ctx.lineTo(b.x - 9 * Math.cos(angle + .4), b.y - 9 * Math.sin(angle + .4)); ctx.stroke();
        }
        if (item.type === 'measure' && shape.points.length >= 2 && shape.segments.length) {
          const text = this.measureText(item), [a, b] = shape.points;
          ctx.font = '11px Segoe UI, Microsoft YaHei, sans-serif';
          const w = Math.min(width, Math.max(...text.map(t => ctx.measureText(t).width)) + 20), h = 47;
          const x = clamp((a.x + b.x - w) / 2, 0, Math.max(0, width - w));
          const y = clamp(Math.min(a.y, b.y) > h + 10 ? Math.min(a.y, b.y) - h - 8 : Math.max(a.y, b.y) + 8, 0, Math.max(0, height - h));
          ctx.setLineDash([]); ctx.fillStyle = '#172334f5'; ctx.strokeStyle = color; ctx.lineWidth = 1;
          ctx.beginPath(); ctx.roundRect(x, y, w, h, 4); ctx.fill(); ctx.stroke();
          ctx.fillStyle = color; ctx.fillText(text[0], x + 10, y + 18); ctx.fillStyle = '#b7c5d9'; ctx.fillText(text[1], x + 10, y + 35);
          this.labels.set(item.id, { x, y, w, h });
        }
        if (selected || preview) {
          ctx.setLineDash([]); ctx.lineWidth = 1.5;
          for (const anchor of item.points) { const p = this.toPixel(anchor); if (!p) continue; ctx.beginPath(); ctx.arc(p.x, p.y, 4.5, 0, Math.PI * 2); ctx.fillStyle = '#0b1019'; ctx.strokeStyle = color; ctx.fill(); ctx.stroke(); }
        }
      }
      ctx.restore();
    }
  }
  root.QHDrawingTools = DrawingTools;
  if (typeof module !== 'undefined') module.exports = { clipLine, distanceToSegment, channelShift, validDrawing };
})(globalThis);
