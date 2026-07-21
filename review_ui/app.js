const state = {
  mode: localStorage.getItem("zed-review-mode") || "focus",
  offset: 0,
  pageSize: null,
  filter: "unreviewed",
  session: "",
  total: 0,
  items: [],
  currentModality: "left",
  saving: false
};

const grid = document.querySelector("#grid");
const focusPanel = document.querySelector("#focus");
const summary = document.querySelector("#summary");
const page = document.querySelector("#page");
const reviewer = document.querySelector("#reviewer");
const filter = document.querySelector("#filter");
const sessionFilter = document.querySelector("#session-filter");
const viewer = document.querySelector("#viewer");
const focusImage = document.querySelector("#focus-image");
const focusReason = document.querySelector("#focus-reason");
const focusNote = document.querySelector("#focus-note");
const focusMessage = document.querySelector("#focus-message");

reviewer.value = localStorage.getItem("zed-reviewer") || "";
reviewer.addEventListener("change", () => localStorage.setItem("zed-reviewer", reviewer.value));

function escapeHtml(value) {
  return String(value).replace(/[&<>'"]/g, char => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;"
  })[char]);
}

async function load() {
  const limit = state.mode === "focus" ? 2 : (state.pageSize || 24);
  const query = new URLSearchParams({
    offset: state.offset,
    limit,
    decision: state.filter,
    session: state.session
  });
  const response = await fetch(`/api/state?${query}`);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "レビュー状態を取得できません");
  if (!data.items.length && state.offset > 0 && data.filtered_total > 0) {
    state.offset = state.mode === "focus"
      ? data.filtered_total - 1
      : Math.floor((data.filtered_total - 1) / (state.pageSize || 24)) * (state.pageSize || 24);
    return load();
  }
  state.items = data.items;
  state.total = data.filtered_total;
  state.pageSize = data.page_size;
  renderSessionFilter(data);
  render(data);
}

function renderSessionFilter(data) {
  sessionFilter.innerHTML = "<option value=''>すべて</option>";
  Object.keys(data.keep_by_session).forEach(session => {
    const option = document.createElement("option");
    option.value = session;
    option.textContent = `${data.site_by_session[session]} — ${session} (${data.keep_by_session[session]}/${data.target_per_session})`;
    option.selected = session === state.session;
    sessionFilter.appendChild(option);
  });
}

function render(data) {
  const counts = data.counts;
  summary.textContent = `各セッション目標 ${data.target_per_session} ｜ 達成 ${data.sessions_achieved}/${data.session_count}セッション ｜ 全体Keep ${counts.keep}/${data.target} ｜ Reject ${counts.reject} ｜ Hold ${counts.hold} ｜ 未レビュー ${counts.unreviewed}`;
  const first = state.total ? state.offset + 1 : 0;
  const visible = state.mode === "focus" ? Math.min(1, state.items.length) : state.items.length;
  const last = Math.min(state.offset + visible, state.total);
  page.textContent = `${first}–${last} / ${state.total}`;

  const focusMode = state.mode === "focus";
  focusPanel.classList.toggle("hidden", !focusMode);
  grid.classList.toggle("hidden", focusMode);
  document.querySelector("#focus-mode").classList.toggle("active", focusMode);
  document.querySelector("#grid-mode").classList.toggle("active", !focusMode);

  if (focusMode) renderFocus(data);
  else renderGrid(data);
}

function reasonOptions(selected, reasons) {
  const selectedReason = selected || "other";
  return reasons
    .map(reason => `<option value="${reason}" ${selectedReason === reason ? "selected" : ""}>${reason}</option>`)
    .join("");
}

function renderFocus(data) {
  const item = state.items[0];
  const controls = ["#focus-keep", "#focus-reject", "#focus-hold", "#focus-reason", "#focus-note"];
  controls.forEach(selector => { document.querySelector(selector).disabled = !item; });
  if (!item) {
    document.querySelector("#focus-id").textContent = "対象画像がありません";
    document.querySelector("#focus-position").textContent = "";
    document.querySelector("#focus-session").textContent = "";
    document.querySelector("#modality-switch").innerHTML = "";
    focusImage.removeAttribute("src");
    focusMessage.textContent = "表示条件を変更するか、レビューを終了してください。";
    return;
  }

  document.querySelector("#focus-id").textContent = item.image_id;
  document.querySelector("#focus-position").textContent = `候補 #${item.candidate_order} · ${state.offset + 1}/${state.total}`;
  document.querySelector("#focus-session").textContent = `${item.site_id} / ${item.session_id}`;
  focusReason.innerHTML = reasonOptions(item.reject_reason, data.reject_reasons);
  focusNote.value = item.note || "";
  focusMessage.textContent = item.decision || "未レビュー";

  const modalities = Object.keys(item.assets);
  if (!modalities.includes(state.currentModality)) state.currentModality = modalities[0];
  renderModalityButtons(item);
  showModality(item, state.currentModality);

  const next = state.items[1];
  if (next && next.assets.left) {
    const preload = new Image();
    preload.src = next.assets.left;
  }
}

function renderModalityButtons(item) {
  const labels = { left: "Left (L)", right: "Right (V)", depth_preview: "Depth (D)" };
  const container = document.querySelector("#modality-switch");
  container.innerHTML = Object.keys(item.assets)
    .map(name => `<button data-modality="${name}" class="${name === state.currentModality ? "active" : ""}">${labels[name] || name}</button>`)
    .join("");
  container.querySelectorAll("[data-modality]").forEach(button => {
    button.addEventListener("click", () => showModality(item, button.dataset.modality));
  });
}

