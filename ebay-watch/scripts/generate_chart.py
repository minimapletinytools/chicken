#!/usr/bin/env python3
"""
Generates a static HTML page (docs/index.html, inside this automation's
folder) charting each search's BIN price history (min/median/max over
time), filterable by tag.

Reads config/ebay_searches.yml for tags/priority and each search's
.github/data/ebay-watch/<id>.json for history. Priority is folded in as an
implicit tag ("high"/"normal") alongside whatever's in `tags`.

The page itself loads Chart.js + its date adapter from a CDN at view time —
open the output file directly in a browser, or serve it via GitHub Pages.

Assumes it lives at <automation folder>/scripts/generate_chart.py, with the
automation folder at the root of a git repo — see ebay_watch.py's docstring.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import yaml

AUTOMATION_ROOT = Path(__file__).resolve().parent.parent
REPO_ROOT = AUTOMATION_ROOT.parent
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", AUTOMATION_ROOT / "config" / "ebay_searches.yml"))
CACHE_DIR = Path(os.environ.get("CACHE_DIR", REPO_ROOT / ".github" / "data" / "ebay-watch"))
OUTPUT_PATH = Path(os.environ.get("OUTPUT_PATH", AUTOMATION_ROOT / "docs" / "index.html"))

HTML_TEMPLATE = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>eBay Watch — Price History</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
<script src="https://cdn.jsdelivr.net/npm/chartjs-adapter-date-fns"></script>
<style>
  body { font-family: system-ui, sans-serif; margin: 2rem; background: #111; color: #eee; }
  h1 { font-size: 1.3rem; }
  #controls { margin-bottom: 1rem; display: flex; gap: 2rem; flex-wrap: wrap; align-items: baseline; }
  #tags label { margin-right: 1rem; cursor: pointer; }
  select { background: #222; color: #eee; border: 1px solid #444; padding: 2px 6px; }
  #chart-row { display: flex; gap: 1.5rem; align-items: flex-start; }
  #chart-container { max-width: 900px; flex: 1 1 auto; }
  #legend {
    display: flex; flex-direction: column; gap: 1px; max-height: 520px; overflow-y: auto;
    min-width: 220px; max-width: 340px; flex: 0 0 auto;
  }
  .legend-row {
    display: flex; align-items: center; gap: 6px; padding: 3px 6px; border-radius: 4px;
    cursor: pointer; transition: background-color 0.15s ease, opacity 0.15s ease;
  }
  .legend-row:hover, .legend-row.legend-active { background-color: rgba(255,255,255,0.12); }
  .legend-row.legend-hidden { opacity: 0.35; }
  .legend-swatch { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .legend-label { flex: 1 1 auto; font-size: 0.85rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .legend-link { color: #8ab4f8; text-decoration: none; font-size: 0.85rem; flex-shrink: 0; }
  .legend-link:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>eBay Watch — Price History</h1>
<div id="controls">
  <div>
    Metric:
    <select id="metric">
      <option value="bin_min" selected>Min BIN</option>
      <option value="bin_median">Median BIN</option>
      <option value="bin_max">Max BIN</option>
    </select>
  </div>
  <div id="tags"></div>
</div>
<div id="chart-row">
  <div id="chart-container"><canvas id="chart"></canvas></div>
  <div id="legend"></div>
</div>
<script>
const DATA = __DATA__;

const COLORS = ['#e6194b','#3cb44b','#ffe119','#4363d8','#f58231','#911eb4','#46f0f0',
  '#f032e6','#bcf60c','#fabebe','#008080','#e6beff','#9a6324','#fffac8','#800000',
  '#aaffc3','#808000','#ffd8b1','#000075','#a9a9a9','#ffffff'];

const BORDER_WIDTH = 2;
const BORDER_WIDTH_ACTIVE = 3.5;
const DIM_ALPHA = 0.12;

function ebaySearchUrl(query) {
  return 'https://www.ebay.com/sch/i.html?_nkw=' + encodeURIComponent(query);
}

function withAlpha(hex, alpha) {
  const r = parseInt(hex.slice(1, 3), 16);
  const g = parseInt(hex.slice(3, 5), 16);
  const b = parseInt(hex.slice(5, 7), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

const allTags = [...new Set(DATA.flatMap(d => d.tags))].sort();
const tagsEl = document.getElementById('tags');
allTags.forEach(tag => {
  const label = document.createElement('label');
  const cb = document.createElement('input');
  cb.type = 'checkbox';
  cb.checked = true;
  cb.value = tag;
  cb.addEventListener('change', render);
  label.appendChild(cb);
  label.append(' ' + tag);
  tagsEl.appendChild(label);
});

let chart;

function buildLegend(visible) {
  const legendEl = document.getElementById('legend');
  legendEl.innerHTML = '';
  visible.forEach((d, i) => {
    const row = document.createElement('div');
    row.className = 'legend-row';
    row.dataset.index = String(i);

    const swatch = document.createElement('span');
    swatch.className = 'legend-swatch';
    swatch.style.backgroundColor = COLORS[i % COLORS.length];

    const label = document.createElement('span');
    label.className = 'legend-label';
    label.textContent = d.query;
    label.title = d.query;

    const searchLink = document.createElement('a');
    searchLink.className = 'legend-link';
    searchLink.href = ebaySearchUrl(d.query);
    searchLink.target = '_blank';
    searchLink.rel = 'noopener';
    searchLink.title = 'Open eBay search in new tab';
    searchLink.textContent = '🔍';
    searchLink.addEventListener('click', event => event.stopPropagation());

    row.append(swatch, label, searchLink);

    if (d.min_price_url) {
      const priceLink = document.createElement('a');
      priceLink.className = 'legend-link';
      priceLink.href = d.min_price_url;
      priceLink.target = '_blank';
      priceLink.rel = 'noopener';
      priceLink.title = 'Open cheapest current listing in new tab';
      priceLink.textContent = '🏷️';
      priceLink.addEventListener('click', event => event.stopPropagation());
      row.append(priceLink);
    }
    row.addEventListener('click', () => {
      if (chart.isDatasetVisible(i)) {
        chart.hide(i);
        row.classList.add('legend-hidden');
      } else {
        chart.show(i);
        row.classList.remove('legend-hidden');
      }
    });
    row.addEventListener('mouseenter', () => setActiveDataset(i));
    row.addEventListener('mouseleave', () => setActiveDataset(-1));

    legendEl.appendChild(row);
  });
}

// Shared by both directions: hovering a line highlights its legend row,
// and hovering a legend row highlights (and un-dims) its line.
function setActiveDataset(index) {
  if (!chart) return;
  chart.data.datasets.forEach((ds, idx) => {
    const color = COLORS[idx % COLORS.length];
    if (index === -1) {
      ds.borderColor = color;
      ds.borderWidth = BORDER_WIDTH;
    } else if (idx === index) {
      ds.borderColor = color;
      ds.borderWidth = BORDER_WIDTH_ACTIVE;
    } else {
      ds.borderColor = withAlpha(color, DIM_ALPHA);
      ds.borderWidth = BORDER_WIDTH;
    }
  });
  chart.update('none');

  document.querySelectorAll('.legend-row').forEach(row => {
    row.classList.toggle('legend-active', row.dataset.index === String(index));
  });
}

function render() {
  const metric = document.getElementById('metric').value;
  const active = new Set([...tagsEl.querySelectorAll('input:checked')].map(cb => cb.value));
  const visible = DATA.filter(d => d.tags.some(t => active.has(t)));

  const datasets = visible.map((d, i) => ({
    label: d.query,
    data: d.history.map(h => ({ x: h.timestamp, y: h[metric] })),
    borderColor: COLORS[i % COLORS.length],
    backgroundColor: COLORS[i % COLORS.length],
    borderWidth: BORDER_WIDTH,
    spanGaps: true,
    tension: 0.15,
    pointRadius: 2,
  }));

  if (chart) chart.destroy();
  chart = new Chart(document.getElementById('chart'), {
    type: 'line',
    data: { datasets },
    options: {
      responsive: true,
      // intersect: false so hovering anywhere near a line (not just exactly
      // on a data point) picks up the nearest one — makes it easy to tell
      // lines apart with this many series.
      interaction: { mode: 'nearest', intersect: false, axis: 'xy' },
      onHover: (event, elements) => {
        setActiveDataset(elements.length ? elements[0].datasetIndex : -1);
      },
      scales: {
        x: {
          type: 'time',
          time: {
            unit: 'day',
            tooltipFormat: 'MMM d, yyyy HH:mm',
            displayFormats: { day: 'MMM d, yyyy' },
          },
          ticks: { color: '#ccc' },
          grid: { color: '#333' },
          title: { display: true, text: 'Date', color: '#ccc' },
        },
        y: { ticks: { color: '#ccc' }, grid: { color: '#333' }, title: { display: true, text: 'USD', color: '#ccc' } },
      },
      plugins: { legend: { display: false } },
    },
  });

  buildLegend(visible);
}

document.getElementById('metric').addEventListener('change', render);
render();
</script>
</body>
</html>
"""


def latest_min_price_url(history: list[dict]) -> str | None:
    # Walk backwards to the most recent run that actually had a BIN listing —
    # the latest run alone might have none (e.g. everything sold/delisted).
    for entry in reversed(history):
        url = entry.get("bin_min_url")
        if url:
            return url
    return None


def build_dataset() -> list[dict]:
    searches = yaml.safe_load(CONFIG_PATH.read_text()) or []
    dataset = []
    for search in searches:
        search_id = search["id"]
        cache_path = CACHE_DIR / f"{search_id}.json"
        if not cache_path.exists():
            continue
        cache = json.loads(cache_path.read_text())
        history = cache.get("history", [])
        tags = list(search.get("tags", [])) + [search.get("priority", "normal")]
        dataset.append(
            {
                "id": search_id,
                "query": search["query"],
                "tags": tags,
                "history": history,
                "min_price_url": latest_min_price_url(history),
            }
        )
    return dataset


def main() -> None:
    dataset = build_dataset()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    html = HTML_TEMPLATE.replace("__DATA__", json.dumps(dataset))
    OUTPUT_PATH.write_text(html)
    print(f"Wrote {OUTPUT_PATH} with {len(dataset)} searches")


if __name__ == "__main__":
    main()
