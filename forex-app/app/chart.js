// Chart: candles, the forecast band (p10-p90 shaded, dashed median), the session
// range box, and the entry / SL / TP lines. Built on TradingView Lightweight Charts 4.2.
const LW = window.LightweightCharts;

export function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function rgba(hex, a) {
  const h = hex.replace("#", "");
  const n = parseInt(h.length === 3 ? h.split("").map((c) => c + c).join("") : h, 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

// ---------------------------------------------------------------- primitives
class BandPrimitive {
  constructor() { this.points = []; this.color = "#6250d6"; }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = null; }
  set(points, color) { this.points = points || []; if (color) this.color = color; this.requestUpdate && this.requestUpdate(); }
  updateAllViews() {}
  paneViews() {
    const self = this;
    return [{
      zOrder: () => "bottom",
      renderer: () => ({
        draw(target) {
          if (!self.chart || self.points.length < 2) return;
          const ts = self.chart.timeScale();
          const top = [], bot = [];
          for (const p of self.points) {
            const x = ts.timeToCoordinate(p.t);
            const y1 = self.series.priceToCoordinate(p.p90);
            const y2 = self.series.priceToCoordinate(p.p10);
            if (x === null || y1 === null || y2 === null) continue;
            top.push([x, y1]); bot.push([x, y2]);
          }
          if (top.length < 2) return;
          target.useMediaCoordinateSpace(({ context: ctx }) => {
            ctx.save();
            ctx.beginPath();
            top.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
            for (let i = bot.length - 1; i >= 0; i--) ctx.lineTo(bot[i][0], bot[i][1]);
            ctx.closePath();
            ctx.fillStyle = rgba(self.color, 0.16);
            ctx.fill();
            ctx.strokeStyle = rgba(self.color, 0.45);
            ctx.lineWidth = 1;
            ctx.beginPath();
            top.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
            ctx.stroke();
            ctx.beginPath();
            bot.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
            ctx.stroke();
            ctx.restore();
          });
        },
      }),
    }];
  }
}

class RangeBoxPrimitive {
  constructor() { this.boxes = []; this.color = "#77766f"; this.text = "#52514e"; }
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.requestUpdate = requestUpdate; }
  detached() { this.chart = null; }
  set(boxes, color, text) { this.boxes = boxes || []; if (color) this.color = color; if (text) this.text = text; this.requestUpdate && this.requestUpdate(); }
  updateAllViews() {}
  paneViews() {
    const self = this;
    return [{
      zOrder: () => "bottom",
      renderer: () => ({
        draw(target) {
          if (!self.chart || !self.boxes.length) return;
          const ts = self.chart.timeScale();
          target.useMediaCoordinateSpace(({ context: ctx, mediaSize }) => {
            for (const b of self.boxes) {
              let x1 = ts.timeToCoordinate(b.start);
              let x2 = ts.timeToCoordinate(b.end);
              const y1 = self.series.priceToCoordinate(b.high);
              const y2 = self.series.priceToCoordinate(b.low);
              if (y1 === null || y2 === null) continue;
              if (x1 === null) continue;
              if (x2 === null) x2 = b.endFallback ?? mediaSize.width;
              x2 = Math.min(x2, mediaSize.width);
              ctx.save();
              ctx.fillStyle = rgba(self.color, 0.10);
              ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
              ctx.setLineDash([4, 3]);
              ctx.strokeStyle = rgba(self.color, 0.8);
              ctx.lineWidth = 1;
              ctx.strokeRect(x1 + 0.5, y1 + 0.5, x2 - x1 - 1, y2 - y1 - 1);
              ctx.setLineDash([]);
              ctx.fillStyle = self.text;
              ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
              ctx.fillText(b.label, x1 + 4, y1 - 4);
              ctx.restore();
            }
          });
        },
      }),
    }];
  }
}