function showModality(item, modality) {
  if (!item || !item.assets[modality]) return;
  state.currentModality = modality;
  focusImage.src = item.assets[modality];
  focusImage.alt = `${item.image_id} ${modality}`;
  document.querySelectorAll("#modality-switch [data-modality]").forEach(button => {
    button.classList.toggle("active", button.dataset.modality === modality);
  });
}

function renderGrid(data) {
  grid.innerHTML = state.items.map((item, index) => cardHtml(item, index, data.reject_reasons)).join("");
  attachCardEvents();
}

function cardHtml(item, index, reasons) {
  return `<article class="card ${escapeHtml(item.decision)}" data-index="${index}">
    <img class="preview" src="${item.thumbnail}" alt="${escapeHtml(item.image_id)}" loading="lazy">
    <div class="details">
      <div class="identity"><code>${escapeHtml(item.image_id)}</code><span>#${item.candidate_order}</span></div>
      <div class="decision-row">
        <button class="keep-button" data-decision="keep">Keep</button>
        <button class="reject-button" data-decision="reject">Reject</button>
        <button class="hold-button" data-decision="hold">Hold</button>
      </div>
      <select class="reason">${reasonOptions(item.reject_reason, reasons)}</select>
      <textarea class="note" placeholder="補足（任意）">${escapeHtml(item.note)}</textarea>
      <div class="message">${item.decision || "未レビュー"}</div>
    </div>
  </article>`;
}

function attachCardEvents() {
  document.querySelectorAll(".card").forEach(card => {
    const item = state.items[Number(card.dataset.index)];
    card.querySelector(".preview").addEventListener("click", () => openViewer(item));
    card.querySelectorAll("[data-decision]").forEach(button => {
      button.addEventListener("click", () => saveGrid(card, item, button.dataset.decision));
    });
  });
}

async function postReview(item, decision, rejectReason, note, message) {
  if (state.saving) return false;
  state.saving = true;
  message.textContent = "保存中…";
  try {
    const response = await fetch("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        image_id: item.image_id,
        decision,
        reject_reason: decision === "reject" ? (rejectReason || "other") : "",
        note,
        reviewer: reviewer.value
      })
    });
    const data = await response.json();
    if (!response.ok) {
      message.textContent = data.error || "保存に失敗しました";
      return false;
    }
    message.textContent = `${decision}として保存しました`;
    return true;
  } catch (error) {
    message.textContent = `保存に失敗しました: ${error}`;
    return false;
  } finally {
    state.saving = false;
  }
}

async function saveGrid(card, item, decision) {
  const saved = await postReview(
    item,
    decision,
    card.querySelector(".reason").value,
    card.querySelector(".note").value,
    card.querySelector(".message")
  );
  if (saved) await load();
}

async function saveFocus(decision) {
  const item = state.items[0];
  if (!item) return;
  const saved = await postReview(
    item, decision, focusReason.value, focusNote.value, focusMessage
  );
  if (!saved) return;
  const remainsInFilter = state.filter === "" || state.filter === decision;
  if (remainsInFilter) state.offset += 1;
  await load();
}

function openViewer(item) {
  document.querySelector("#viewer-title").textContent = item.image_id;
  document.querySelector("#viewer-images").innerHTML = Object.entries(item.assets)
    .map(([name, source]) => `<figure><figcaption>${escapeHtml(name)}</figcaption><img src="${source}" alt="${escapeHtml(name)}"></figure>`)
    .join("");
  viewer.showModal();
}

function setMode(mode) {
  state.mode = mode;
  state.offset = 0;
  localStorage.setItem("zed-review-mode", mode);
  load();
}

function move(direction) {
  const step = state.mode === "focus" ? 1 : (state.pageSize || 24);
  const nextOffset = Math.max(0, state.offset + direction * step);
  if (nextOffset < state.total) {
    state.offset = nextOffset;
    load();
  }
}

document.querySelector("#focus-mode").addEventListener("click", () => setMode("focus"));
document.querySelector("#grid-mode").addEventListener("click", () => setMode("grid"));
document.querySelector("#focus-keep").addEventListener("click", () => saveFocus("keep"));
document.querySelector("#focus-reject").addEventListener("click", () => saveFocus("reject"));
document.querySelector("#focus-hold").addEventListener("click", () => saveFocus("hold"));
document.querySelector("#close-viewer").addEventListener("click", () => viewer.close());

filter.addEventListener("change", () => {
  state.filter = filter.value;
  state.offset = 0;
  load();
});
sessionFilter.addEventListener("change", () => {
  state.session = sessionFilter.value;
  state.offset = 0;
  load();
});
document.querySelector("#previous").addEventListener("click", () => move(-1));
document.querySelector("#next").addEventListener("click", () => move(1));

document.addEventListener("keydown", event => {
  if (state.mode !== "focus" || viewer.open || state.saving) return;
  const tag = event.target.tagName;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(tag)) return;
  const key = event.key.toLowerCase();
  const item = state.items[0];
  if (!item) return;
  if (key === "k") saveFocus("keep");
  else if (key === "r") saveFocus("reject");
  else if (key === "h") saveFocus("hold");
  else if (key === "l") showModality(item, "left");
  else if (key === "v") showModality(item, "right");
  else if (key === "d") showModality(item, "depth_preview");
  else if (event.key === "ArrowLeft") move(-1);
  else if (event.key === "ArrowRight") move(1);
  else return;
  event.preventDefault();
});

load().catch(error => { summary.textContent = `読み込みエラー: ${error}`; });
