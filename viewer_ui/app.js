const saved = JSON.parse(localStorage.getItem("zed-dataset-viewer") || "{}");
const state = {
  mode: saved.mode || "grid",
  modality: saved.modality || "left",
  offset: 0,
  pageSize: saved.pageSize || null,
  groups: new Set(),
  sites: new Set(),
  metadataReady: false,
  search: "",
  sort: saved.sort || "sample_order",
  total: 0,
  items: [],
  depth: {
    min: saved.depth?.min ?? 0.5,
    max: saved.depth?.max ?? 10,
    gamma: saved.depth?.gamma ?? 1,
    colormap: saved.depth?.colormap || "turbo",
    invert: saved.depth?.invert ?? true,
    auto: saved.depth?.auto ?? false
  },
  request: 0
};

const elements = {
  datasetName: document.querySelector("#dataset-name"),
  summary: document.querySelector("#summary"),
  groupOptions: document.querySelector("#group-options"),
  presetOptions: document.querySelector("#preset-options"),
  siteOptions: document.querySelector("#site-options"),
  siteSummary: document.querySelector("#site-summary"),
  search: document.querySelector("#search"),
  sort: document.querySelector("#sort"),
  pageSize: document.querySelector("#page-size"),
  depthControls: document.querySelector("#depth-controls"),
  grid: document.querySelector("#grid"),
  focus: document.querySelector("#focus"),
  focusImage: document.querySelector("#focus-image"),
  error: document.querySelector("#error"),
  page: document.querySelector("#page"),
  previous: document.querySelector("#previous"),
  next: document.querySelector("#next")
};

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

function persist() {
  localStorage.setItem("zed-dataset-viewer", JSON.stringify({
    mode: state.mode,
    modality: state.modality,
    pageSize: state.pageSize,
    sort: state.sort,
    depth: state.depth
  }));
}

function depthQuery() {
  return new URLSearchParams({
    min: state.depth.min,
    max: state.depth.max,
    gamma: state.depth.gamma,
    colormap: state.depth.colormap,
    invert: state.depth.invert ? "1" : "0",
    auto: state.depth.auto ? "1" : "0"
  }).toString();
}

function imageUrl(item, thumbnail = false) {
  const sources = thumbnail ? item.thumbnails : item.assets;
  const source = sources[state.modality];
  if (!source) return "";
  return state.modality === "depth" ? `${source}?${depthQuery()}` : source;
}

function queryString() {
  const query = new URLSearchParams({
    offset: state.offset,
    search: state.search,
    sort: state.sort
  });
  if (state.mode === "focus") query.set("limit", 2);
  else if (state.pageSize) query.set("limit", state.pageSize);
  if (state.metadataReady) {
    query.set("groups", state.groups.size ? [...state.groups].join(",") : "__none__");
    query.set("sites", state.sites.size ? [...state.sites].join(",") : "__none__");
  }
  return query.toString();
}

