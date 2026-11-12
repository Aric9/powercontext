const state = { data: null, active: 0 };

const input = document.getElementById("demo-input");
const chips = document.getElementById("demo-chips");
const list = document.getElementById("demo-results");

function escapeHtml(text) {
  return String(text)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

function terms(query) {
  return query
    .split(/[\s，。、；：！？,.;:!?]+/)
    .map((t) => t.trim())
    .filter((t) => t.length >= 2);
}

function highlight(text, query) {
  let html = escapeHtml(text);
  for (const term of terms(query).sort((a, b) => b.length - a.length)) {
    const safe = term.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    html = html.replace(new RegExp(safe, "gi"), (m) => `<mark>${m}</mark>`);
  }
  return html;
}

function render(items, query) {
  if (!items.length) {
    list.innerHTML = `<li><p>没有匹配的演示结果，试试“RRF”“分块”或“行号”。</p></li>`;
    return;
  }
  list.innerHTML = items
    .slice(0, 6)
    .map(
      (item) => `
      <li>
        <h4>${escapeHtml(item.title)} · ${escapeHtml(item.section)}</h4>
        <button class="cite" data-cite="${escapeHtml(item.citation)}" title="点击复制引用">${escapeHtml(item.citation)}</button>
        <p>${highlight(item.snippet, query)}</p>
        <p class="meta">score ${Number(item.score).toFixed(3)} · 命中 ${escapeHtml((item.matched_by || []).join(" / ") || "dense")}</p>
      </li>`
    )
    .join("");
}

function show(index) {
  if (!state.data) return;
  state.active = index;
  const entry = state.data.queries[index];
  input.value = entry.q;
  chips.querySelectorAll("button").forEach((btn, i) => btn.classList.toggle("on", i === index));
  render(entry.results, entry.q);
}

function search(text) {
  if (!state.data) return;
  const query = text.trim();
  if (!query) {
    show(state.active);
    return;
  }
  const all = state.data.queries.flatMap((entry) => entry.results);
  const seen = new Set();
  const hits = all.filter((item) => {
    const haystack = `${item.title} ${item.section} ${item.snippet}`.toLowerCase();
    const ok = terms(query).some((term) => haystack.includes(term.toLowerCase())) || haystack.includes(query.toLowerCase());
    if (!ok || seen.has(item.citation)) return false;
    seen.add(item.citation);
    return true;
  });
  render(hits, query);
}

async function boot() {
  try {
    const response = await fetch("assets/demo.json");
    state.data = await response.json();
    chips.innerHTML = state.data.queries
      .map((entry, index) => `<button type="button" data-index="${index}">${escapeHtml(entry.q)}</button>`)
      .join("");
    chips.addEventListener("click", (event) => {
      const button = event.target.closest("button");
      if (button) show(Number(button.dataset.index));
    });
    list.addEventListener("click", async (event) => {
      const button = event.target.closest(".cite");
      if (!button) return;
      try {
        await navigator.clipboard.writeText(button.dataset.cite);
        const old = button.textContent;
        button.textContent = "已复制 ✓";
        setTimeout(() => (button.textContent = old), 1200);
      } catch (error) {
        /* clipboard may be unavailable on http; citation stays selectable */
      }
    });
    let timer = null;
    input.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(() => search(input.value), 120);
    });
    show(0);
  } catch (error) {
    list.innerHTML = `<li><p>演示数据加载失败：${escapeHtml(error.message)}</p></li>`;
  }
}

boot();
