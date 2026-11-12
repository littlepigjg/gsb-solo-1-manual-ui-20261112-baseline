"use strict";

/* 图像对比视图：选择两张素材，按对齐参数生成差异图、改动范围与统计。 */

const diffState = {
  first: "",
  second: "",
  result: null,
  view: "heatmap",
  token: 0,
  timer: null,
  entered: false,
};

const DIFF_VIEW_NAMES = { heatmap: "差异图", overlay: "改动范围" };

function diffThreshold(sensitivity) {
  return Math.round(255 * (100 - sensitivity) / 100);
}

function diffReady() {
  return Boolean(diffState.first && diffState.second);
}

async function loadDiffAssets() {
  const result = await api("/assets?limit=200");
  rememberAssets(result.items);
  const first = $("#diff-first");
  const second = $("#diff-second");
  const previousFirst = diffState.first;
  const previousSecond = diffState.second;
  const options = result.items.map(asset =>
    `<option value="${asset.id}">${escapeHtml(asset.name)} · ${asset.width}×${asset.height}</option>`
  ).join("");
  first.innerHTML = options;
  second.innerHTML = options;
  const ids = new Set(result.items.map(asset => asset.id));
  diffState.first = ids.has(previousFirst) ? previousFirst : (state.current && ids.has(state.current.id) ? state.current.id : (result.items[0]?.id || ""));
  diffState.second = ids.has(previousSecond) ? previousSecond : (result.items[1]?.id || result.items[0]?.id || "");
  first.value = diffState.first;
  second.value = diffState.second;
  if (result.items.length < 2) {
    $("#diff-empty").innerHTML = '<div class="empty-icon" data-icon="image"></div><h2>素材库至少需要两张图片</h2><p>先导入或复制一张素材，再开始对比</p>';
    hydrateIcons($("#diff-empty"));
  }
  return result.items.length;
}

function scheduleDiff(delay = 400) {
  clearTimeout(diffState.timer);
  diffState.timer = setTimeout(() => {
    runDiff().catch(error => toast(error.message, "error"));
  }, delay);
}

async function runDiff() {
  if (!diffReady()) {
    $("#diff-busy").hidden = true;
    return;
  }
  const offsetX = Number($("#diff-offset-x").value) || 0;
  const offsetY = Number($("#diff-offset-y").value) || 0;
  const token = ++diffState.token;
  $("#diff-busy").hidden = false;
  try {
    const result = await api("/diffs", {
      method: "POST",
      body: {
        first_id: diffState.first,
        second_id: diffState.second,
        alignment: $("#diff-alignment").value,
        offset_x: offsetX,
        offset_y: offsetY,
        sensitivity: Number($("#diff-sensitivity").value),
      },
    });
    if (token !== diffState.token) return;
    diffState.result = result;
    renderDiffResult();
  } catch (error) {
    if (token === diffState.token) toast(error.message, "error");
  } finally {
    if (token === diffState.token) $("#diff-busy").hidden = true;
  }
}

function renderDiffResult() {
  const result = diffState.result;
  if (!result) return;
  const stats = result.stats;
  $("#diff-empty").hidden = true;
  const image = $("#diff-image");
  image.hidden = false;
  image.src = result.urls[diffState.view];
  const firstAsset = state.assetMap.get(diffState.first);
  const secondAsset = state.assetMap.get(diffState.second);
  $("#diff-canvas-note").textContent = `${firstAsset?.name || "第一张"} × ${secondAsset?.name || "第二张"} · 画布 ${stats.canvas_size[0]}×${stats.canvas_size[1]}`;
  const rows = [
    ["总变化像素", `${stats.total_changed_pixels.toLocaleString()}（${stats.total_change_percent}%）`],
    ["内容变化", `${stats.changed_pixels.toLocaleString()}（重叠区 ${stats.changed_percent_overlap}%）`],
    ["仅第一张覆盖", stats.only_first_pixels.toLocaleString()],
    ["仅第二张覆盖", stats.only_second_pixels.toLocaleString()],
    ["覆盖变化占比", `${stats.coverage_percent}%`],
    ["未变化像素", stats.unchanged_pixels.toLocaleString()],
    ["最大差异", `${stats.max_distance} / 255`],
    ["平均差异 · 重叠区", stats.mean_distance_overlap],
    ["判定阈值", `> ${stats.threshold} / 255`],
    ["对齐 / 偏移", `${{ center: "中心", first: "第一张基准", second: "第二张基准" }[stats.alignment]} · ${stats.offset[0]}, ${stats.offset[1]}`],
  ];
  if (!stats.full_resolution) {
    rows.push(["工作分辨率", `已缩放至 ${Math.round(stats.work_scale * 100)}%（画布超过像素上限）`]);
  }
  $("#diff-stats").innerHTML = rows.map(([key, value]) => `<dt>${key}</dt><dd>${escapeHtml(value)}</dd>`).join("");
  if (stats.identical) {
    $("#diff-bbox").innerHTML = "<strong>两图在当前对齐与敏感度下完全一致。</strong>";
  } else if (result.bbox) {
    const box = result.bbox;
    $("#diff-bbox").innerHTML = `改动范围：左上角 (${box.x}, ${box.y})，宽 ${box.width} × 高 ${box.height} 像素。`;
  }
  renderDiffLegend();
  const downloadImage = $("#diff-download-image");
  downloadImage.href = result.urls[diffState.view];
  downloadImage.setAttribute("download", `${DIFF_VIEW_NAMES[diffState.view]}.png`);
  downloadImage.setAttribute("aria-disabled", "false");
  const downloadZip = $("#diff-download-zip");
  downloadZip.href = result.download_url;
  downloadZip.setAttribute("download", result.download_name);
  downloadZip.setAttribute("aria-disabled", "false");
}

