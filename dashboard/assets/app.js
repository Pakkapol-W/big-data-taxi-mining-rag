/* Dashboard logic. Reads window.DASH (assets/data.js); computes every filtered value in the browser from it.
   Time: hours are naive New York local time; all date math uses UTC so the viewer's time zone never shifts them. */
(function () {
  'use strict';
  var D = window.DASH;
  if (!D || !window.echarts) {
    document.querySelector('main').innerHTML = '<div class="card"><b>Data or chart library missing.</b> ' +
      'Run <code>python run_pipeline.py --config config.json --mode full</code> to build assets/data.js, and keep the ' +
      'assets/ folder next to index.html.</div>';
    return;
  }
  var $ = function (id) { return document.getElementById(id); };
  var esc = function (s) { return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]; }); };
  var fmt = function (n) { return n == null || isNaN(n) ? '–' : Math.round(n).toLocaleString('en-US'); };
  var fx = function (n, d) { return n == null || isNaN(n) ? '–' : Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); };
  var pct = function (n, d) { return fx(n * 100, d == null ? 1 : d) + '%'; };

  // ------------------------------------------------------------------ time grid
  function parseNaive(s) { var p = s.split(' '), d = p[0].split('-'), h = p[1].split(':');
    return Date.UTC(+d[0], +d[1] - 1, +d[2], +h[0]); }
  var DAY = 864e5, T0 = parseNaive(D.meta.hours_start), NH = D.meta.n_hours, ND = Math.round(NH / 24);
  var dayStr = function (d) { return new Date(T0 + d * DAY).toISOString().slice(0, 10); };
  var dayOf = function (s) { return Math.round((Date.UTC(+s.slice(0, 4), +s.slice(5, 7) - 1, +s.slice(8, 10)) - T0) / DAY); };
  var dowOf = function (d) { return (new Date(T0 + d * DAY).getUTCDay() + 6) % 7; };  // Mon = 0
  var WD = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var F = D.forecast, FOFF = F.offset, FN = F.n_hours;
  var zoneById = {}; D.zones.forEach(function (z) { zoneById[z.id] = z; });
  var zname = function (id) { return zoneById[id] ? zoneById[id].name : 'zone ' + id; };

  // ------------------------------------------------------------------ state
  var ALL = D.zones.map(function (z) { return z.id; });
  var state = { zones: new Set(ALL), from: 0, to: ND - 1 };

  // ------------------------------------------------------------------ theme
  function tokens() {
    var cs = getComputedStyle(document.documentElement), g = function (v) { return cs.getPropertyValue(v).trim(); };
    return { surface: g('--surface'), ink: g('--ink'), ink2: g('--ink-2'), muted: g('--muted'), grid: g('--grid'),
      axis: g('--axis'), border: g('--border'), s1: g('--s1'), s2: g('--s2'), s3: g('--s3'), neutral: g('--neutral'),
      seq: g('--seq').split(',').map(function (x) { return x.trim(); }) };
  }
  var C = tokens();
  function setTheme(t) {
    if (t) document.documentElement.setAttribute('data-theme', t); else document.documentElement.removeAttribute('data-theme');
    try { if (t) localStorage.setItem('dash-theme', t); else localStorage.removeItem('dash-theme'); } catch (e) { /* storage blocked */ }
    C = tokens(); renderAll(true);
  }
  try { var saved = localStorage.getItem('dash-theme'); if (saved) document.documentElement.setAttribute('data-theme', saved); } catch (e) { /* ignore */ }
  C = tokens();
  $('themeBtn').addEventListener('click', function () {
    var dark = (document.documentElement.getAttribute('data-theme') || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light')) === 'dark';
    setTheme(dark ? 'light' : 'dark');
  });
  matchMedia('(prefers-color-scheme: dark)').addEventListener('change', function () { C = tokens(); renderAll(true); });

  // ------------------------------------------------------------------ chart helpers
  var charts = {};
  function chart(id) {
    if (!charts[id]) charts[id] = echarts.init($(id), null, { renderer: 'canvas' });
    return charts[id];
  }
  function setEmpty(id, msg) {
    var el = $(id), e = el.querySelector('.empty');
    if (msg) {
      if (!e) { e = document.createElement('div'); e.className = 'empty'; el.appendChild(e); }
      e.textContent = msg; if (charts[id]) charts[id].clear(); return true;
    }
    if (e) e.remove();
    return false;
  }
  function axisStyle(extra) {
    return Object.assign({ axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
      axisLabel: { color: C.muted, fontSize: 11 }, splitLine: { lineStyle: { color: C.grid, width: 1 } },
      nameTextStyle: { color: C.muted, fontSize: 11 } }, extra || {});
  }
  function base(extra) {
    return Object.assign({ backgroundColor: 'transparent', animationDuration: 250,
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif', color: C.ink2 },
      tooltip: { trigger: 'axis', backgroundColor: C.surface, borderColor: C.border, textStyle: { color: C.ink, fontSize: 12 },
        axisPointer: { type: 'line', lineStyle: { color: C.axis } }, confine: true },
      legend: { top: 0, left: 0, icon: 'roundRect', itemWidth: 12, itemHeight: 4, textStyle: { color: C.ink2, fontSize: 12 } },
      grid: { left: 16, right: 24, top: 36, bottom: 8, containLabel: true } }, extra || {});
  }
  var evChip = function (id) { return '<span class="ev" tabindex="0" data-ev="' + esc(id) + '">' + esc(id) + '</span>'; };
  var srcTag = function (k) { return '<code>' + esc(D.meta.sources[k]) + '</code>'; };

  // ------------------------------------------------------------------ selection math
  function selZones() { return ALL.filter(function (z) { return state.zones.has(z); }); }
  function problem() {
    if (!state.zones.size) return 'No zones selected - choose at least one zone or press Reset.';
    if (state.from > state.to) return 'Start date is after end date - adjust the dates or press Reset.';
    return null;
  }
  function selection() {
    var zs = selZones(), h0 = state.from * 24, h1 = (state.to + 1) * 24, n = 0, sum = 0, unknown = 0;
    zs.forEach(function (z) { var a = D.counts[z]; for (var h = h0; h < h1; h++) { if (a[h] == null) unknown++; else { n++; sum += a[h]; } } });
    var f0 = Math.max(h0, FOFF), f1 = Math.min(h1, FOFF + FN);
    return { zones: zs, h0: h0, h1: h1, n: n, sum: sum, unknown: unknown, days: state.to - state.from + 1,
      f0: f0 - FOFF, f1: f1 - FOFF, hasTest: f1 > f0 };
  }

  // ------------------------------------------------------------------ fixed blocks
  function renderKpis() {
    var q = D.quality.total, t = F.metrics.test;
    $('kpis').innerHTML = [
      ['Trip rows in', fmt(q.input), '3 monthly TLC files'],
      ['Retained trips', fmt(q.retained), pct(q.retained / q.input, 2) + ' of input'],
      ['Quarantined rows', fmt(q.quarantined), 'removed from pickup counts'],
      ['Forecast zone-hours', fmt(D.zones.length * NH), D.zones.length + ' zones × ' + fmt(NH) + ' hours'],
      ['Top-20 share of pickups', pct(D.eda.top20_share), 'of ' + D.eda.n_valid_zones + ' valid zones'],
      ['June MAE: model vs baseline', fx(t.mae_model, 2) + ' / ' + fx(t.mae_baseline, 2), esc(t.improvement_label) + ' lower than baseline']
    ].map(function (k) { return '<div class="tile"><div class="k">' + k[0] + '</div><div class="v">' + k[1] + '</div><div class="d">' + k[2] + '</div></div>'; }).join('');
  }

  function renderC1() {
    var rules = D.quality.rules, q = D.quality.total;
    $('c1sub').innerHTML = 'Input <b>' + fmt(q.input) + '</b> = retained <b>' + fmt(q.retained) + '</b> + quarantined <b>' +
      fmt(q.quarantined) + '</b>. A row is quarantined by its first failing rule only. Flags (' + fmt(q.any_flag) +
      ' rows with at least one flag) keep the trip in pickup counts and exclude it only from fare/distance analyses.';
    var cats = rules.map(function (r) { return r.id; });
    var lab = { show: true, position: 'right', color: C.ink2, fontSize: 11, formatter: function (p) { return p.value > 0 ? fmt(p.value) : ''; } };
    chart('c1').setOption(base({
      tooltip: Object.assign(base().tooltip, { trigger: 'item', formatter: function (p) {
        var r = rules[p.dataIndex]; return '<b>' + esc(r.id) + '</b> · ' + esc(r.action) + '<br>' + esc(r.condition) +
          '<br>' + p.seriesName + ': <b>' + fmt(p.value) + '</b> rows'; } }),
      grid: { left: 8, right: 90, top: 36, bottom: 30, containLabel: true },
      xAxis: axisStyle({ type: 'value', name: 'trip rows', nameLocation: 'middle', nameGap: 26, axisLabel: { color: C.muted, fontSize: 11, formatter: function (v) { return v >= 1e6 ? v / 1e6 + 'M' : v >= 1e3 ? v / 1e3 + 'k' : v; } } }),
      yAxis: axisStyle({ type: 'category', data: cats, inverse: true, splitLine: { show: false } }),
      series: [
        { name: 'Quarantined (removed)', type: 'bar', stack: 'r', barWidth: 14, itemStyle: { color: C.s1, borderRadius: [0, 4, 4, 0] }, label: lab, data: rules.map(function (r) { return r.quarantined; }) },
        { name: 'Flagged (kept)', type: 'bar', stack: 'r', barWidth: 14, itemStyle: { color: C.s3, borderRadius: [0, 4, 4, 0] }, label: lab, data: rules.map(function (r) { return r.flagged; }) }
      ]
    }), true);
    $('c1table').innerHTML = '<table><thead><tr><th>Rule</th><th>Condition</th><th>Action</th><th class="num">Quarantined</th><th class="num">Flagged</th></tr></thead><tbody>' +
      rules.map(function (r) { return '<tr><td>' + esc(r.id) + '</td><td>' + esc(r.condition) + '</td><td>' + esc(r.action) + '</td><td class="num">' + fmt(r.quarantined) + '</td><td class="num">' + fmt(r.flagged) + '</td></tr>'; }).join('') +
      '<tr><th>Total</th><td>input = retained + quarantined</td><td></td><td class="num"><b>' + fmt(q.quarantined) + '</b></td><td class="num"><b>' + fmt(q.any_flag) + '</b> rows</td></tr></tbody></table>';
    $('c1meta').innerHTML = 'Units: trip rows, all three months, all zones. Source: ' + srcTag('quality') + ' (src/clean.py). Evidence: ' +
      evChip('EV-QR-TOTAL') + evChip('EV-QR-R4B') + evChip('EV-EDA-QUALITY');
  }

  function clusterName(c) { return 'Cluster ' + c.cluster_id + ' · peak ' + c.peak_hour + ':00 · ' + c.size + ' zones'; }
  function clusterColor(id) { return [C.s1, C.s2, C.s3][id] || C.neutral; }
  function renderC4() {
    var cl = D.clusters, hours = []; for (var h = 0; h < 24; h++) hours.push(h + ':00');
    var series = [];
    D.zones.forEach(function (z) {
      var p = cl.member_profiles[z.id]; if (!p) return;
      series.push({ name: '_m' + z.id, type: 'line', data: p.map(function (v) { return v * 100; }), symbol: 'none', silent: true,
        lineStyle: { width: 1, color: clusterColor(z.cluster), opacity: 0.22 }, tooltip: { show: false }, z: 1 });
    });
    cl.clusters.forEach(function (c) {
      series.push({ name: clusterName(c), type: 'line', data: c.profile.map(function (v) { return v * 100; }), symbol: 'circle', symbolSize: 6, showSymbol: false,
        lineStyle: { width: 2.5, color: clusterColor(c.cluster_id) }, itemStyle: { color: clusterColor(c.cluster_id) }, z: 3,
        endLabel: { show: true, formatter: 'C' + c.cluster_id, color: C.ink2, fontSize: 11 } });
    });
    chart('c4').setOption(base({
      legend: Object.assign(base().legend, { data: cl.clusters.map(clusterName) }),
      tooltip: Object.assign(base().tooltip, { formatter: function (ps) {
        ps = ps.filter(function (p) { return p.seriesName.charAt(0) !== '_'; });
        return ps.length ? '<b>' + ps[0].axisValue + '</b><br>' + ps.map(function (p) { return p.marker + p.seriesName + ': <b>' + fx(p.value, 2) + '%</b>'; }).join('<br>') : ''; } }),
      grid: { left: 16, right: 36, top: 40, bottom: 8, containLabel: true },
      xAxis: axisStyle({ type: 'category', data: hours, boundaryGap: false, axisLabel: { color: C.muted, fontSize: 11, interval: 2 } }),
      yAxis: axisStyle({ type: 'value', name: '% of zone’s daily pickups', nameLocation: 'middle', nameGap: 40, axisLabel: { color: C.muted, fontSize: 11, formatter: '{value}%' } }),
      series: series
    }), true);
    chart('c4b').setOption(base({
      legend: { show: false },
      tooltip: Object.assign(base().tooltip, { trigger: 'item', formatter: function (p) { var c = cl.clusters[p.dataIndex];
        return '<b>Cluster ' + c.cluster_id + '</b> (' + c.size + ' zones)<br>' + c.zones.map(esc).join('<br>'); } }),
      grid: { left: 8, right: 40, top: 36, bottom: 28, containLabel: true },
      xAxis: axisStyle({ type: 'value', name: 'zones', nameLocation: 'middle', nameGap: 26, minInterval: 1 }),
      yAxis: axisStyle({ type: 'category', inverse: true, data: cl.clusters.map(function (c) { return 'Cluster ' + c.cluster_id; }), splitLine: { show: false } }),
      series: [{ type: 'bar', barWidth: 16, data: cl.clusters.map(function (c) { return { value: c.size, itemStyle: { color: clusterColor(c.cluster_id), borderRadius: [0, 4, 4, 0] } }; }),
        label: { show: true, position: 'right', color: C.ink2, fontSize: 12 } }]
    }), true);
    var night = cl.clusters.reduce(function (a, b) { return b.share_22_02 > a.share_22_02 ? b : a; });
    $('c4sub').innerHTML = 'Each zone’s April average pickups per hour of day, scaled to sum to 100% (shape only). K-means, chosen k = <b>' + cl.chosen_k +
      '</b>. Thin lines are member zones; thick lines are cluster averages. Cluster ' + night.cluster_id + ' (' + night.zones.map(esc).join(', ') + ') takes <b>' +
      pct(night.share_22_02) + '</b> of its pickups between 22:00 and 02:59.';
    $('c4table').innerHTML = '<table><thead><tr><th class="num">k</th><th class="num">Silhouette</th><th>Sizes</th><th class="num">May ARI</th><th>Chosen</th></tr></thead><tbody>' +
      cl.selection.map(function (r) { return '<tr><td class="num">' + r.k + '</td><td class="num">' + fx(r.silhouette, 3) + '</td><td>' + esc(r.sizes) + '</td><td class="num">' + fx(r.may_ari, 2) + '</td><td>' + (r.chosen ? '<b>chosen</b>' : '') + '</td></tr>'; }).join('') +
      '</tbody></table><p class="meta">Rule (fixed before results): ' + esc(cl.rule) + '.</p>';
    $('c4meta').innerHTML = 'Units: share of a zone’s daily pickups. Scope: 20 forecast zones, April 2024 (train month) only; June never used. Sources: ' +
      srcTag('cluster_profiles') + ', ' + srcTag('clusters') + ', ' + srcTag('cluster_selection') + ' (src/cluster.py). Evidence: ' +
      cl.clusters.map(function (c) { return evChip('EV-CLU-' + c.cluster_id); }).join('') + evChip('EV-CLU-SELECTION');
  }

  function renderForecastKpis() {
    var t = F.metrics.test, pk = F.metrics.peak, w = F.metrics.worst_zone;
    $('fkpis').innerHTML = [
      ['Model MAE · full June test', fx(t.mae_model, 2), 'RMSE ' + fx(t.rmse_model, 2) + ' · ' + fmt(t.rows) + ' zone-hours'],
      ['Baseline MAE (same hour last week)', fx(t.mae_baseline, 2), 'RMSE ' + fx(t.rmse_baseline, 2)],
      ['MAE improvement', '<span class="good">' + esc(t.improvement_label) + '</span>', 'model better in ' + F.metrics.zones_model_beats_baseline + ' of 20 zones'],
      ['Peak MAE (weekdays 07–09)', fx(pk.mae_model, 2) + ' / ' + fx(pk.mae_baseline, 2), 'model / baseline · ' + fmt(pk.rows) + ' rows'],
      ['Worst zone', esc(w.zone), 'MAE ' + fx(w.mae_model, 1) + ' vs baseline ' + fx(w.mae_baseline, 1)]
    ].map(function (k) { return '<div class="tile"><div class="k">' + k[0] + '</div><div class="v">' + k[1] + '</div><div class="d">' + k[2] + '</div></div>'; }).join('') +
      '<div class="tile"><div class="k">Selection (filtered)</div><div class="v" id="selMae">–</div><div class="d" id="selMaeD"></div></div>';
  }

  // ------------------------------------------------------------------ filtered charts
  function renderC2(s) {
    var msg = problem();
    $('c2meta').innerHTML = 'Units: recorded pickups per day (sum of selected zones). Time: New York local. Source: ' + srcTag('zone_hour') +
      ' (src/aggregate.py, src/store.py). Missing hours stay unknown (not zero). Evidence: ' + evChip('EV-EDA-TEMPORAL') + evChip('EV-DICT-DERIVED');
    $('c2bmeta').innerHTML = $('c2meta').innerHTML.replace('per day', 'per hour, averaged over the selected days');
    if (setEmpty('c2', msg) | setEmpty('c2b', msg)) { $('c2sub').textContent = ''; $('c2bsub').textContent = ''; return; }
    var days = [], vals = [], wk = new Array(24).fill(0), we = new Array(24).fill(0), nwk = new Array(24).fill(0), nwe = new Array(24).fill(0);
    for (var d = state.from; d <= state.to; d++) {
      var tot = 0, known = false;
      for (var hh = 0; hh < 24; hh++) {
        var hsum = 0, hknown = false;
        s.zones.forEach(function (z) { var v = D.counts[z][d * 24 + hh]; if (v != null) { hsum += v; hknown = true; } });
        if (hknown) { tot += hsum; known = true; if (dowOf(d) < 5) { wk[hh] += hsum; nwk[hh]++; } else { we[hh] += hsum; nwe[hh]++; } }
      }
      days.push(dayStr(d)); vals.push(known ? tot : null);
    }
    var maxI = vals.indexOf(Math.max.apply(null, vals.filter(function (v) { return v != null; })));
    chart('c2').setOption(base({
      legend: { show: false },
      tooltip: Object.assign(base().tooltip, { formatter: function (ps) { var p = ps[0], d = dayOf(p.axisValue);
        return '<b>' + p.axisValue + '</b> (' + WD[dowOf(d)] + ')<br>' + fmt(p.value) + ' pickups'; } }),
      xAxis: axisStyle({ type: 'category', data: days, boundaryGap: false, axisLabel: { color: C.muted, fontSize: 11, formatter: function (v) { return v.slice(5); } } }),
      yAxis: axisStyle({ type: 'value', name: 'pickups / day', nameLocation: 'middle', nameGap: 52, min: 0 }),
      series: [{ name: 'Daily pickups', type: 'line', data: vals, symbol: 'circle', symbolSize: 8, showSymbol: false, connectNulls: false,
        lineStyle: { width: 2, color: C.s1 }, itemStyle: { color: C.s1 },
        markPoint: maxI >= 0 ? { symbol: 'circle', symbolSize: 8, itemStyle: { color: C.s1, borderColor: C.surface, borderWidth: 2 },
          label: { position: 'top', color: C.ink2, fontSize: 11, formatter: function () { return 'max ' + fmt(vals[maxI]) + ' (' + days[maxI].slice(5) + ')'; } },
          data: [{ coord: [maxI, vals[maxI]] }] } : undefined }]
    }), true);
    $('c2sub').innerHTML = '<b>' + s.zones.length + '</b> zones, ' + days[0] + ' to ' + days[days.length - 1] + ' (' + s.days + ' days): <b>' + fmt(s.sum) + '</b> pickups' +
      (s.unknown ? '; <b>' + fmt(s.unknown) + '</b> zone-hours unknown (skipped)' : '') + '.';
    var mwk = wk.map(function (v, i) { return nwk[i] ? v / nwk[i] : null; }), mwe = we.map(function (v, i) { return nwe[i] ? v / nwe[i] : null; });
    var hours = []; for (var h = 0; h < 24; h++) hours.push(h + ':00');
    var line = function (name, data, col) { return { name: name, type: 'line', data: data, symbol: 'circle', symbolSize: 8, showSymbol: false,
      lineStyle: { width: 2, color: col }, itemStyle: { color: col }, endLabel: { show: true, formatter: name, color: C.ink2, fontSize: 11 } }; };
    chart('c2b').setOption(base({
      tooltip: Object.assign(base().tooltip, { valueFormatter: function (v) { return fx(v, 1) + ' pickups/h'; } }),
      grid: { left: 16, right: 70, top: 36, bottom: 8, containLabel: true },
      xAxis: axisStyle({ type: 'category', data: hours, boundaryGap: false, axisLabel: { color: C.muted, fontSize: 11, interval: 2 } }),
      yAxis: axisStyle({ type: 'value', name: 'pickups / hour', nameLocation: 'middle', nameGap: 44, min: 0 }),
      series: [line('Weekday', mwk, C.s1), line('Weekend', mwe, C.s2)]
    }), true);
    var pk = mwk.indexOf(Math.max.apply(null, mwk.filter(function (v) { return v != null; })));
    var nWkDays = 0; for (var dd = state.from; dd <= state.to; dd++) if (dowOf(dd) < 5) nWkDays++;
    $('c2bsub').innerHTML = (pk >= 0 ? 'Weekday peak in this selection: <b>' + pk + ':00</b> (' + fx(mwk[pk], 1) + ' pickups/h). ' : 'No weekday in the selected range. ') +
      nWkDays + ' weekday(s), ' + (s.days - nWkDays) + ' weekend day(s) averaged.' + (s.days - nWkDays === 0 ? ' Weekend line empty: no weekend day selected.' : '');
  }

  function renderC3(s) {
    var msg = problem();
    $('c3meta').innerHTML = 'Units: mean recorded pickups per hour over the selected days. Rows ordered by April rank. Source: ' + srcTag('zone_hour') +
      '; names from data/dim/zones.parquet. Evidence: ' + evChip('EV-EDA-ZONES');
    if (setEmpty('c3', msg)) { $('c3').style.height = '260px'; $('c3sub').textContent = ''; return; }
    var zs = s.zones.slice().sort(function (a, b) { return zoneById[a].rank - zoneById[b].rank; }), data = [], mx = 0;
    zs.forEach(function (z, yi) {
      for (var h = 0; h < 24; h++) {
        var sum = 0, n = 0;
        for (var d = state.from; d <= state.to; d++) { var v = D.counts[z][d * 24 + h]; if (v != null) { sum += v; n++; } }
        var m = n ? sum / n : null; if (m != null && m > mx) mx = m;
        data.push([h, yi, m == null ? '-' : Math.round(m * 10) / 10, n]);
      }
    });
    $('c3').style.height = Math.max(260, 60 + zs.length * 24) + 'px'; chart('c3').resize();
    chart('c3').setOption(base({
      legend: { show: false },
      tooltip: Object.assign(base().tooltip, { trigger: 'item', formatter: function (p) {
        return '<b>' + esc(zname(zs[p.value[1]])) + '</b> · ' + p.value[0] + ':00<br>' + fx(p.value[2], 1) + ' pickups/h (mean of ' + p.value[3] + ' days)'; } }),
      grid: { left: 8, right: 16, top: 8, bottom: 56, containLabel: true },
      xAxis: axisStyle({ type: 'category', data: Array.from({ length: 24 }, function (_, i) { return i; }), splitLine: { show: false }, name: 'hour of day', nameLocation: 'middle', nameGap: 24 }),
      yAxis: axisStyle({ type: 'category', data: zs.map(function (z) { return zname(z) + ' (' + z + ')'; }), inverse: true, splitLine: { show: false } }),
      visualMap: { min: 0, max: Math.max(1, Math.ceil(mx)), calculable: false, orient: 'horizontal', left: 'center', bottom: 0, itemWidth: 12, itemHeight: 160,
        dimension: 2, inRange: { color: C.seq }, textStyle: { color: C.muted, fontSize: 11 }, text: ['high', '0'] },
      series: [{ type: 'heatmap', data: data, itemStyle: { borderColor: C.surface, borderWidth: 1 }, emphasis: { itemStyle: { borderColor: C.ink, borderWidth: 1 } } }]
    }), true);
    $('c3sub').innerHTML = s.zones.length + ' zones × 24 hours of day, averaged over ' + s.days + ' days. Darker = more pickups.';
  }

  function renderC5C6(s) {
    var msg = problem() || (!s.hasTest ? 'The forecast covers June 2024 only. Widen the date range to include June (or press “Jun”).' : null);
    $('c5meta').innerHTML = 'Units: recorded pickups per hour (sum of selected zones). Prediction made at the end of the previous hour from past counts only; baseline = same hour 7 days earlier; negative predictions clipped to 0. Source: ' +
      srcTag('predictions') + ' (src/forecast.py). Evidence: ' + evChip('EV-MOD-SPEC') + evChip('EV-MOD-OVERALL') + evChip('EV-MOD-PEAK');
    $('c6meta').innerHTML = 'Units: mean absolute error, pickups per zone-hour, over the selected June hours; identical rows for model and baseline. Source: ' +
      srcTag('predictions') + ', ' + srcTag('metrics') + '. Evidence: ' + evChip('EV-MOD-ZONES') + evChip('EV-MOD-WORST');
    var e5 = setEmpty('c5', msg), e6 = setEmpty('c6', msg);
    if (e5 || e6) { $('c6').style.height = '260px'; $('c5sub').textContent = ''; $('c6sub').textContent = ''; $('selMae').textContent = '–'; $('selMaeD').textContent = msg ? 'no June hours selected' : ''; $('c6table').innerHTML = ''; return; }
    var xs = [], ys = [], bs = [], ps = [];
    for (var i = s.f0; i < s.f1; i++) {
      var y = 0, b = 0, p = 0;
      s.zones.forEach(function (z) { var r = F.zones[z]; y += r.y[i]; b += r.b[i]; p += r.p[i]; });
      var t = new Date(T0 + (FOFF + i) * 3600e3).toISOString(); xs.push(t.slice(5, 10) + ' ' + t.slice(11, 13) + ':00');
      ys.push(y); bs.push(b); ps.push(Math.round(p * 10) / 10);
    }
    var ln = function (name, data, col, w) { return { name: name, type: 'line', data: data, symbol: 'circle', symbolSize: 8, showSymbol: false, sampling: 'lttb',
      lineStyle: { width: w, color: col }, itemStyle: { color: col } }; };
    chart('c5').setOption(base({
      tooltip: Object.assign(base().tooltip, { valueFormatter: function (v) { return fx(v, 1); } }),
      grid: { left: 30, right: 24, top: 36, bottom: 56, containLabel: true },
      dataZoom: [{ type: 'inside' }, { type: 'slider', height: 18, bottom: 8, borderColor: C.border, textStyle: { color: C.muted, fontSize: 10 } }],
      xAxis: axisStyle({ type: 'category', data: xs, boundaryGap: false, axisLabel: { color: C.muted, fontSize: 11 } }),
      yAxis: axisStyle({ type: 'value', name: 'pickups / hour', nameLocation: 'middle', nameGap: 44, min: 0 }),
      series: [ln('Actual', ys, C.s1, 2), ln('Model prediction', ps, C.s2, 1.5), ln('Baseline (last week)', bs, C.neutral, 1.5)]
    }), true);
    // per-zone MAE over the selected June hours
    var rows = s.zones.map(function (z) { var r = F.zones[z], em = 0, eb = 0, n = s.f1 - s.f0, sy = 0;
      for (var i = s.f0; i < s.f1; i++) { em += Math.abs(r.y[i] - r.p[i]); eb += Math.abs(r.y[i] - r.b[i]); sy += r.y[i]; }
      return { z: z, m: em / n, b: eb / n, n: n, mean: sy / n }; }).sort(function (a, b) { return b.m - a.m; });
    var tm = 0, tb = 0, tn = 0; rows.forEach(function (r) { tm += r.m * r.n; tb += r.b * r.n; tn += r.n; });
    $('c6').style.height = Math.max(240, 70 + rows.length * 26) + 'px'; chart('c6').resize();
    chart('c6').setOption(base({
      tooltip: Object.assign(base().tooltip, { axisPointer: { type: 'shadow' }, valueFormatter: function (v) { return fx(v, 2); } }),
      grid: { left: 8, right: 24, top: 36, bottom: 30, containLabel: true },
      xAxis: axisStyle({ type: 'value', name: 'MAE (pickups / zone-hour)', nameLocation: 'middle', nameGap: 26 }),
      yAxis: axisStyle({ type: 'category', inverse: true, data: rows.map(function (r) { return zname(r.z) + ' (' + r.z + ')'; }), splitLine: { show: false } }),
      series: [
        { name: 'Model', type: 'bar', barWidth: 8, barGap: '25%', itemStyle: { color: C.s2, borderRadius: [0, 4, 4, 0] }, data: rows.map(function (r) { return r.m; }) },
        { name: 'Baseline', type: 'bar', barWidth: 8, itemStyle: { color: C.neutral, borderRadius: [0, 4, 4, 0] }, data: rows.map(function (r) { return r.b; }) }
      ]
    }), true);
    var better = rows.filter(function (r) { return r.m < r.b; }).length, from = xs[0], to = xs[xs.length - 1];
    $('c5sub').innerHTML = s.zones.length + ' zones, ' + fmt(xs.length) + ' June hours (' + from + ' to ' + to + '). Peak period for error analysis: ' + esc(F.peak_def.definition) + '. Drag the slider to zoom.';
    $('c6sub').innerHTML = 'Selection: model MAE <b>' + fx(tm / tn, 2) + '</b> vs baseline <b>' + fx(tb / tn, 2) + '</b> over ' + fmt(tn) + ' zone-hours; model better in <b>' + better + ' of ' + rows.length + '</b> zones.';
    $('selMae').textContent = fx(tm / tn, 2) + ' / ' + fx(tb / tn, 2);
    $('selMaeD').textContent = 'model / baseline MAE · ' + fmt(tn) + ' zone-hours';
    $('c6table').innerHTML = '<table><thead><tr><th>Zone</th><th class="num">Mean pickups/h</th><th class="num">Model MAE</th><th class="num">Baseline MAE</th><th class="num">Hours</th></tr></thead><tbody>' +
      rows.map(function (r) { return '<tr><td>' + esc(zname(r.z)) + ' (' + r.z + ')</td><td class="num">' + fx(r.mean, 1) + '</td><td class="num">' + fx(r.m, 2) + '</td><td class="num">' + fx(r.b, 2) + '</td><td class="num">' + r.n + '</td></tr>'; }).join('') + '</tbody></table>';
  }

  // ------------------------------------------------------------------ LLM section
  function pill(ok, label) { return '<span class="pill ' + (ok === true ? 'pass' : ok === false ? 'fail' : 'pending') + '">' + label + '</span>'; }
  function linkIds(text) { return esc(text).replace(/EV-[A-Z0-9]+(?:-[A-Z0-9]+)*/g, function (m) { return D.evidence[m] ? evChip(m) : '<span class="pill fail">' + m + ' (not in corpus)</span>'; }); }
  function renderLLM() {
    var L = D.llm;
    if (!L) { $('llmIntro').textContent = 'No saved LLM outputs yet (run scripts/run_llm.py).'; return; }
    var s = L.summary;
    $('llmIntro').innerHTML = 'Question → embedding (' + esc(L.embedding_model) + ') → cosine top-3 evidence chunks → prompt with evidence → LLM (' + esc(L.model) +
      ', local Ollama, digest ' + esc(L.model_digest) + ', temperature 0) → automatic checks + human verification. Each question was also asked <i>without</i> evidence. ' +
      'Reference answers were written on ' + esc(L.questions_written_at.slice(0, 10)) + ', before any query. Outputs below are the original model text; human corrections are shown separately.';
    var hp = s.human_passed;
    $('llmKpis').innerHTML = [['Retrieval found expected evidence', s.retrieval_hits, 'answerable questions'], ['Automatic checks passed', s.auto_passed || '–', 'citations, numbers, refusals'],
      ['Human-verified', s.human_reviewed || '–', 'passed: ' + (hp || 'pending')], ['Saved genuine outputs', String(s.llm_runs), '6 questions × with / without evidence']]
      .map(function (k) { return '<div class="tile"><div class="k">' + k[0] + '</div><div class="v">' + esc(k[1]) + '</div><div class="d">' + esc(k[2]) + '</div></div>'; }).join('');
    $('llmItems').innerHTML = L.items.map(function (it) {
      var ans = function (cond, title) { var r = it.runs[cond]; if (!r) return '<div class="ans"><h4>' + title + '</h4><i>not run</i></div>';
        var hv = r.human_verdict;
        return '<div class="ans"><h4>' + title + '</h4><pre>' + linkIds(r.output) + '</pre><div class="checks">' + pill(r.auto_pass, 'auto ' + (r.auto_pass ? 'pass' : 'fail')) +
          pill(hv === 'pass' ? true : hv === 'fail' ? false : null, 'human ' + (hv || 'pending')) +
          (r.auto_failed.length ? ' failed: ' + esc(r.auto_failed.join(', ')) : '') + (r.unsupported_numbers.length ? ' · unsupported numbers: ' + esc(r.unsupported_numbers.join(', ')) : '') +
          (r.human_errors && r.human_errors.length ? '<br>Human-noted errors: ' + esc(r.human_errors.join('; ')) : '') +
          (r.corrected ? '<br><b>Human-corrected answer</b> (separate from the original): ' + linkIds(r.corrected) : '') +
          '<br><span class="meta">' + esc(r.model) + ' · ' + esc(r.date) + ' · <code>' + esc(r.file) + '</code></span></div></div>'; };
      return '<article class="qa"><div class="qa-head"><span class="qid">' + esc(it.id) + '</span><span class="qtype">' + esc(it.type.replace('_', ' ')) + '</span>' +
        (it.answerable ? '' : '<span class="pill pending">expected: insufficient evidence</span>') + '</div><p class="q">' + esc(it.question) + '</p>' +
        '<p class="hits">Retrieved top-3: ' + it.top_k.map(function (h) { return evChip(h.evidence_id) + '<span class="meta">' + fx(h.score, 3) + '</span>'; }).join(' ') + '</p>' +
        '<div class="qa-grid">' + ans('with_evidence', 'With retrieved evidence') + ans('without_evidence', 'Without evidence') + '</div>' +
        '<p class="ref">Reference (written before querying): ' + it.reference_facts.map(esc).join('; ') + '</p></article>';
    }).join('');
  }

  // ------------------------------------------------------------------ narrative + lineage
  function renderAdvice() {
    var li = function (x) { return '<li>' + esc(x.text) + ' ' + x.evidence.map(evChip).join('') + '</li>'; };
    $('findings').innerHTML = D.narrative.findings.map(li).join('');
    $('recs').innerHTML = D.narrative.recommendations.map(li).join('');
    $('limits').innerHTML = D.narrative.limitations.map(li).join('');
    var S = D.meta.sources, rows = [
      ['KPI tiles', [S.quality, S.metrics, S.eda], 'clean rules → counts; forecast metrics', ['EV-QR-TOTAL', 'EV-MOD-OVERALL']],
      ['Chart 1 quality', [S.quality], 'src/clean.py rules R1–R7, R4b', ['EV-QR-TOTAL', 'EV-EDA-QUALITY']],
      ['Charts 2a, 2b, 3', [S.zone_hour, S.top_zones], 'clean → aggregate (A = B) → zone-hour grid', ['EV-EDA-TEMPORAL', 'EV-EDA-ZONES']],
      ['Chart 4 clusters', [S.cluster_profiles, S.clusters, S.cluster_selection], 'src/cluster.py (April profiles, k-means)', ['EV-CLU-SELECTION']],
      ['Charts 5, 6 forecast', [S.predictions, S.metrics], 'src/features.py → src/forecast.py (frozen spec, June test)', ['EV-MOD-OVERALL', 'EV-MOD-ZONES']],
      ['LLM evidence', [S.rag, S.corpus], 'src/evidence.py → src/rag.py → scripts/run_llm.py → src/rag_verify.py', []]];
    $('lineageTable').innerHTML = '<table><thead><tr><th>Shown as</th><th>Source file(s)</th><th>Produced by</th><th>Evidence</th></tr></thead><tbody>' +
      rows.map(function (r) { return '<tr><td>' + r[0] + '</td><td>' + r[1].map(function (f) { return '<code>' + esc(f) + '</code>'; }).join('<br>') + '</td><td>' + esc(r[2]) + '</td><td>' + r[3].map(evChip).join('') + '</td></tr>'; }).join('') + '</tbody></table>';
    $('genMeta').textContent = 'Data built ' + D.meta.generated_at + ' (mode ' + D.meta.mode + ') by src/dashboard.py. Times are ' + D.meta.time_note +
      '. Split: train ' + D.meta.splits.train + ', validation ' + D.meta.splits.valid + ', test ' + D.meta.splits.test + '. Opens offline: all scripts are local files.';
  }

  // evidence pop-over (hover + keyboard focus)
  var pop = $('tooltipEv');
  function showEv(el) { var e = D.evidence[el.getAttribute('data-ev')]; if (!e) return;
    pop.innerHTML = '<b>' + esc(el.getAttribute('data-ev')) + ' · ' + esc(e.title) + '</b>' + esc(e.text) + '<div class="meta">' + esc(e.source) + '</div>';
    pop.hidden = false; var r = el.getBoundingClientRect(), w = Math.min(420, window.innerWidth - 24);
    pop.style.left = Math.max(12, Math.min(window.scrollX + r.left, window.scrollX + window.innerWidth - w - 12)) + 'px';
    pop.style.top = (window.scrollY + r.bottom + 6) + 'px'; }
  function hideEv() { pop.hidden = true; }
  document.addEventListener('mouseover', function (e) { var t = e.target.closest && e.target.closest('.ev'); if (t) showEv(t); else if (!pop.contains(e.target)) hideEv(); });
  document.addEventListener('focusin', function (e) { if (e.target.classList && e.target.classList.contains('ev')) showEv(e.target); });
  document.addEventListener('focusout', hideEv);
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape') hideEv(); });

  // ------------------------------------------------------------------ filters
  function buildFilters() {
    $('zoneList').innerHTML = D.zones.map(function (z) { return '<label><input type="checkbox" value="' + z.id + '" checked> ' + z.rank + '. ' + esc(z.name) +
      ' <span class="zmeta">' + z.id + (z.cluster != null ? ' · C' + z.cluster : '') + '</span></label>'; }).join('');
    $('clusterBtns').innerHTML = D.clusters.clusters.map(function (c) { return '<button type="button" class="btn small" data-cluster="' + c.cluster_id + '">Cluster ' + c.cluster_id + '</button>'; }).join(' ');
    var dmin = dayStr(0), dmax = dayStr(ND - 1);
    ['dFrom', 'dTo'].forEach(function (id) { $(id).min = dmin; $(id).max = dmax; });
    $('zoneList').addEventListener('change', function (e) { var z = +e.target.value; if (e.target.checked) state.zones.add(z); else state.zones.delete(z); update(); });
    document.querySelector('.zone-actions').addEventListener('click', function (e) {
      var b = e.target.closest('button'); if (!b) return;
      if (b.dataset.zones === 'all') state.zones = new Set(ALL);
      else if (b.dataset.zones === 'none') state.zones = new Set();
      else if (b.dataset.cluster != null) state.zones = new Set(D.zones.filter(function (z) { return z.cluster === +b.dataset.cluster; }).map(function (z) { return z.id; }));
      syncInputs(); update();
    });
    var onDate = function () { var f = $('dFrom').value, t = $('dTo').value;
      state.from = f ? Math.max(0, Math.min(ND - 1, dayOf(f))) : 0; state.to = t ? Math.max(0, Math.min(ND - 1, dayOf(t))) : ND - 1; update(); };
    $('dFrom').addEventListener('change', onDate); $('dTo').addEventListener('change', onDate);
    document.querySelector('.presets').addEventListener('click', function (e) { var b = e.target.closest('button'); if (!b) return; var p = b.dataset.preset;
      if (p === 'all') { state.from = 0; state.to = ND - 1; }
      else { var first = dayOf(p + '-01'), last = first; while (last + 1 < ND && dayStr(last + 1).slice(0, 7) === p) last++; state.from = first; state.to = last; }
      syncInputs(); update(); });
    $('resetBtn').addEventListener('click', function () { state = { zones: new Set(ALL), from: 0, to: ND - 1 }; syncInputs(); update(); });
    document.addEventListener('click', function (e) { var zp = $('zonePick'); if (zp.open && !zp.contains(e.target)) zp.open = false; });
    syncInputs();
  }
  function syncInputs() {
    Array.prototype.forEach.call(document.querySelectorAll('#zoneList input'), function (i) { i.checked = state.zones.has(+i.value); });
    $('dFrom').value = dayStr(state.from); $('dTo').value = dayStr(state.to);
    Array.prototype.forEach.call(document.querySelectorAll('.presets .btn'), function (b) { var p = b.dataset.preset, on;
      if (p === 'all') on = state.from === 0 && state.to === ND - 1;
      else on = dayStr(state.from) === p + '-01' && dayStr(state.to).slice(0, 7) === p && (state.to + 1 >= ND || dayStr(state.to + 1).slice(0, 7) !== p);
      b.classList.toggle('active', on); });
  }

  function update() {
    syncInputs();
    var s = selection(), msg = problem();
    $('zoneCount').textContent = state.zones.size + ' / ' + ALL.length;
    $('selSummary').innerHTML = msg ? '<span class="warn">' + esc(msg) + '</span>' :
      'Selection: <b>' + s.zones.length + '</b> zones · <b>' + dayStr(state.from) + '</b> to <b>' + dayStr(state.to) + '</b> (' + s.days + ' days) · <b>' +
      fmt(s.n) + '</b> zone-hours · <b>' + fmt(s.sum) + '</b> recorded pickups' + (s.unknown ? ' · ' + fmt(s.unknown) + ' unknown zone-hours' : '') +
      ' · forecast rows: <b>' + fmt(s.hasTest ? (s.f1 - s.f0) * s.zones.length : 0) + '</b>';
    renderC2(s); renderC3(s); renderC5C6(s);
  }

  function renderAll(themeChanged) {
    if (themeChanged) { Object.keys(charts).forEach(function (k) { charts[k].dispose(); }); charts = {}; }
    renderC1(); renderC4(); update();
  }

  // optional deep link: index.html?zones=48,79&from=2024-06-01&to=2024-06-07 (used for T9 screenshots)
  (function () {
    var q = new URLSearchParams(location.search), z = q.get('zones'), f = q.get('from'), t = q.get('to');
    if (z != null) state.zones = new Set(z.split(',').map(Number).filter(function (x) { return zoneById[x]; }));
    if (f) state.from = Math.max(0, Math.min(ND - 1, dayOf(f)));
    if (t) state.to = Math.max(0, Math.min(ND - 1, dayOf(t)));
  })();
  buildFilters(); renderKpis(); renderForecastKpis(); renderLLM(); renderAdvice(); renderAll(false);
  window.addEventListener('resize', function () { Object.keys(charts).forEach(function (k) { charts[k].resize(); }); });
  window.__dash = { state: function () { return state; }, selection: selection };  // for manual T9/T10 checks in the console
})();