async function load() {
  const request = ++state.request;
  elements.error.classList.add("hidden");
  try {
    const response = await fetch(`/api/state?${queryString()}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "データセットを読み込めません");
    if (request !== state.request) return;

    if (!state.pageSize) {
      state.pageSize = data.page_size;
      if (![...elements.pageSize.options].some(option => Number(option.value) === state.pageSize)) {
        elements.pageSize.add(new Option(String(state.pageSize), String(state.pageSize)));
      }
      elements.pageSize.value = String(state.pageSize);
    }
    if (!state.metadataReady) {
      state.groups = new Set(data.groups.map(group => group.id));
      state.sites = new Set(data.sites.map(site => site.id));
      state.metadataReady = true;
      renderFilters(data);
    }
    if (!data.items.length && state.offset > 0 && data.filtered_total > 0) {
      state.offset = state.mode === "focus"
        ? data.filtered_total - 1
        : Math.floor((data.filtered_total - 1) / state.pageSize) * state.pageSize;
      return load();
    }
    state.items = data.items;
    state.total = data.filtered_total;
    render(data);
  } catch (error) {
    elements.error.textContent = error.message;
    elements.error.classList.remove("hidden");
  }
}

function renderFilters(data) {
  elements.groupOptions.innerHTML = data.groups.map(group => `
    <label><input type="checkbox" value="${escapeHtml(group.id)}" checked>
      ${escapeHtml(group.label)} <small>${group.count}枚</small>
    </label>`).join("");
  elements.groupOptions.querySelectorAll("input").forEach(input => {
    input.addEventListener("change", () => {
      if (input.checked) state.groups.add(input.value);
      else state.groups.delete(input.value);
      state.offset = 0;
      updatePresetButtons(data);
      load();
    });
  });

  elements.presetOptions.innerHTML = [
    ...data.presets.map(preset => `<button data-groups="${preset.groups.join(",")}">${preset.label}</button>`),
    `<button data-groups="${data.groups.map(group => group.id).join(",")}" class="active">すべて</button>`,
    `<button data-groups="">解除</button>`
  ].join("");
  elements.presetOptions.querySelectorAll("button").forEach(button => {
    button.addEventListener("click", () => {
      state.groups = new Set(button.dataset.groups.split(",").filter(Boolean));
      elements.groupOptions.querySelectorAll("input").forEach(input => {
        input.checked = state.groups.has(input.value);
      });
      state.offset = 0;
      updatePresetButtons(data);
      load();
    });
  });

  elements.siteOptions.innerHTML = data.sites.map(site => `
    <label><input type="checkbox" value="${escapeHtml(site.id)}" checked>
      ${escapeHtml(site.id)} <small>${escapeHtml(site.session_id)} · ${site.count}枚</small>
    </label>`).join("");
  elements.siteOptions.querySelectorAll("input").forEach(input => {
    input.addEventListener("change", () => {
      if (input.checked) state.sites.add(input.value);
      else state.sites.delete(input.value);
      state.offset = 0;
      updateSiteSummary(data);
      load();
    });
  });
  document.querySelector("#all-sites").addEventListener("click", () => selectSites(data, true));
  document.querySelector("#no-sites").addEventListener("click", () => selectSites(data, false));
  updateSiteSummary(data);
}

function updatePresetButtons(data) {
  const selected = [...state.groups].sort().join(",");
  elements.presetOptions.querySelectorAll("button").forEach(button => {
    button.classList.toggle("active", button.dataset.groups.split(",").filter(Boolean).sort().join(",") === selected);
  });
}

function selectSites(data, selected) {
  state.sites = new Set(selected ? data.sites.map(site => site.id) : []);
  elements.siteOptions.querySelectorAll("input").forEach(input => { input.checked = selected; });
  state.offset = 0;
  updateSiteSummary(data);
  load();
}

function updateSiteSummary(data) {
  elements.siteSummary.textContent = state.sites.size === data.sites.length
    ? "Site: すべて"
    : `Site: ${state.sites.size}/${data.sites.length}`;
}

function render(data) {
  elements.datasetName.textContent = data.dataset_name;
  const first = state.total ? state.offset + 1 : 0;
  const visible = state.mode === "focus" ? Math.min(1, state.items.length) : state.items.length;
  const last = Math.min(state.offset + visible, state.total);
  elements.summary.textContent = `${first}–${last} / ${state.total}枚（全${data.total}枚）`;
  elements.page.textContent = `${first}–${last} / ${state.total}`;
  elements.previous.disabled = state.offset <= 0;
  elements.next.disabled = state.offset + visible >= state.total;

  const focusMode = state.mode === "focus";
  elements.grid.classList.toggle("hidden", focusMode);
  elements.focus.classList.toggle("hidden", !focusMode);
  document.querySelector("#grid-mode").classList.toggle("active", !focusMode);
  document.querySelector("#focus-mode").classList.toggle("active", focusMode);
  document.querySelectorAll("[data-modality]").forEach(button => {
    button.classList.toggle("active", button.dataset.modality === state.modality);
  });
  elements.depthControls.classList.toggle("hidden", state.modality !== "depth");
  if (focusMode) renderFocus();
  else renderGrid();
}

function renderGrid() {
  if (!state.items.length) {
    elements.grid.innerHTML = `<p class="empty">条件に合う画像がありません。</p>`;
    return;
  }
  elements.grid.innerHTML = state.items.map((item, index) => {
    const source = imageUrl(item, true);
    return `<article class="card" data-index="${index}" tabindex="0">
      ${source
        ? `<img src="${escapeHtml(source)}" alt="${escapeHtml(item.image_id)} ${state.modality}" loading="lazy">`
        : `<div class="image-missing">${state.modality}なし</div>`}
      <div class="card-info">
        <code class="card-id">${escapeHtml(item.image_id)}</code>
        <div class="card-meta"><span>${escapeHtml(item.site_id)} · ${escapeHtml(item.increment_label)}</span><span>#${item.sample_order}</span></div>
      </div>
    </article>`;
  }).join("");
  elements.grid.querySelectorAll(".card").forEach(card => {
    const open = () => {
      state.offset += Number(card.dataset.index);
      setMode("focus");
    };
    card.addEventListener("click", open);
    card.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") open();
    });
  });
}

function renderFocus() {
  const item = state.items[0];
  if (!item) {
    document.querySelector("#focus-id").textContent = "条件に合う画像がありません";
    elements.focusImage.removeAttribute("src");
    document.querySelector("#focus-metadata").innerHTML = "";
    return;
  }
  document.querySelector("#focus-id").textContent = item.image_id;
  document.querySelector("#focus-site").textContent = `${item.site_id} · ${item.increment_label}`;
  document.querySelector("#focus-position").textContent = `${state.offset + 1} / ${state.total}`;
  const source = imageUrl(item, false);
  if (source) elements.focusImage.src = source;
  else elements.focusImage.removeAttribute("src");
  elements.focusImage.alt = `${item.image_id} ${state.modality}`;
  document.querySelector("#focus-metadata").innerHTML = [
    ["Site", item.site_id], ["Session", item.session_id],
    ["Group", item.increment_label], ["Frame", item.actual_frame_index],
    ["Sample", `#${item.sample_order}`]
  ].map(([key, value]) => `<div><dt>${key}</dt><dd>${escapeHtml(value)}</dd></div>`).join("");

  const next = state.items[1];
  if (next) {
    const preloadSource = imageUrl(next, false);
    if (preloadSource) new Image().src = preloadSource;
  }
}

function setMode(mode) {
  state.mode = mode;
  persist();
  load();
}

function setModality(modality) {
  state.modality = modality;
  persist();
  renderDepthControls();
  load();
}

function move(direction) {
  const step = state.mode === "focus" ? 1 : state.pageSize;
  const next = Math.max(0, state.offset + direction * step);
  if (next < state.total) {
    state.offset = next;
    load();
  }
}

function renderDepthControls() {
  document.querySelector("#depth-min").value = state.depth.min;
  document.querySelector("#depth-max").value = state.depth.max;
  document.querySelector("#gamma").value = state.depth.gamma;
  document.querySelector("#gamma-value").textContent = Number(state.depth.gamma).toFixed(1);
  document.querySelector("#colormap").value = state.depth.colormap;
  document.querySelector("#invert").checked = state.depth.invert;
  document.querySelector("#auto-range").checked = state.depth.auto;
  const gradients = {
    turbo: "#30123b, #4662d7, #35abf8, #1ae4b6, #a4fc3c, #f9ba38, #f66b19, #c92d35, #7a0403",
    viridis: "#440154, #3b528b, #21918c, #5ec962, #fde725",
    magma: "#000004, #51127c, #b73779, #fc8961, #fcfdbf",
    grayscale: "#000, #fff"
  };
  const direction = state.depth.invert ? "to left" : "to right";
  document.querySelector("#colorbar").style.background = `linear-gradient(${direction}, ${gradients[state.depth.colormap]})`;
  document.querySelector("#legend-near").textContent = state.depth.auto ? "p2" : `${state.depth.min}m`;
  document.querySelector("#legend-far").textContent = state.depth.auto ? "p98" : `${state.depth.max}m`;
  document.querySelectorAll("[data-depth-range]").forEach(button => {
    const [minimum, maximum] = button.dataset.depthRange.split(",").map(Number);
    button.classList.toggle("active", !state.depth.auto && minimum === Number(state.depth.min) && maximum === Number(state.depth.max));
  });
}

let depthTimer;
function depthChanged(immediate = false) {
  state.depth.min = Number(document.querySelector("#depth-min").value);
  state.depth.max = Number(document.querySelector("#depth-max").value);
  state.depth.gamma = Number(document.querySelector("#gamma").value);
  state.depth.colormap = document.querySelector("#colormap").value;
  state.depth.invert = document.querySelector("#invert").checked;
  state.depth.auto = document.querySelector("#auto-range").checked;
  persist();
  renderDepthControls();
  clearTimeout(depthTimer);
  depthTimer = setTimeout(load, immediate ? 0 : 250);
}

document.querySelector("#grid-mode").addEventListener("click", () => {
  if (state.mode !== "grid") {
    state.offset = Math.floor(state.offset / state.pageSize) * state.pageSize;
    setMode("grid");
  }
});
document.querySelector("#focus-mode").addEventListener("click", () => setMode("focus"));
document.querySelectorAll("[data-modality]").forEach(button => {
  button.addEventListener("click", () => setModality(button.dataset.modality));
});
elements.previous.addEventListener("click", () => move(-1));
elements.next.addEventListener("click", () => move(1));
elements.search.addEventListener("input", () => {
  state.search = elements.search.value;
  state.offset = 0;
  clearTimeout(elements.search.timer);
  elements.search.timer = setTimeout(load, 200);
});
elements.sort.value = state.sort;
elements.sort.addEventListener("change", () => {
  state.sort = elements.sort.value;
  state.offset = 0;
  persist();
  load();
});
if (state.pageSize) elements.pageSize.value = String(state.pageSize);
elements.pageSize.addEventListener("change", () => {
  state.pageSize = Number(elements.pageSize.value);
  state.offset = 0;
  persist();
  load();
});

["#depth-min", "#depth-max"].forEach(selector => {
  document.querySelector(selector).addEventListener("change", () => depthChanged(true));
});
document.querySelector("#gamma").addEventListener("input", () => depthChanged(false));
["#colormap", "#invert", "#auto-range"].forEach(selector => {
  document.querySelector(selector).addEventListener("change", () => depthChanged(true));
});
document.querySelectorAll("[data-depth-range]").forEach(button => {
  button.addEventListener("click", () => {
    const [minimum, maximum] = button.dataset.depthRange.split(",").map(Number);
    document.querySelector("#depth-min").value = minimum;
    document.querySelector("#depth-max").value = maximum;
    document.querySelector("#auto-range").checked = false;
    depthChanged(true);
  });
});

document.addEventListener("keydown", event => {
  if (["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
  const key = event.key.toLowerCase();
  if (key === "l") setModality("left");
  else if (key === "r") setModality("right");
  else if (key === "d") setModality("depth");
  else if (key === "g") {
    state.offset = Math.floor(state.offset / state.pageSize) * state.pageSize;
    setMode("grid");
  } else if (event.key === "ArrowLeft") move(-1);
  else if (event.key === "ArrowRight") move(1);
});

renderDepthControls();
load();