function renderDiffLegend() {
  const ramp = '<span class="sw ramp"></span>';
  const shared = `
    <span class="chip"><span class="sw" style="background:#ff7a45"></span>仅第一张覆盖</span>
    <span class="chip"><span class="sw" style="background:#4a99e2"></span>仅第二张覆盖</span>
    <span class="chip"><span class="sw" style="background:#ffe85c"></span>改动范围框</span>`;
  $("#diff-legend").innerHTML = diffState.view === "heatmap"
    ? `<span class="chip"><span class="sw" style="background:#18201c"></span>无差异</span><span class="chip">${ramp}差异强度（蓝→红）</span>${shared}`
    : `<span class="chip"><span class="sw" style="background:#c94c38"></span>内容变化</span>${shared}<span class="chip">底色为第一张（仅第二张区域显示第二张）</span>`;
}

function setDiffView(view) {
  diffState.view = view;
  $$("#diff-view [data-diff-view]").forEach(button => {
    const active = button.dataset.diffView === view;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  if (diffState.result) renderDiffResult();
  else renderDiffLegend();
}

function updateSensitivityHint() {
  const sensitivity = Number($("#diff-sensitivity").value);
  $("#diff-sensitivity-value").textContent = sensitivity;
  $("#diff-threshold-hint").textContent = `差异超过 ${diffThreshold(sensitivity)} / 255 色阶才计为变化`;
}

async function enterDiffView() {
  try {
    const count = await loadDiffAssets();
    if (count >= 2 && diffReady() && !diffState.result) scheduleDiff(0);
  } catch (error) {
    toast(error.message, "error");
  }
}

function initDiff() {
  document.addEventListener("studio:viewchange", event => {
    if (event.detail.view === "diff") {
      diffState.entered = true;
      enterDiffView();
    }
  });
  $("#diff-first").addEventListener("change", event => { diffState.first = event.target.value; scheduleDiff(); });
  $("#diff-second").addEventListener("change", event => { diffState.second = event.target.value; scheduleDiff(); });
  $("#diff-swap").addEventListener("click", () => {
    [diffState.first, diffState.second] = [diffState.second, diffState.first];
    $("#diff-first").value = diffState.first;
    $("#diff-second").value = diffState.second;
    scheduleDiff();
  });
  $("#diff-use-selection").addEventListener("click", () => {
    const ids = [...state.selected];
    if (ids.length !== 2) {
      toast("请在素材库勾选正好两张图片。", "error");
      return;
    }
    [diffState.first, diffState.second] = ids;
    $("#diff-first").value = diffState.first;
    $("#diff-second").value = diffState.second;
    scheduleDiff(0);
  });
  $("#diff-alignment").addEventListener("change", () => scheduleDiff());
  $("#diff-offset-x").addEventListener("input", () => scheduleDiff());
  $("#diff-offset-y").addEventListener("input", () => scheduleDiff());
  $("#diff-sensitivity").addEventListener("input", () => {
    updateSensitivityHint();
    scheduleDiff();
  });
  $("#diff-run").addEventListener("click", () => scheduleDiff(0));
  $$("#diff-view [data-diff-view]").forEach(button => {
    button.addEventListener("click", () => setDiffView(button.dataset.diffView));
  });
  ["#diff-download-image", "#diff-download-zip"].forEach(selector => {
    $(selector).addEventListener("click", event => {
      if (!diffState.result) {
        event.preventDefault();
        toast("请先生成对比结果。", "error");
      }
    });
  });
  updateSensitivityHint();
  renderDiffLegend();
}

initDiff();