// ---------------------------------------------------------------- the chart
export class FxChart {
  constructor(el, legendEl, keyEl) {
    this.el = el;
    this.legendEl = legendEl;
    this.keyEl = keyEl;
    this.digits = 5;
    this.tfSec = 300;
    this.bars = [];
    this.lines = [];
    this.chart = LW.createChart(el, {
      autoSize: true,
      layout: { fontFamily: "ui-sans-serif, system-ui, sans-serif", fontSize: 11, attributionLogo: true },
      rightPriceScale: { borderVisible: false, scaleMargins: { top: 0.12, bottom: 0.12 } },
      timeScale: { borderVisible: false, timeVisible: true, secondsVisible: false, rightOffset: 4,
        tickMarkFormatter: (t, type) => {
          const d = new Date(t * 1000);
          if (type <= 2) return d.toLocaleDateString([], { month: "short", day: "numeric" });
          return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
        } },
      localization: { timeFormatter: (t) => new Date(t * 1000).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) },
      crosshair: { mode: LW.CrosshairMode.Normal },
      handleScale: { axisPressedMouseMove: { time: true, price: true } },
    });
    this.candles = this.chart.addCandlestickSeries({ priceLineVisible: true, lastValueVisible: true });
    this.median = this.chart.addLineSeries({ lineWidth: 2, lineStyle: LW.LineStyle.Dashed, priceLineVisible: false,
      lastValueVisible: false, crosshairMarkerVisible: false });
    this.band = new BandPrimitive();
    this.ranges = new RangeBoxPrimitive();
    this.candles.attachPrimitive(this.band);
    this.candles.attachPrimitive(this.ranges);
    this.chart.subscribeCrosshairMove((p) => this._legend(p));
    this.applyTheme();
  }

  applyTheme() {
    this.colors = {
      surface: cssVar("--surface-1"), text: cssVar("--text-secondary"), grid: cssVar("--grid"), ink: cssVar("--candle-ink"),
      upFill: cssVar("--candle-up-fill"), entry: cssVar("--entry"), stop: cssVar("--stop"), target: cssVar("--target"),
      band: cssVar("--band"), range: cssVar("--range"), primary: cssVar("--text-primary"),
    };
    const c = this.colors;
    this.chart.applyOptions({
      layout: { background: { type: "solid", color: c.surface }, textColor: c.text },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      crosshair: { vertLine: { color: c.text, labelBackgroundColor: c.primary }, horzLine: { color: c.text, labelBackgroundColor: c.primary } },
    });
    // Monochrome candles (hollow up, filled down) so the colour-coded levels stand out.
    this.candles.applyOptions({ upColor: c.upFill, downColor: c.ink, borderUpColor: c.ink, borderDownColor: c.ink,
      wickUpColor: c.ink, wickDownColor: c.ink, priceLineColor: c.text });
    this.median.applyOptions({ color: c.band });
    this.band.set(this.band.points, c.band);
    this.ranges.set(this.ranges.boxes, c.range, c.text);
    if (this.signal) this.setSignal(this.signal);
    this._key();
  }

  setInstrument(digits, tfSec) {
    this.digits = digits;
    this.tfSec = tfSec;
    const minMove = Math.pow(10, -digits);
    const fmt = { type: "price", precision: digits, minMove };
    this.candles.applyOptions({ priceFormat: fmt });
    this.median.applyOptions({ priceFormat: fmt });
  }

  setCandles(rows) {
    this.bars = rows.map((r) => ({ time: r[0], open: r[1], high: r[2], low: r[3], close: r[4] }));
    this.candles.setData(this.bars);
    this._legend(null);
  }

  fit(n = 120) {
    const total = this.bars.length + (this.medianData ? this.medianData.length : 0);
    this.chart.timeScale().setVisibleLogicalRange({ from: Math.max(0, total - n), to: total + 2 });
  }

  // a closed candle from the server replaces the forming one
  closedCandle(c) {
    const bar = { time: c.open_time, open: c.open, high: c.high, low: c.low, close: c.close };
    const last = this.bars[this.bars.length - 1];
    if (last && bar.time < last.time) return;
    if (last && last.time === bar.time) this.bars[this.bars.length - 1] = bar; else this.bars.push(bar);
    this.candles.update(bar);
    this._legend(null);
  }

  // a tick moves the forming candle
  tick(time, price) {
    const t = Math.floor(time / this.tfSec) * this.tfSec;
    const last = this.bars[this.bars.length - 1];
    if (!last || t < last.time) return;
    let bar;
    if (t > last.time) bar = { time: t, open: price, high: price, low: price, close: price };
    else bar = { ...last, high: Math.max(last.high, price), low: Math.min(last.low, price), close: price };
    if (t > last.time) this.bars.push(bar); else this.bars[this.bars.length - 1] = bar;
    this.candles.update(bar);
  }

  setForecast(f) {
    this.forecast = f;
    if (!f || !f.band || !f.band.length) {
      this.medianData = null;
      this.median.setData([]);
      this.band.set([]);
      this._key();
      return;
    }
    const start = { t: f.base_time, p10: f.last_close, p50: f.last_close, p90: f.last_close };
    const pts = [start, ...f.band.map((b) => ({ t: b.t, p10: b.p10, p50: b.p50, p90: b.p90 }))];
    this.medianData = pts.map((p) => ({ time: p.t, value: p.p50 }));
    this.median.setData(this.medianData);
    this.band.set(pts, this.colors.band);
    this._key();
  }

  setRanges(sessions) {
    const boxes = [];
    for (const s of sessions || []) {
      if (!s.range) continue;
      const lastT = this.bars.length ? this.bars[this.bars.length - 1].time : s.expiry;
      boxes.push({ start: s.range.start, end: Math.min(s.expiry, lastT), high: s.range.high, low: s.range.low,
        label: `${s.label.replace(" ORB", "")} range` });
    }
    this.ranges.set(boxes, this.colors.range, this.colors.text);
    this._key();
  }

  setSignal(sig) {
    for (const l of this.lines) this.candles.removePriceLine(l);
    this.lines = [];
    this.signal = sig;
    if (sig && sig.sl != null && sig.status !== "filtered" && sig.status !== "expired") {
      const c = this.colors;
      const add = (price, color, title, style = LW.LineStyle.Solid) => this.lines.push(this.candles.createPriceLine({
        price, color, title, lineWidth: 2, lineStyle: style, axisLabelVisible: true }));
      add(sig.entry, c.entry, "Entry", sig.status === "waiting" ? LW.LineStyle.Dashed : LW.LineStyle.Solid);
      add(sig.sl, c.stop, "SL");
      add(sig.tp1, c.target, "TP1");
      if (sig.tp2 != null && sig.tp2 !== sig.tp1) add(sig.tp2, c.target, "TP2", LW.LineStyle.Dashed);
    }
    this._key();
  }

  _key() {
    if (!this.keyEl || !this.colors) return;
    const c = this.colors;
    const items = [];
    if (this.forecast && this.forecast.band) {
      items.push(`<span class="key"><i class="box" style="background:${rgba(c.band, 0.3)}"></i>Forecast 10–90%</span>`);
      items.push(`<span class="key" style="color:${c.band}"><i class="dash"></i><span style="color:${c.text}">Median</span></span>`);
    }
    if (this.ranges.boxes.length) items.push(`<span class="key"><i class="box" style="background:${rgba(c.range, 0.25)}"></i>Session range</span>`);
    if (this.lines.length) {
      items.push(`<span class="key" style="color:${c.entry}"><i></i><span style="color:${c.text}">Entry</span></span>`);
      items.push(`<span class="key" style="color:${c.stop}"><i></i><span style="color:${c.text}">SL</span></span>`);
      items.push(`<span class="key" style="color:${c.target}"><i></i><span style="color:${c.text}">TP</span></span>`);
    }
    this.keyEl.innerHTML = items.join("");
  }

  _legend(param) {
    let bar = null;
    if (param && param.time !== undefined && param.seriesData) bar = param.seriesData.get(this.candles);
    if (!bar) bar = this.bars[this.bars.length - 1];
    if (!bar || bar.open === undefined) { this.legendEl.textContent = ""; return; }
    const f = (x) => x.toFixed(this.digits);
    this.legendEl.textContent = `O ${f(bar.open)}  H ${f(bar.high)}  L ${f(bar.low)}  C ${f(bar.close)}`;
  }
}
