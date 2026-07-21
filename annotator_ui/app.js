"use strict";

const app = {
  groups: [],
  presets: [],
  sites: [],
  selectedSites: new Set(),
  offset: 0,
  total: 0,
  item: null,
  nextItem: null,
  image: null,
  canvasImages: {},
  canvasModality: localStorage.getItem("zed-annotator-canvas-modality") === "depth"
    ? "depth"
    : "left",
  backgroundToken: 0,
  loadToken: 0,
  saving: false,

  mode: "line",
  curveSide: "left",
  farEndMode: "image_boundary",
  leftPoints: [],
  rightPoints: [],
  endPoints: [],
  loadedPolygon: null,
  dragging: null,

  canvas: document.querySelector("#annotation-canvas"),
  ctx: null,

  async init() {
    this.ctx = this.canvas.getContext("2d");
    this.bindControls();
    this.bindCanvas();
    await this.loadState(true);
  },

  selectedGroups() {
    const value = document.querySelector("#scope-value").value;
    if (!value) return [];
    if (document.querySelector("#scope-mode").value === "exact") return [value];
    const preset = this.presets.find(item => item.label === value);
    return preset ? preset.groups : [];
  },

  queryString() {
    const query = new URLSearchParams({
      offset: String(this.offset),
      sort: document.querySelector("#sort").value,
      search: document.querySelector("#search").value,
    });
    this.selectedGroups().forEach(group => query.append("groups", group));
    this.selectedSites.forEach(site => query.append("sites", site));
    const status = document.querySelector("#status-filter").value;
    if (status) query.append("statuses", status);
    return query;
  },

  async loadState(firstLoad = false) {
    this.showError("");
    try {
      const response = await fetch(`/api/state?${this.queryString()}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "状態を取得できません");

      if (firstLoad) {
        this.groups = data.groups;
        this.presets = data.presets;
        this.sites = data.sites;
        this.renderScopeOptions();
        this.renderSites();
        this.restoreFilters();
        if (this.selectedGroups().length) return this.loadState(false);
      }

      if (!data.items.length && this.offset > 0 && data.filtered_total > 0) {
        this.offset = data.filtered_total - 1;
        return this.loadState(false);
      }
      this.total = data.filtered_total;
      this.item = data.items[0] || null;
      this.nextItem = data.items[1] || null;
      this.renderHeader(data);
      await this.loadCurrent();
    } catch (error) {
      this.showError(String(error));
    }
  },

  renderHeader(data) {
    document.querySelector("#dataset-name").textContent = data.dataset_name;
    const done = data.counts.positive + data.counts.empty;
    document.querySelector("#summary").textContent =
      `対象 ${data.scope_total}枚 ｜ 完了 ${done} ｜ ラベルあり ${data.counts.positive} ｜ 対象なし ${data.counts.empty} ｜ 未処理 ${data.counts.unannotated}`;
    document.querySelector("#readiness").innerHTML = data.readiness.map(item =>
      `<span class="ready-chip ${item.ready ? "ready" : ""}">${item.label}: ${item.completed}/${item.total}${item.ready ? " ✓" : ""}</span>`
    ).join("");
  },

  renderScopeOptions() {
    const select = document.querySelector("#scope-value");
    const mode = document.querySelector("#scope-mode").value;
    const previous = select.value;
    if (mode === "exact") {
      select.innerHTML = this.groups.map(group =>
        `<option value="${group.id}">${group.label}（${group.count}枚）</option>`
      ).join("");
    } else {
      select.innerHTML = this.presets.map(preset => {
        const count = this.groups
          .filter(group => preset.groups.includes(group.id))
          .reduce((sum, group) => sum + group.count, 0);
        return `<option value="${preset.label}">${preset.label}（${count}枚）</option>`;
      }).join("");
    }
    if ([...select.options].some(option => option.value === previous)) select.value = previous;
  },

  renderSites() {
    document.querySelector("#site-options").innerHTML = this.sites.map(site =>
      `<label><input type="checkbox" value="${site.id}"> ${site.id}<small>${site.session_id}</small></label>`
    ).join("");
    document.querySelectorAll("#site-options input").forEach(input => {
      input.addEventListener("change", () => {
        if (input.checked) this.selectedSites.add(input.value);
        else this.selectedSites.delete(input.value);
        this.updateSiteSummary();
        this.offset = 0;
        this.persistFilters();
        this.loadState();
      });
    });
    this.updateSiteSummary();
  },

  updateSiteSummary() {
    const count = this.selectedSites.size;
    document.querySelector("#site-summary").textContent = count
      ? `Site: ${count}件選択`
      : "Site: すべて";
  },

  persistFilters() {
    const value = {
      scopeMode: document.querySelector("#scope-mode").value,
      scopeValue: document.querySelector("#scope-value").value,
      sites: [...this.selectedSites],
      status: document.querySelector("#status-filter").value,
      sort: document.querySelector("#sort").value,
    };
    localStorage.setItem("zed-annotator-filters", JSON.stringify(value));
  },

  restoreFilters() {
    let saved = null;
    try { saved = JSON.parse(localStorage.getItem("zed-annotator-filters")); } catch (_) { /* ignore */ }
    if (!saved) return;
    if (["exact", "cumulative"].includes(saved.scopeMode)) {
      document.querySelector("#scope-mode").value = saved.scopeMode;
      this.renderScopeOptions();
    }
    const scope = document.querySelector("#scope-value");
    if ([...scope.options].some(option => option.value === saved.scopeValue)) scope.value = saved.scopeValue;
    const validSites = new Set(this.sites.map(site => site.id));
    this.selectedSites = new Set((saved.sites || []).filter(site => validSites.has(site)));
    document.querySelectorAll("#site-options input").forEach(input => {
      input.checked = this.selectedSites.has(input.value);
    });
    if (["", "unannotated", "positive", "empty"].includes(saved.status)) {
      document.querySelector("#status-filter").value = saved.status;
    }
    if (["site_frame", "sample_order", "image_id"].includes(saved.sort)) {
      document.querySelector("#sort").value = saved.sort;
    }
    this.updateSiteSummary();
  },

  async loadCurrent() {
    const workspace = document.querySelector("#workspace");
    const empty = document.querySelector("#empty-state");
    if (!this.item) {
      workspace.classList.add("hidden");
      empty.classList.remove("hidden");
      this.setActionDisabled(true);
      return;
    }
    workspace.classList.remove("hidden");
    empty.classList.add("hidden");
    this.setActionDisabled(false);
    const token = ++this.loadToken;
    this.resetDrawing();
    document.querySelector("#image-id").textContent = this.item.image_id;
    document.querySelector("#image-meta").textContent =
      `${this.item.site_id} / ${this.item.increment_label} / frame ${this.item.actual_frame_index}`;
    document.querySelector("#position").textContent = `${this.offset + 1} / ${this.total}`;
    this.setStatus(this.item.status);
    document.querySelector("#save-message").textContent = "読み込み中…";

    try {
      if (!this.item.assets[this.canvasModality]) this.canvasModality = "left";
      this.syncCanvasModalityButtons();
      const initialModality = this.canvasModality;
      const imagePromise = this.loadImage(this.item.assets[initialModality]);
      const annotationPromise = fetch(`/api/annotation/${encodeURIComponent(this.item.image_id)}`)
        .then(async response => {
          const data = await response.json();
          if (!response.ok) throw new Error(data.error || "ラベルを読み込めません");
          return data;
        });
      const [image, annotation] = await Promise.all([imagePromise, annotationPromise]);
      if (token !== this.loadToken) return;
      this.canvasImages[initialModality] = image;
      if (this.canvasModality === initialModality || !this.image) this.image = image;
      this.applyAnnotation(annotation);
      this.resizeCanvas();
      this.draw();
      this.renderReferenceButtons();
      document.querySelector("#save-message").textContent = "";
      const preloadModality = this.nextItem?.assets?.[this.canvasModality]
        ? this.canvasModality
        : "left";
      if (this.nextItem?.assets?.[preloadModality]) {
        const preload = new Image();
        preload.src = this.nextItem.assets[preloadModality];
      }
    } catch (error) {
      if (token === this.loadToken) this.showError(String(error));
    }
  },

  loadImage(source) {
    return new Promise((resolve, reject) => {
      if (!source) return reject(new Error("表示画像がありません"));
      const image = new Image();
      image.onload = () => resolve(image);
      image.onerror = () => reject(new Error("画像を読み込めません"));
      image.src = source;
    });
  },

  applyAnnotation(annotation) {
    this.setStatus(annotation.status);
    if (annotation.edit) {
      const edit = annotation.edit;
      this.mode = edit.mode === "curve" ? "curve" : "line";
      this.leftPoints = (edit.left_points || []).map(point => point.slice());
      this.rightPoints = (edit.right_points || []).map(point => point.slice());
      this.farEndMode = edit.far_end?.mode === "line" ? "line" : "image_boundary";
      this.endPoints = (edit.far_end?.points || []).map(point => point.slice());
      this.curveSide = "done";
    } else if (annotation.polygon?.length >= 3) {
      this.loadedPolygon = annotation.polygon;
    }
    this.syncDrawingControls();
  },

  resetDrawing() {
    this.mode = "line";
    this.curveSide = "left";
    this.farEndMode = "image_boundary";
    this.leftPoints = [];
    this.rightPoints = [];
    this.endPoints = [];
    this.loadedPolygon = null;
    this.dragging = null;
    this.image = null;
    this.canvasImages = {};
    this.backgroundToken += 1;
    this.syncDrawingControls();
  },

  syncCanvasModalityButtons() {
    const hasDepth = Boolean(this.item?.assets?.depth);
    document.querySelector("#canvas-left").classList.toggle("active", this.canvasModality === "left");
    document.querySelector("#canvas-depth").classList.toggle("active", this.canvasModality === "depth");
    document.querySelector("#canvas-depth").disabled = !hasDepth;
  },

  async setCanvasModality(modality) {
    if (!this.item || !["left", "depth"].includes(modality) || !this.item.assets[modality]) return;
    this.canvasModality = modality;
    localStorage.setItem("zed-annotator-canvas-modality", modality);
    this.syncCanvasModalityButtons();
    if (this.canvasImages[modality]) {
      this.image = this.canvasImages[modality];
      this.resizeCanvas();
      this.draw();
      return;
    }
    const token = ++this.backgroundToken;
    const imageId = this.item.image_id;
    document.querySelector("#save-message").textContent = `${modality === "depth" ? "Depth" : "Left"}を読み込み中…`;
    try {
      const image = await this.loadImage(this.item.assets[modality]);
      if (token !== this.backgroundToken || imageId !== this.item?.image_id) return;
      this.canvasImages[modality] = image;
      this.image = image;
      this.resizeCanvas();
      this.draw();
      document.querySelector("#save-message").textContent = "";
    } catch (error) {
      if (token === this.backgroundToken) this.showError(String(error));
    }
  },

  setStatus(status) {
    const labels = { unannotated: "未処理", positive: "ラベルあり", empty: "対象なし" };
    const element = document.querySelector("#annotation-status");
    element.textContent = labels[status] || status;
    element.dataset.status = status;
  },

  setActionDisabled(disabled) {
    ["previous", "undo", "clear", "delete", "empty", "save", "next"].forEach(id => {
      document.querySelector(`#${id}`).disabled = disabled;
    });
  },

  syncDrawingControls() {
    document.querySelector("#mode-line").classList.toggle("active", this.mode === "line");
    document.querySelector("#mode-curve").classList.toggle("active", this.mode === "curve");
    document.querySelector("#far-end-mode").value = this.farEndMode;
    const confirm = document.querySelector("#confirm-side");
    if (this.mode !== "curve" || this.curveSide === "done") {
      confirm.classList.add("hidden");
    } else {
      confirm.classList.remove("hidden");
      if (this.curveSide === "left") {
        confirm.textContent = "左境界を確定 → 右境界";
        confirm.disabled = this.leftPoints.length < 2;
      } else {
        confirm.textContent = this.farEndMode === "line"
          ? "右境界を確定 → 終端2点"
          : "右境界を確定";
        confirm.disabled = this.rightPoints.length < 2;
      }
    }
    document.querySelector("#step-message").textContent = this.stepMessage();
  },

  stepMessage() {
    if (this.loadedPolygon && !this.leftPoints.length) return "既存ポリゴンを表示中です。編集する場合はClearして描き直してください。";
    if (this.mode === "curve") {
      if (this.curveSide === "left") return `左境界を上から下へ入力（${this.leftPoints.length}点）`;
      if (this.curveSide === "right") return `右境界を上から下へ入力（${this.rightPoints.length}点）`;
      if (this.farEndMode === "line" && this.endPoints.length < 2) return `中畦終端を2点で指定（${this.endPoints.length}/2）`;
      return "入力完了。点をドラッグして調整し、保存してください。";
    }
    if (this.leftPoints.length < 2) return `左境界を2点で指定（${this.leftPoints.length}/2）`;
    if (this.rightPoints.length < 2) return `右境界を2点で指定（${this.rightPoints.length}/2）`;
    if (this.farEndMode === "line" && this.endPoints.length < 2) return `中畦終端を2点で指定（${this.endPoints.length}/2）。傾斜可能です。`;
    return "入力完了。点をドラッグして調整し、保存してください。";
  },

  setMode(mode) {
    if (mode === this.mode) return;
    if (this.leftPoints.length || this.rightPoints.length || this.endPoints.length || this.loadedPolygon) {
      if (!confirm("現在の入力を消してモードを切り替えますか？")) return;
    }
    this.mode = mode;
    this.leftPoints = [];
    this.rightPoints = [];
    this.endPoints = [];
    this.loadedPolygon = null;
    this.curveSide = "left";
    this.syncDrawingControls();
    this.draw();
  },

  setFarEndMode(mode) {
    this.farEndMode = mode;
    this.endPoints = [];
    if (this.mode === "curve" && this.curveSide === "done" && this.rightPoints.length < 2) {
      this.curveSide = "right";
    }
    this.syncDrawingControls();
    this.draw();
  },

  confirmCurveSide() {
    if (this.mode !== "curve") return;
    if (this.curveSide === "left" && this.leftPoints.length >= 2) this.curveSide = "right";
    else if (this.curveSide === "right" && this.rightPoints.length >= 2) this.curveSide = "done";
    this.syncDrawingControls();
    this.draw();
  },

  bindControls() {
    document.querySelector("#scope-mode").addEventListener("change", () => {
      this.renderScopeOptions(); this.offset = 0; this.persistFilters(); this.loadState();
    });
    ["scope-value", "status-filter", "sort"].forEach(id => {
      document.querySelector(`#${id}`).addEventListener("change", () => {
        this.offset = 0; this.persistFilters(); this.loadState();
      });
    });
    let searchTimer = null;
    document.querySelector("#search").addEventListener("input", () => {
      clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { this.offset = 0; this.loadState(); }, 250);
    });
    document.querySelector("#all-sites").addEventListener("click", () => {
      this.selectedSites.clear();
      document.querySelectorAll("#site-options input").forEach(input => { input.checked = false; });
      this.updateSiteSummary(); this.offset = 0; this.persistFilters(); this.loadState();
    });
    document.querySelector("#mode-line").addEventListener("click", () => this.setMode("line"));
    document.querySelector("#mode-curve").addEventListener("click", () => this.setMode("curve"));
    document.querySelector("#canvas-left").addEventListener("click", () => this.setCanvasModality("left"));
    document.querySelector("#canvas-depth").addEventListener("click", () => this.setCanvasModality("depth"));
    document.querySelector("#far-end-mode").addEventListener("change", event => this.setFarEndMode(event.target.value));
    document.querySelector("#confirm-side").addEventListener("click", () => this.confirmCurveSide());
    document.querySelector("#previous").addEventListener("click", () => this.previous());
    document.querySelector("#next").addEventListener("click", () => this.next());
    document.querySelector("#undo").addEventListener("click", () => this.undo());
    document.querySelector("#clear").addEventListener("click", () => this.clear());
    document.querySelector("#save").addEventListener("click", () => this.save());
    document.querySelector("#empty").addEventListener("click", () => this.saveEmpty());
    document.querySelector("#delete").addEventListener("click", () => this.deleteAnnotation());
    window.addEventListener("resize", () => { if (this.image) { this.resizeCanvas(); this.draw(); } });
    document.addEventListener("keydown", event => {
      if (["INPUT", "SELECT", "TEXTAREA"].includes(event.target.tagName)) return;
      if (event.key === "ArrowLeft") { event.preventDefault(); this.previous(); }
      else if (event.key === "ArrowRight") { event.preventDefault(); this.next(); }
      else if (!event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "l") { event.preventDefault(); this.setCanvasModality("left"); }
      else if (!event.ctrlKey && !event.metaKey && event.key.toLowerCase() === "d") { event.preventDefault(); this.setCanvasModality("depth"); }
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") { event.preventDefault(); this.save(); }
      else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "z") { event.preventDefault(); this.undo(); }
    });
  },

  bindCanvas() {
    const coordinate = event => {
      const rect = this.canvas.getBoundingClientRect();
      return [
        Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
        Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height)),
      ];
    };
    this.canvas.addEventListener("pointerdown", event => {
      if (!this.image) return;
      event.preventDefault();
      const [x, y] = coordinate(event);
      const hit = this.findNearPoint(x, y, 24);
      if (hit) {
        this.dragging = hit;
        this.canvas.setPointerCapture(event.pointerId);
        return;
      }
      this.loadedPolygon = null;
      if (this.mode === "line") {
        if (this.leftPoints.length < 2) this.leftPoints.push([x, y]);
        else if (this.rightPoints.length < 2) this.rightPoints.push([x, y]);
        else if (this.farEndMode === "line" && this.endPoints.length < 2) this.endPoints.push([x, y]);
      } else if (this.curveSide === "left") this.leftPoints.push([x, y]);
      else if (this.curveSide === "right") this.rightPoints.push([x, y]);
      else if (this.farEndMode === "line" && this.endPoints.length < 2) this.endPoints.push([x, y]);
      this.syncDrawingControls();
      this.draw();
    });
    this.canvas.addEventListener("pointermove", event => {
      if (!this.dragging) return;
      event.preventDefault();
      const [x, y] = coordinate(event);
      const collection = this[this.dragging.collection];
      collection[this.dragging.index] = [x, y];
      this.draw();
    });
    const finish = () => { this.dragging = null; this.syncDrawingControls(); };
    this.canvas.addEventListener("pointerup", finish);
    this.canvas.addEventListener("pointercancel", finish);
  },

  findNearPoint(x, y, threshold) {
    const width = this.canvas.width, height = this.canvas.height;
    let best = null, distance = Infinity;
    [["leftPoints", this.leftPoints], ["rightPoints", this.rightPoints], ["endPoints", this.endPoints]].forEach(([name, points]) => {
      points.forEach((point, index) => {
        const value = ((point[0] - x) * width) ** 2 + ((point[1] - y) * height) ** 2;
        if (value < threshold ** 2 && value < distance) {
          best = { collection: name, index };
          distance = value;
        }
      });
    });
    return best;
  },

  resizeCanvas() {
    const wrap = document.querySelector("#canvas-wrap");
    const maxWidth = Math.max(320, wrap.clientWidth - 2);
    const maxHeight = Math.max(220, window.innerHeight - 305);
    const aspect = this.image.naturalWidth / this.image.naturalHeight;
    let width = maxWidth;
    let height = width / aspect;
    if (height > maxHeight) { height = maxHeight; width = height * aspect; }
    this.canvas.width = Math.round(width);
    this.canvas.height = Math.round(height);
  },

  draw() {
    if (!this.ctx || !this.image) return;
    const ctx = this.ctx, width = this.canvas.width, height = this.canvas.height;
    ctx.clearRect(0, 0, width, height);
    ctx.drawImage(this.image, 0, 0, width, height);
    if (this.loadedPolygon && !this.leftPoints.length && !this.rightPoints.length) {
      this.drawPolygon(this.loadedPolygon);
      return;
    }
    const polygon = this.computePolygon();
    if (polygon) this.drawPolygon(polygon);

    if (this.mode === "line") {
      if (this.leftPoints.length === 2) this.drawExtendedLine(this.leftPoints[0], this.leftPoints[1], "#00e5ff");
      if (this.rightPoints.length === 2) this.drawExtendedLine(this.rightPoints[0], this.rightPoints[1], "#ffa500");
    } else {
      if (this.leftPoints.length >= 2) this.drawPolyline(this.extendSplineToBounds(this.leftPoints), "#00e5ff");
      if (this.rightPoints.length >= 2) this.drawPolyline(this.extendSplineToBounds(this.rightPoints), "#ffa500");
    }
    if (this.endPoints.length === 2) this.drawExtendedLine(this.endPoints[0], this.endPoints[1], "#c77dff", [8, 5]);
    this.leftPoints.forEach((point, index) => this.drawPoint(point, "#00e5ff", `L${index + 1}`));
    this.rightPoints.forEach((point, index) => this.drawPoint(point, "#ffa500", `R${index + 1}`));
    this.endPoints.forEach((point, index) => this.drawPoint(point, "#c77dff", `G${index + 1}`));
    this.drawCenterline(polygon);
  },

  drawPolygon(points) {
    const ctx = this.ctx, width = this.canvas.width, height = this.canvas.height;
    ctx.beginPath();
    ctx.moveTo(points[0][0] * width, points[0][1] * height);
    points.slice(1).forEach(point => ctx.lineTo(point[0] * width, point[1] * height));
    ctx.closePath();
    ctx.fillStyle = "rgba(233,69,96,.22)";
    ctx.fill();
    ctx.strokeStyle = "#e94560";
    ctx.lineWidth = 1.5;
    ctx.stroke();
  },

  drawPoint(point, color, label) {
    const x = point[0] * this.canvas.width, y = point[1] * this.canvas.height;
    this.ctx.beginPath(); this.ctx.arc(x, y, 6, 0, Math.PI * 2);
    this.ctx.fillStyle = color; this.ctx.fill();
    this.ctx.fillStyle = "#fff"; this.ctx.font = "10px sans-serif"; this.ctx.textAlign = "center";
    this.ctx.fillText(label, x, y + 3);
  },

  drawPolyline(points, color) {
    if (points.length < 2) return;
    this.ctx.beginPath();
    this.ctx.moveTo(points[0][0] * this.canvas.width, points[0][1] * this.canvas.height);
    points.slice(1).forEach(point => this.ctx.lineTo(point[0] * this.canvas.width, point[1] * this.canvas.height));
    this.ctx.strokeStyle = color; this.ctx.lineWidth = 2; this.ctx.setLineDash([]); this.ctx.stroke();
  },

  drawExtendedLine(first, second, color, dash = []) {
    const hits = this.lineImageIntersections(first, second);
    if (hits.length < 2) return;
    this.ctx.beginPath();
    this.ctx.moveTo(hits[0][0] * this.canvas.width, hits[0][1] * this.canvas.height);
    this.ctx.lineTo(hits[1][0] * this.canvas.width, hits[1][1] * this.canvas.height);
    this.ctx.strokeStyle = color; this.ctx.lineWidth = 2; this.ctx.setLineDash(dash); this.ctx.stroke(); this.ctx.setLineDash([]);
  },

  drawCenterline(polygon) {
    if (!polygon || this.leftPoints.length < 2 || this.rightPoints.length < 2) return;
    let left, right;
    if (this.mode === "line") {
      left = this.lineImageIntersections(this.leftPoints[0], this.leftPoints[1]);
      right = this.lineImageIntersections(this.rightPoints[0], this.rightPoints[1]);
    } else {
      left = this.extendSplineToBounds(this.leftPoints);
      right = this.extendSplineToBounds(this.rightPoints);
    }
    const middle = [];
    for (let index = 0; index <= 60; index++) {
      const y = index / 60;
      const lx = this.sampleAtY(left, y), rx = this.sampleAtY(right, y);
      if (lx === null || rx === null) continue;
      const point = [(lx + rx) / 2, y];
      if (this.pointInPolygon(point, polygon)) middle.push(point);
    }
    this.drawPolyline(middle, "#28df65");
  },

  sideOfLine(first, second, point) {
    return (second[0] - first[0]) * (point[1] - first[1]) -
      (second[1] - first[1]) * (point[0] - first[0]);
  },

  segmentLineIntersection(first, second, lineFirst, lineSecond) {
    const ax = second[0] - first[0], ay = second[1] - first[1];
    const bx = lineSecond[0] - lineFirst[0], by = lineSecond[1] - lineFirst[1];
    const cross = ax * by - ay * bx;
    if (Math.abs(cross) < 1e-12) return first.slice();
    const t = ((lineFirst[0] - first[0]) * by - (lineFirst[1] - first[1]) * bx) / cross;
    return [first[0] + t * ax, first[1] + t * ay];
  },

  clipPolygonByLine(polygon, first, second, keepSign) {
    if (!polygon.length) return [];
    const output = [];
    polygon.forEach((current, index) => {
      const following = polygon[(index + 1) % polygon.length];
      const currentSide = this.sideOfLine(first, second, current) * keepSign;
      const followingSide = this.sideOfLine(first, second, following) * keepSign;
      if (currentSide >= 0) {
        output.push(current);
        if (followingSide < 0) output.push(this.segmentLineIntersection(current, following, first, second));
      } else if (followingSide >= 0) {
        output.push(this.segmentLineIntersection(current, following, first, second));
      }
    });
    return output;
  },

  lineImageIntersections(first, second) {
    const dx = second[0] - first[0], dy = second[1] - first[1], hits = [];
    if (Math.abs(dx) > 1e-12) {
      [0, 1].forEach(x => {
        const t = (x - first[0]) / dx, y = first[1] + t * dy;
        if (y >= 0 && y <= 1) hits.push([x, y]);
      });
    }
    if (Math.abs(dy) > 1e-12) {
      [0, 1].forEach(y => {
        const t = (y - first[1]) / dy, x = first[0] + t * dx;
        if (x >= 0 && x <= 1) hits.push([x, y]);
      });
    }
    const unique = hits.filter((point, index) => !hits.slice(0, index).some(other =>
      Math.abs(point[0] - other[0]) < 1e-9 && Math.abs(point[1] - other[1]) < 1e-9
    ));
    unique.sort((a, b) => a[1] - b[1] || a[0] - b[0]);
    return unique.slice(0, 2);
  },

  computeLinePolygon() {
    if (this.leftPoints.length < 2 || this.rightPoints.length < 2) return null;
    const [leftFirst, leftSecond] = this.leftPoints;
    const [rightFirst, rightSecond] = this.rightPoints;
    if ((leftFirst[0] === leftSecond[0] && leftFirst[1] === leftSecond[1]) ||
        (rightFirst[0] === rightSecond[0] && rightFirst[1] === rightSecond[1])) return null;
    let polygon = [[0, 0], [1, 0], [1, 1], [0, 1]];
    const rightMiddle = [(rightFirst[0] + rightSecond[0]) / 2, (rightFirst[1] + rightSecond[1]) / 2];
    const leftSign = this.sideOfLine(leftFirst, leftSecond, rightMiddle) >= 0 ? 1 : -1;
    polygon = this.clipPolygonByLine(polygon, leftFirst, leftSecond, leftSign);
    const leftMiddle = [(leftFirst[0] + leftSecond[0]) / 2, (leftFirst[1] + leftSecond[1]) / 2];
    const rightSign = this.sideOfLine(rightFirst, rightSecond, leftMiddle) >= 0 ? 1 : -1;
    return this.clipPolygonByLine(polygon, rightFirst, rightSecond, rightSign);
  },

  catmullRom(p0, p1, p2, p3, t) {
    const t2 = t * t, t3 = t2 * t;
    return [0, 1].map(axis => 0.5 * ((2 * p1[axis]) + (-p0[axis] + p2[axis]) * t +
      (2 * p0[axis] - 5 * p1[axis] + 4 * p2[axis] - p3[axis]) * t2 +
      (-p0[axis] + 3 * p1[axis] - 3 * p2[axis] + p3[axis]) * t3));
  },

  splinePoints(points, segments = 20) {
    if (points.length < 2) return points.slice();
    const output = [];
    for (let index = 0; index < points.length - 1; index++) {
      const p0 = points[Math.max(0, index - 1)], p1 = points[index];
      const p2 = points[index + 1], p3 = points[Math.min(points.length - 1, index + 2)];
      for (let segment = 0; segment < segments; segment++) {
        output.push(this.catmullRom(p0, p1, p2, p3, segment / segments));
      }
    }
    output.push(points[points.length - 1]);
    return output;
  },

  rayBoundaryIntersection(point, direction) {
    const candidates = [];
    if (Math.abs(direction[0]) > 1e-9) [0, 1].forEach(x => {
      const t = (x - point[0]) / direction[0], y = point[1] + t * direction[1];
      if (t > 0 && y >= 0 && y <= 1) candidates.push({ point: [x, y], t });
    });
    if (Math.abs(direction[1]) > 1e-9) [0, 1].forEach(y => {
      const t = (y - point[1]) / direction[1], x = point[0] + t * direction[0];
      if (t > 0 && x >= 0 && x <= 1) candidates.push({ point: [x, y], t });
    });
    candidates.sort((a, b) => a.t - b.t);
    return candidates[0]?.point || null;
  },

  extendSplineToBounds(points) {
    const dense = this.splinePoints(points, 20);
    if (points.length < 2) return dense;
    const topDirection = [points[0][0] - points[1][0], points[0][1] - points[1][1]];
    const bottomDirection = [
      points[points.length - 1][0] - points[points.length - 2][0],
      points[points.length - 1][1] - points[points.length - 2][1],
    ];
    const onBoundary = point => point[0] < 1e-6 || point[0] > 1 - 1e-6 || point[1] < 1e-6 || point[1] > 1 - 1e-6;
    const top = onBoundary(dense[0]) ? null : this.rayBoundaryIntersection(dense[0], topDirection);
    const bottom = onBoundary(dense[dense.length - 1])
      ? null
      : this.rayBoundaryIntersection(dense[dense.length - 1], bottomDirection);
    return [...(top ? [top] : []), ...dense, ...(bottom ? [bottom] : [])];
  },

  perimeterPosition(point) {
    const x = Math.max(0, Math.min(1, point[0])), y = Math.max(0, Math.min(1, point[1]));
    if (Math.abs(y) < 1e-6) return x;
    if (Math.abs(x - 1) < 1e-6) return 1 + y;
    if (Math.abs(y - 1) < 1e-6) return 3 - x;
    if (Math.abs(x) < 1e-6) return 4 - y;
    return null;
  },

  boundaryCorner(position) {
    if (position === 1) return [1, 0];
    if (position === 2) return [1, 1];
    if (position === 3) return [0, 1];
    return [0, 0];
  },

  dedupe(points) {
    return points.filter((point, index) => index === 0 ||
      Math.abs(point[0] - points[index - 1][0]) > 1e-9 ||
      Math.abs(point[1] - points[index - 1][1]) > 1e-9);
  },

  boundaryPath(first, second, direction) {
    const firstPosition = this.perimeterPosition(first), secondPosition = this.perimeterPosition(second);
    if (firstPosition === null || secondPosition === null) return [first, second];
    if (direction === "ccw") return this.dedupe(this.boundaryPath(second, first, "cw").reverse());
    let end = secondPosition;
    if (end < firstPosition) end += 4;
    const output = [first];
    [1, 2, 3, 4].forEach(raw => {
      let value = raw;
      if (value <= firstPosition) value += 4;
      if (value > firstPosition && value < end) output.push(this.boundaryCorner(raw));
    });
    output.push(second);
    return this.dedupe(output);
  },

  pointInPolygon(point, polygon) {
    let inside = false;
    for (let index = 0, previous = polygon.length - 1; index < polygon.length; previous = index++) {
      const a = polygon[index], b = polygon[previous];
      if ((a[1] > point[1]) !== (b[1] > point[1]) &&
          point[0] < ((b[0] - a[0]) * (point[1] - a[1])) / ((b[1] - a[1]) || 1e-12) + a[0]) inside = !inside;
    }
    return inside;
  },

  halfRegionFromCurve(curve, reference) {
    const start = curve[0], end = curve[curve.length - 1];
    if (this.perimeterPosition(start) === null || this.perimeterPosition(end) === null) return null;
    const clockwise = this.dedupe(curve.concat(this.boundaryPath(end, start, "cw").slice(1)));
    const counter = this.dedupe(curve.concat(this.boundaryPath(end, start, "ccw").slice(1)));
    const inClockwise = this.pointInPolygon(reference, clockwise);
    const inCounter = this.pointInPolygon(reference, counter);
    if (inCounter && !inClockwise) return counter;
    return clockwise;
  },

  curveReference(points) {
    const sum = points.reduce((value, point) => [value[0] + point[0], value[1] + point[1]], [0, 0]);
    return [sum[0] / points.length, sum[1] / points.length];
  },

  pathOnContext(ctx, points, scale) {
    ctx.beginPath(); ctx.moveTo(points[0][0] * scale, points[0][1] * scale);
    points.slice(1).forEach(point => ctx.lineTo(point[0] * scale, point[1] * scale));
    ctx.closePath();
  },

  traceMaskContour(alpha, size) {
    const edges = new Map();
    const add = (x1, y1, x2, y2) => {
      const key = `${x1},${y1}`, value = `${x2},${y2}`;
      if (!edges.has(key)) edges.set(key, []);
      edges.get(key).push(value);
    };
    const inside = (x, y) => x >= 0 && y >= 0 && x < size && y < size && alpha[y * size + x];
    for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (inside(x, y)) {
      if (!inside(x, y - 1)) add(x, y, x + 1, y);
      if (!inside(x + 1, y)) add(x + 1, y, x + 1, y + 1);
      if (!inside(x, y + 1)) add(x + 1, y + 1, x, y + 1);
      if (!inside(x - 1, y)) add(x, y + 1, x, y);
    }
    const loops = [];
    while (edges.size) {
      const start = edges.keys().next().value, loop = [];
      let current = start, guard = 0;
      while (current && guard++ < size * size * 8) {
        const [x, y] = current.split(",").map(Number);
        loop.push([x / size, y / size]);
        const nextValues = edges.get(current);
        if (!nextValues?.length) break;
        const next = nextValues.pop();
        if (!nextValues.length) edges.delete(current);
        current = next;
        if (current === start) break;
      }
      if (loop.length >= 3) loops.push(loop);
    }
    loops.sort((a, b) => b.length - a.length);
    return loops[0] || null;
  },

  computeCurvePolygon() {
    if (this.leftPoints.length < 2 || this.rightPoints.length < 2) return null;
    const left = this.extendSplineToBounds(this.leftPoints), right = this.extendSplineToBounds(this.rightPoints);
    const leftRegion = this.halfRegionFromCurve(left, this.curveReference(right));
    const rightRegion = this.halfRegionFromCurve(right, this.curveReference(left));
    if (!leftRegion || !rightRegion) return null;
    const size = 512, canvas = document.createElement("canvas");
    canvas.width = size; canvas.height = size;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.clearRect(0, 0, size, size); context.fillStyle = "#fff";
    this.pathOnContext(context, leftRegion, size); context.fill();
    context.globalCompositeOperation = "destination-in";
    this.pathOnContext(context, rightRegion, size); context.fill();
    context.globalCompositeOperation = "source-over";
    const data = context.getImageData(0, 0, size, size).data, alpha = new Uint8Array(size * size);
    for (let index = 0; index < alpha.length; index++) alpha[index] = data[index * 4 + 3] ? 1 : 0;
    return this.traceMaskContour(alpha, size);
  },

  nearReference() {
    if (this.mode === "curve") {
      const left = this.leftPoints[this.leftPoints.length - 1], right = this.rightPoints[this.rightPoints.length - 1];
      return [(left[0] + right[0]) / 2, (left[1] + right[1]) / 2];
    }
    const leftHits = this.lineImageIntersections(this.leftPoints[0], this.leftPoints[1]);
    const rightHits = this.lineImageIntersections(this.rightPoints[0], this.rightPoints[1]);
    if (!leftHits.length || !rightHits.length) {
      const left = this.leftPoints[this.leftPoints.length - 1], right = this.rightPoints[this.rightPoints.length - 1];
      return [(left[0] + right[0]) / 2, (left[1] + right[1]) / 2];
    }
    const left = leftHits[leftHits.length - 1], right = rightHits[rightHits.length - 1];
    return [(left[0] + right[0]) / 2, (left[1] + right[1]) / 2];
  },

  computePolygon() {
    let polygon = this.mode === "curve" ? this.computeCurvePolygon() : this.computeLinePolygon();
    if (!polygon?.length) return null;
    if (this.farEndMode === "line") {
      if (this.endPoints.length < 2) return null;
      if (this.endPoints[0][0] === this.endPoints[1][0] && this.endPoints[0][1] === this.endPoints[1][1]) return null;
      const reference = this.nearReference();
      const sign = this.sideOfLine(this.endPoints[0], this.endPoints[1], reference) >= 0 ? 1 : -1;
      polygon = this.clipPolygonByLine(polygon, this.endPoints[0], this.endPoints[1], sign);
    }
    if (polygon.length < 3) return null;
    return this.dedupe(polygon.map(point => [
      Math.max(0, Math.min(1, point[0])), Math.max(0, Math.min(1, point[1])),
    ]));
  },

  sampleAtY(points, y) {
    for (let index = 0; index < points.length - 1; index++) {
      const first = points[index], second = points[index + 1];
      if (y >= Math.min(first[1], second[1]) && y <= Math.max(first[1], second[1]) && Math.abs(second[1] - first[1]) > 1e-12) {
        const ratio = (y - first[1]) / (second[1] - first[1]);
        return first[0] + ratio * (second[0] - first[0]);
      }
    }
    return null;
  },

  downsample(points, target = 120) {
    if (points.length <= target) return points;
    return Array.from({ length: target }, (_, index) =>
      points[Math.round(index * (points.length - 1) / (target - 1))]
    );
  },

  undo() {
    this.loadedPolygon = null;
    if (this.endPoints.length) this.endPoints.pop();
    else if (this.mode === "curve") {
      if (this.curveSide === "done") this.curveSide = "right";
      if (this.curveSide === "right" && this.rightPoints.length) this.rightPoints.pop();
      else if (this.curveSide === "right" && !this.rightPoints.length) this.curveSide = "left";
      else this.leftPoints.pop();
    } else if (this.rightPoints.length) this.rightPoints.pop();
    else this.leftPoints.pop();
    this.syncDrawingControls(); this.draw();
  },

  clear() {
    this.leftPoints = []; this.rightPoints = []; this.endPoints = [];
    this.loadedPolygon = null; this.curveSide = "left";
    this.syncDrawingControls(); this.draw();
  },

  async request(path, options) {
    const response = await fetch(path, options);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "保存に失敗しました");
    return data;
  },

  async save() {
    if (!this.item || this.saving) return;
    const polygon = this.computePolygon();
    if (!polygon) return this.showError("左右境界と、必要な場合は終端2点を完成させてください。");
    this.saving = true; this.showError("");
    document.querySelector("#save-message").textContent = "保存中…";
    try {
      await this.request(`/api/annotation/${encodeURIComponent(this.item.image_id)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          polygon: this.downsample(polygon),
          edit: {
            mode: this.mode,
            left_points: this.leftPoints,
            right_points: this.rightPoints,
            far_end: { mode: this.farEndMode, points: this.endPoints },
          },
        }),
      });
      await this.advanceAfterSave("positive");
    } catch (error) {
      this.showError(String(error));
      document.querySelector("#save-message").textContent = "保存に失敗しました";
    } finally { this.saving = false; }
  },

  async saveEmpty() {
    if (!this.item || this.saving) return;
    if (!confirm("この画像を「対象なし」として保存しますか？")) return;
    this.saving = true;
    try {
      await this.request(`/api/annotation/${encodeURIComponent(this.item.image_id)}/empty`, { method: "POST" });
      await this.advanceAfterSave("empty");
    } catch (error) { this.showError(String(error)); }
    finally { this.saving = false; }
  },

  async deleteAnnotation() {
    if (!this.item || this.saving) return;
    if (!confirm("保存済みラベルと編集情報を削除しますか？")) return;
    this.saving = true;
    try {
      await this.request(`/api/annotation/${encodeURIComponent(this.item.image_id)}`, { method: "DELETE" });
      await this.advanceAfterSave("unannotated");
    } catch (error) { this.showError(String(error)); }
    finally { this.saving = false; }
  },

  async advanceAfterSave(newStatus) {
    const filter = document.querySelector("#status-filter").value;
    if (!filter || filter === newStatus) this.offset += 1;
    await this.loadState();
  },

  previous() { if (!this.saving && this.offset > 0) { this.offset -= 1; this.loadState(); } },
  next() { if (!this.saving && this.offset + 1 < this.total) { this.offset += 1; this.loadState(); } },

  renderReferenceButtons() {
    const container = document.querySelector("#reference-switch");
    const modalities = ["right", "depth"].filter(name => this.item.assets[name]);
    container.innerHTML = modalities.map((name, index) =>
      `<button type="button" data-reference="${name}" class="${index === 0 ? "active" : ""}">${name === "right" ? "Right" : "Depth"}</button>`
    ).join("");
    container.querySelectorAll("button").forEach(button => {
      button.addEventListener("click", () => this.showReference(button.dataset.reference));
    });
    if (modalities.length) this.showReference(modalities[0]);
    else {
      document.querySelector("#reference-image").removeAttribute("src");
      document.querySelector("#reference-empty").classList.remove("hidden");
    }
  },

  showReference(modality) {
    document.querySelectorAll("#reference-switch button").forEach(button =>
      button.classList.toggle("active", button.dataset.reference === modality)
    );
    const image = document.querySelector("#reference-image");
    image.src = this.item.assets[modality];
    document.querySelector("#reference-empty").classList.add("hidden");
  },

  showError(message) {
    const element = document.querySelector("#error");
    element.textContent = message;
    element.classList.toggle("hidden", !message);
  },
};

app.init();
