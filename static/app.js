/* TikTok Caption Tool — frontend */
"use strict";

// Locked defaults (mirrors app/config.py CAPTION_STYLE)
const DEFAULT_STYLE = {
  fontWeight: 500,
  strokeRatio: 0.135,
  fontSizeRatio: 0.038,
  maxWidthRatio: 0.68,
  yRatio: 0.46,
  lineHeight: 1.15,
  maxLines: 6,
  captionMode: "outline",
  boxPadRatio: 0.38,
  boxCornerRatio: 0.2,
};

const LS_THEME = "captionToolTheme";
const LS_CROPS = "captionToolCrops_v1";
const LS_TRIMS = "captionToolTrims_v1";
const LS_ACTIVE_JOBS = "captionToolActiveJobs_v2";

let config = null;
let sources = []; // ordered list of source objects from server
let workspaceRenderPairs = [];
let clearRenderPairsOnSave = false;
let style = loadStyle();
let currentBatchId = null;
let currentBatchOutputDir = null;
let focusedBatchId = null;
let focusedUploadId = null;
const activeBatchIds = new Set();
const activeUploadIds = new Set();
const activeSparkIds = new Set();
const activeProvisionIds = new Set();
const jobSnapshots = Object.create(null); // id -> last state
let persistTimer = null;
let restoring = false;
let selectedSourceId = null;
let endCapSaveTimer = null;
let emojiTargetId = null; // element id currently receiving emoji inserts
let emojiCat = "smileys";
let emojiOpen = false;
let emojiSel = { start: 0, end: 0 }; // caret on target while picker has focus
let emojiOpenedAt = 0;
let ttAccounts = [];
let ttSelectedIds = new Set();
let ttSettingsSaveTimer = null;
let currentUploadId = null;
let lastUploadBatchId = null;
let cropModalSourceId = null;
let cropDraft = { x: 0, y: 0, w: 1, h: 1 };
let cropDrag = null;
let trimDraft = { start: 0, end: 0 };
let trimDrag = null; // 'in' | 'out' | null
let trimSaveTimer = null;
let fbStatus = null;
let fbCampaigns = [];
let currentFbJobId = null;
let currentSparkJobId = null;
let currentProvisionDockSt = null;
let currentProvisionWorkflows = [];
let lastProvisionDetailAt = 0;
let remoteLoginId = null;
let remoteFrameTimer = null;

// ---------------------------------------------------------------------------
// Active job persistence (survives tab close / refresh; work continues server-side)
// Closing the tab NEVER cancels jobs — only explicit Cancel buttons do.
// ---------------------------------------------------------------------------

function readActiveJobsLs() {
  try {
    const raw = JSON.parse(localStorage.getItem(LS_ACTIVE_JOBS) || "{}");
    if (raw && (Array.isArray(raw.batchIds) || Array.isArray(raw.uploadIds) || raw.fbJobId || raw.loginId || Array.isArray(raw.sparkJobIds) || Array.isArray(raw.provisionJobIds))) {
      return pruneStaleActiveJobs({
        batchIds: (raw.batchIds || []).filter(Boolean),
        uploadIds: (raw.uploadIds || []).filter(Boolean),
        uploadBatchId: raw.uploadBatchId || "",
        fbJobId: raw.fbJobId || "",
        sparkJobIds: (raw.sparkJobIds || []).filter(Boolean),
        provisionJobIds: (raw.provisionJobIds || []).filter(Boolean),
        loginId: raw.loginId || "",
        savedAt: raw.savedAt || 0,
      });
    }
    // Migrate v1 single-id shape
    const v1 = JSON.parse(localStorage.getItem("captionToolActiveJobs_v1") || "{}");
    return {
      batchIds: v1 && v1.batchId ? [v1.batchId] : [],
      uploadIds: v1 && v1.uploadId ? [v1.uploadId] : [],
      uploadBatchId: (v1 && v1.uploadBatchId) || "",
      fbJobId: "",
      sparkJobIds: [],
      provisionJobIds: [],
      loginId: "",
    };
  } catch {
    return { batchIds: [], uploadIds: [], uploadBatchId: "", fbJobId: "", sparkJobIds: [], provisionJobIds: [], loginId: "" };
  }
}

function pruneStaleActiveJobs(mem) {
  const now = Date.now();
  const maxAgeMs = 6 * 60 * 60 * 1000;
  const ts = Number(mem && mem.savedAt) || 0;
  if (ts && now - ts > maxAgeMs) {
    return { batchIds: [], uploadIds: [], uploadBatchId: "", fbJobId: "", sparkJobIds: [], provisionJobIds: [], loginId: "" };
  }
  return mem;
}

function writeActiveJobsLs() {
  const next = {
    batchIds: [...activeBatchIds],
    uploadIds: [...activeUploadIds],
    uploadBatchId: lastUploadBatchId || "",
    fbJobId: currentFbJobId || "",
    sparkJobIds: [...activeSparkIds],
    provisionJobIds: [...activeProvisionIds],
    loginId: remoteLoginId || "",
    savedAt: Date.now(),
  };
  try {
    localStorage.setItem(LS_ACTIVE_JOBS, JSON.stringify(next));
  } catch (err) {
    console.warn("Failed to save active jobs to localStorage:", err);
  }
}

function rememberBatchJob(batchId) {
  if (!batchId) return;
  const isNew = !activeBatchIds.has(batchId);
  activeBatchIds.add(batchId);
  focusedBatchId = batchId;
  currentBatchId = batchId;
  window.currentBatchId = currentBatchId;
  // Switch dock selection only when a new render starts — never on poll/resume re-track.
  if (isNew) {
    dockFocusKind = "batch";
    dockManualCollapseDuringJob = false;
    dockAutoExpandedFor = null;
    lastFinishedBatchSt = null;
    lastFinishedUploadSt = null;
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function focusNextLiveDockJob() {
  // A completed job must never hide a live Spark fetch. Fetches are commonly
  // started while an upload or render is still finishing.
  if (currentSparkJobId) dockFocusKind = "spark";
  else if (focusedUploadId) dockFocusKind = "upload";
  else if (focusedBatchId) dockFocusKind = "batch";
  else if (currentFbJobId) dockFocusKind = "facebook";
  else if (activeProvisionIds.size) dockFocusKind = "provision";
}

function forgetBatchJob(batchId) {
  if (batchId) activeBatchIds.delete(batchId);
  delete jobSnapshots[batchId];
  if (focusedBatchId === batchId) {
    focusedBatchId = [...activeBatchIds].pop() || null;
    currentBatchId = focusedBatchId;
    window.currentBatchId = currentBatchId;
    if (!focusedBatchId && dockFocusKind === "batch") {
      focusNextLiveDockJob();
    }
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function rememberUploadJob(uploadId, batchId) {
  if (!uploadId) return;
  const isNew = !activeUploadIds.has(uploadId);
  activeUploadIds.add(uploadId);
  focusedUploadId = uploadId;
  currentUploadId = uploadId;
  // Switch dock selection only when a new upload starts — never on poll/resume re-track.
  if (isNew) {
    dockFocusKind = "upload";
    dockManualCollapseDuringJob = false;
    dockAutoExpandedFor = null;
    lastFinishedUploadSt = null;
  }
  if (batchId) lastUploadBatchId = batchId;
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function forgetUploadJob(uploadId) {
  if (uploadId) activeUploadIds.delete(uploadId);
  delete jobSnapshots[uploadId];
  if (focusedUploadId === uploadId) {
    focusedUploadId = [...activeUploadIds].pop() || null;
    currentUploadId = focusedUploadId;
    if (!focusedUploadId && dockFocusKind === "upload") {
      focusNextLiveDockJob();
    }
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function clearBatchJobMemory() {
  // Legacy: clear focused only if no active set left after forget
  if (focusedBatchId) forgetBatchJob(focusedBatchId);
  else writeActiveJobsLs();
}

function clearUploadJobMemory() {
  if (focusedUploadId) forgetUploadJob(focusedUploadId);
  else writeActiveJobsLs();
}

function rememberFbJob(jobId) {
  if (!jobId) return;
  const isNew = currentFbJobId !== jobId;
  currentFbJobId = jobId;
  if (isNew) {
    dockFocusKind = "facebook";
    dockManualCollapseDuringJob = false;
    dockAutoExpandedFor = null;
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function forgetFbJob(jobId) {
  if (jobId && currentFbJobId && jobId !== currentFbJobId) return;
  if (currentFbJobId) delete jobSnapshots[currentFbJobId];
  currentFbJobId = null;
  if (dockFocusKind === "facebook") {
    focusNextLiveDockJob();
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

window.rememberFbJob = rememberFbJob;
window.forgetFbJob = forgetFbJob;

function rememberSparkJob(jobId) {
  if (!jobId) return;
  const isNew = !activeSparkIds.has(jobId);
  const shouldFocus = isNew && !currentSparkJobId;
  activeSparkIds.add(jobId);
  // A queued-job poll must not steal the dock from the job using the phone.
  if (!currentSparkJobId) currentSparkJobId = jobId;
  if (shouldFocus) {
    dockFocusKind = "spark";
    dockManualCollapseDuringJob = false;
    dockAutoExpandedFor = null;
    lastFinishedSparkSt = null;
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function selectPreferredSparkJob() {
  const current = jobSnapshots[currentSparkJobId];
  if (
    currentSparkJobId &&
    activeSparkIds.has(currentSparkJobId) &&
    (!current || current.status === "running")
  ) {
    return currentSparkJobId;
  }
  // A running job always takes precedence. Set order preserves FIFO among
  // queued jobs after the running job finishes.
  const running = [...activeSparkIds].find((id) => jobSnapshots[id]?.status === "running");
  return running || [...activeSparkIds][0] || null;
}

function forgetSparkJob(jobId) {
  if (!jobId) return;
  activeSparkIds.delete(jobId);
  delete jobSnapshots[jobId];
  if (currentSparkJobId === jobId) {
    currentSparkJobId = selectPreferredSparkJob();
    if (!currentSparkJobId && dockFocusKind === "spark") {
      focusNextLiveDockJob();
    }
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function rememberProvisionJob(jobId) {
  if (!jobId) return;
  const isNew = !activeProvisionIds.has(jobId);
  activeProvisionIds.add(jobId);
  if (isNew) {
    if (!activeBatchIds.size && !activeUploadIds.size && !currentSparkJobId && !currentFbJobId) {
      dockFocusKind = "provision";
    }
    dockManualCollapseDuringJob = false;
    dockAutoExpandedFor = null;
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

function forgetProvisionJob(jobId) {
  if (!jobId) return;
  activeProvisionIds.delete(jobId);
  delete jobSnapshots[jobId];
  if (dockFocusKind === "provision" && !activeProvisionIds.size) {
    focusNextLiveDockJob();
  }
  writeActiveJobsLs();
  refreshActiveJobsDock();
}

const LS_DOCK_COLLAPSED = "captionToolDockCollapsed";
let dockManualCollapseDuringJob = false;
let dockAutoExpandedFor = null;
let dockFocusKind = "batch"; // "batch" | "upload" | "facebook"
let lastFinishedBatchSt = null;
let lastFinishedUploadSt = null;
let lastFinishedSparkSt = null;
let _dockWired = false;

function jobStatusLabel(st) {
  if (!st) return "";
  if (st.status === "queued") {
    const pos = st.queuePosition || "?";
    return `queued #${pos}`;
  }
  if (st.status === "running") {
    const done = st.completed != null ? st.completed : (st.uploaded != null ? st.uploaded : 0);
    const total = st.total != null ? st.total : "?";
    return `${done}/${total}`;
  }
  return st.status || "";
}

function sparkJobTotal(st) {
  const reported = Number(st && st.total);
  if (Number.isFinite(reported) && reported >= 0) return reported;
  return Array.isArray(st && st.items) ? st.items.length : 0;
}

function isDockCollapsed() {
  const panel = $("progressPanel");
  if (!panel) return true;
  return panel.getAttribute("data-collapsed") !== "false";
}

function readDockCollapsedPref() {
  try {
    const v = localStorage.getItem(LS_DOCK_COLLAPSED);
    if (v === null) return true;
    return v !== "false";
  } catch {
    return true;
  }
}

function writeDockCollapsedPref(collapsed) {
  try {
    localStorage.setItem(LS_DOCK_COLLAPSED, collapsed ? "true" : "false");
  } catch {}
}

function setDockCollapsed(collapsed, { auto = false, persist = true } = {}) {
  const panel = $("progressPanel");
  const strip = $("dockStrip");
  const backdrop = $("dockBackdrop");
  if (!panel) return;
  const next = !!collapsed;
  panel.setAttribute("data-collapsed", next ? "true" : "false");
  if (strip) strip.setAttribute("aria-expanded", next ? "false" : "true");
  if (backdrop) {
    if (next) {
      backdrop.classList.add("hidden");
      backdrop.classList.remove("is-open");
      backdrop.setAttribute("aria-hidden", "true");
    } else {
      const mobile = window.matchMedia("(max-width: 700px)").matches;
      if (mobile) {
        backdrop.classList.remove("hidden");
        requestAnimationFrame(() => backdrop.classList.add("is-open"));
        backdrop.setAttribute("aria-hidden", "false");
      } else {
        backdrop.classList.add("hidden");
        backdrop.classList.remove("is-open");
      }
    }
  }
  if (persist && !auto) writeDockCollapsedPref(next);
  if (!auto && next) dockManualCollapseDuringJob = true;
  if (!auto && !next) dockManualCollapseDuringJob = false;
}

function showProgressDock({ expand = false } = {}) {
  const panel = $("progressPanel");
  if (!panel) return;
  panel.classList.remove("hidden");
  if (expand) {
    setDockCollapsed(false, { persist: false });
  } else if (!panel.dataset.dockInited) {
    setDockCollapsed(readDockCollapsedPref(), { persist: false });
    panel.dataset.dockInited = "1";
  }
}

function hideProgressDock() {
  const panel = $("progressPanel");
  const backdrop = $("dockBackdrop");
  if (panel) {
    panel.classList.add("hidden");
    delete panel.dataset.dockInited;
  }
  if (backdrop) {
    backdrop.classList.add("hidden");
    backdrop.classList.remove("is-open");
  }
  dockManualCollapseDuringJob = false;
  dockAutoExpandedFor = null;
  lastFinishedBatchSt = null;
  lastFinishedUploadSt = null;
  // Hide leftover action buttons from a finished upload/render.
  if ($("continueTikTokUpload")) $("continueTikTokUpload").classList.add("hidden");
  if ($("uploadToTikTok")) $("uploadToTikTok").classList.add("hidden");
  if ($("dockDismiss")) $("dockDismiss").classList.add("hidden");
}

function maybeAutoExpandDock(jobId, status) {
  if (!jobId) return;
  if (status !== "done" && status !== "cancelled") return;
  if (dockManualCollapseDuringJob) return;
  if (dockAutoExpandedFor === jobId) return;
  dockAutoExpandedFor = jobId;
  setDockCollapsed(false, { auto: true, persist: false });
}

function dockPctOfCurrent(current) {
  if (!current || !current.durationSeconds) return 0;
  return Math.round((current.progressSeconds / current.durationSeconds) * 100);
}

function dockFrac(st) {
  if (!st) return 0;
  const total = Math.max(st.total || 1, 1);
  if (st.kind === "upload" || st.uploaded != null) {
    const uploaded = st.uploaded ?? 0;
    const skipped = st.skippedAlready ?? 0;
    const failed = st.failed ?? 0;
    return (uploaded + skipped + failed) / total;
  }
  if (st.kind === "spark") {
    return (st.completed || 0) / total;
  }
  if (st.kind === "provision") {
    return Math.min((st.completed || 0) / 100, 1);
  }
  let frac = (st.completed || 0) / total;
  if (st.current && st.current.durationSeconds) {
    frac += (st.current.progressSeconds / st.current.durationSeconds) / total;
  }
  return Math.min(frac, 1);
}

function dockSummaryFor(st, kind) {
  if (!st) return "";
  if (st.status === "queued") return `queued #${st.queuePosition ?? "…"}`;
  if (kind === "upload") {
    const uploaded = st.uploaded ?? 0;
    const total = st.total || 0;
    if (st.status === "done") return `${uploaded}/${total} uploaded`;
    if (st.canContinue) return `${uploaded}/${total} · paused`;
    const cur = st.current
      ? ` · ${st.current.filename || st.current.status || ""}${
          st.current.accountName ? ` → ${st.current.accountName}` : ""
        }`
      : "";
    return `${uploaded}/${total}${cur}`.trim();
  }
  if (kind === "facebook") {
    if (st.status === "done") return `${st.completed || 0}/${st.total || 0} ads done`;
    return `${st.completed || 0}/${st.total || 0}${st.message ? " · " + st.message : ""}`;
  }
  if (kind === "spark") {
    const fetched = st.completed || 0;
    const total = st.total || 0;
    return st.status === "done" ? `${fetched}/${total} fetched` : `${fetched}/${total} fetching`;
  }
  if (kind === "provision") {
    return st.message || "Business Center work in progress";
  }
  if (st.status === "done") return `${st.completed}/${st.total} done`;
  if (st.status === "cancelled") return `${st.completed}/${st.total} cancelled`;
  const pct = dockPctOfCurrent(st.current);
  const cur = st.current
    ? ` · ${st.current.video}${pct ? ` ${pct}%` : ""}`
    : "";
  return `${st.completed}/${st.total}${cur}`;
}

function dockPillLabelFor(st, kind) {
  if (!st) return "…";
  if (st.status === "done") return "✓";
  if (st.status === "cancelled" || st.status === "failed") return "!";
  if (st.status === "queued") return `#${st.queuePosition || "?"}`;
  if (kind === "upload") {
    const uploaded = st.uploaded ?? 0;
    return `${uploaded}/${st.total || "?"}`;
  }
  if (kind === "spark") return `${st.completed ?? 0}/${st.total ?? "?"}`;
  if (kind === "provision") return st.pillLabel || "Active";
  return `${st.completed ?? 0}/${st.total ?? "?"}`;
}

function focusedDockState() {
  if (dockFocusKind === "provision" && currentProvisionDockSt) {
    return { kind: "provision", id: currentProvisionDockSt.id, st: currentProvisionDockSt };
  }
  if (dockFocusKind === "facebook" && currentFbJobId) {
    const st = jobSnapshots[currentFbJobId] || null;
    return { kind: "facebook", id: currentFbJobId, st };
  }
  if (dockFocusKind === "upload" && focusedUploadId) {
    return { kind: "upload", id: focusedUploadId, st: jobSnapshots[focusedUploadId] || null };
  }
  if (dockFocusKind === "spark" && currentSparkJobId) {
    return { kind: "spark", id: currentSparkJobId, st: jobSnapshots[currentSparkJobId] || null };
  }
  if (focusedBatchId) {
    return { kind: "batch", id: focusedBatchId, st: jobSnapshots[focusedBatchId] || null };
  }
  if (focusedUploadId) {
    return { kind: "upload", id: focusedUploadId, st: jobSnapshots[focusedUploadId] || null };
  }
  if (currentFbJobId) {
    return { kind: "facebook", id: currentFbJobId, st: jobSnapshots[currentFbJobId] || null };
  }
  if (lastFinishedSparkSt) {
    return { kind: "spark", id: lastFinishedSparkSt.id, st: lastFinishedSparkSt };
  }
  if (currentProvisionDockSt) {
    return { kind: "provision", id: currentProvisionDockSt.id, st: currentProvisionDockSt };
  }
  // Finished jobs kept for Continue / Upload CTAs after the live job set is cleared.
  if (lastFinishedUploadSt && lastFinishedUploadSt.canContinue) {
    return {
      kind: "upload",
      id: lastFinishedUploadSt.id,
      st: lastFinishedUploadSt,
    };
  }
  if (lastFinishedBatchSt) {
    return {
      kind: "batch",
      id: lastFinishedBatchSt.id,
      st: lastFinishedBatchSt,
    };
  }
  return null;
}

function renderDockChips() {
  const chips = $("dockChips");
  if (!chips) return;
  const rows = [];
  for (const id of activeBatchIds) {
    const st = jobSnapshots[id] || { id, status: "running", kind: "batch" };
    rows.push({ kind: "batch", id, st });
  }
  for (const id of activeUploadIds) {
    const st = jobSnapshots[id] || { id, status: "running", kind: "upload" };
    rows.push({ kind: "upload", id, st });
  }
  if (currentFbJobId) {
    const st = jobSnapshots[currentFbJobId] || { id: currentFbJobId, status: "running" };
    rows.push({ kind: "facebook", id: currentFbJobId, st });
  }
  for (const id of activeSparkIds) {
    const st = jobSnapshots[id] || { id, status: "running", kind: "spark" };
    rows.push({ kind: "spark", id, st });
  }
  if (currentProvisionDockSt) {
    rows.push({ kind: "provision", id: currentProvisionDockSt.id, st: currentProvisionDockSt });
  }
  if (rows.length <= 1) {
    chips.innerHTML = "";
    return;
  }
  const focus = focusedDockState();
  chips.innerHTML = rows
    .map((row) => {
      const focused = !!(focus && focus.kind === row.kind && focus.id === row.id);
      const kindLabel =
        row.kind === "batch" ? "Render" : row.kind === "upload" ? "Upload" : row.kind === "spark" ? "Spark codes" : row.kind === "provision" ? "Business centers" : "Ads";
      // A queued Spark fetch is still a complete job, so show its own total.
      const statusLabel = row.kind === "spark"
        ? `${row.st.completed ?? 0}/${sparkJobTotal(row.st)}${row.st.status === "queued" ? " · queued" : ""}`
        : jobStatusLabel(row.st);
      return `<button type="button" class="dock-chip ${focused ? "is-focused" : ""}" data-kind="${row.kind}" data-id="${escapeHtml(row.id)}" title="Show ${kindLabel}">
        <span>${kindLabel}</span>
        <span class="dock-chip-count">${escapeHtml(statusLabel)}</span>
      </button>`;
    })
    .join("");
}

function isUploadJobBusy() {
  for (const id of activeUploadIds) {
    const st = jobSnapshots[id];
    if (!st) return true;
    if (st.status === "running" || st.status === "queued") return true;
  }
  return false;
}

function syncDockCancelButtons() {
  const focus = focusedDockState();
  const kind = focus ? focus.kind : null;
  const st = focus && focus.st;
  const running = !!(st && (st.status === "running" || st.status === "queued"));
  if ($("cancelBatch")) {
    $("cancelBatch").classList.toggle("hidden", !(kind === "batch" && running));
  }
  if ($("cancelTikTokUpload")) {
    $("cancelTikTokUpload").classList.toggle("hidden", !(kind === "upload" && running));
  }
  if ($("cancelFacebookAds")) {
    $("cancelFacebookAds").classList.toggle("hidden", !(kind === "facebook" && running));
  }
  if ($("cancelProvision")) {
    $("cancelProvision").classList.toggle("hidden", !(kind === "provision" && running && (st.jobIds || []).length));
  }
  if ($("continueTikTokUpload")) {
    const showCont =
      kind === "upload" &&
      !!st &&
      !!st.canContinue &&
      !!st.sourceBatchId &&
      !running;
    $("continueTikTokUpload").classList.toggle("hidden", !showCont);
  }
  // Never offer "Upload to TikTok" while an upload is already going.
  if ($("uploadToTikTok")) {
    const fromFocus =
      kind === "batch" &&
      st &&
      st.status === "done" &&
      (st.items || []).some((i) => i.output);
    const fromLast =
      lastFinishedBatchSt &&
      lastFinishedBatchSt.status === "done" &&
      (lastFinishedBatchSt.items || []).some((i) => i.output);
    const showUploadCta =
      (fromFocus || fromLast) &&
      !isUploadJobBusy() &&
      kind !== "upload" &&
      kind !== "facebook";
    $("uploadToTikTok").classList.toggle("hidden", !showUploadCta);
  }
}

function renderDockStrip(optionalSt) {
  const panel = $("progressPanel");
  if (!panel || panel.classList.contains("hidden")) return;

  const focus = focusedDockState();
  const kind = focus ? focus.kind : "batch";
  const st = optionalSt && focus && optionalSt.id === focus.id
    ? optionalSt
    : (focus && focus.st) || optionalSt || null;

  const kindEl = $("dockKind");
  const summaryEl = $("dockSummary");
  const etaEl = $("dockEta");
  const dot = $("dockStatusDot");
  const edge = $("dockEdgeFill");
  const pill = $("dockPillLabel");
  const dismiss = $("dockDismiss");

  const kindLabel =
    kind === "upload" ? "Upload" : kind === "facebook" ? "Ads" : kind === "spark" ? "Fetching Spark Codes" : kind === "provision" ? "Business Center activity" : "Render";
  if (kindEl) kindEl.textContent = kindLabel;

  const status = st ? (st.status || "running") : "running";
  panel.setAttribute("data-status", status);
  if (dot) {
    dot.className = "dock-dot " + (
      status === "queued" ? "queued"
        : status === "done" ? "done"
          : status === "failed" || status === "cancelled" ? "failed"
            : "running"
    );
  }

  const summary = st ? dockSummaryFor(st, kind) : "";
  if (summaryEl) summaryEl.textContent = summary;
  if (pill) pill.textContent = st ? dockPillLabelFor(st, kind) : "…";

  let eta = "";
  if (st && (status === "running" || status === "queued")) {
    eta = fmtEtaLeft(st.etaSeconds) || "";
  }
  if (etaEl) etaEl.textContent = eta.replace(/^about\s+/i, "~").replace(/\s+left$/i, "");

  const frac = dockFrac(st);
  const pct = Math.round(Math.min(frac, 1) * 100);
  panel.style.setProperty("--pct", String(pct));
  if (edge) {
    edge.style.width = `${pct}%`;
    if (status === "done") edge.style.width = "100%";
  }

  const terminal = status === "done" || status === "cancelled" || status === "failed";
  const hasActive =
    activeBatchIds.size > 0 || activeUploadIds.size > 0 || activeSparkIds.size > 0 || activeProvisionIds.size > 0 || !!currentFbJobId || !!currentSparkJobId || !!currentProvisionDockSt;
  if (dismiss) {
    dismiss.classList.toggle("hidden", !(terminal && !hasActive));
  }

  // Sync which body panel is visible for the focused kind
  if ($("renderProgressBlock")) {
    $("renderProgressBlock").classList.toggle("hidden", kind !== "batch");
  }
  if ($("tiktokUploadPanel")) {
    $("tiktokUploadPanel").classList.toggle("hidden", kind !== "upload");
  }
  if ($("sparkFetchPanel")) {
    $("sparkFetchPanel").classList.toggle("hidden", kind !== "spark");
  }
  if ($("provisionProgressPanel")) {
    $("provisionProgressPanel").classList.toggle("hidden", kind !== "provision");
  }
  if ($("facebookAdsPanel")) {
    $("facebookAdsPanel").classList.toggle("hidden", kind !== "facebook");
  }

  renderDockChips();
  syncDockCancelButtons();
  if (st && terminal) maybeAutoExpandDock(st.id, status);
}

function refreshActiveJobsDock() {
  const hasLive =
    activeBatchIds.size > 0 || activeUploadIds.size > 0 || activeSparkIds.size > 0 || activeProvisionIds.size > 0 || !!currentFbJobId || !!currentSparkJobId || !!currentProvisionDockSt;
  const focus = focusedDockState();
  if (!hasLive && !focus) {
    const panel = $("progressPanel");
    if (panel && !panel.classList.contains("hidden")) hideProgressDock();
    return;
  }
  renderDockChips();
  renderDockStrip();
}

function focusDockJob(kind, id) {
  dockFocusKind = kind;
  if (kind === "batch") {
    focusedBatchId = id;
    currentBatchId = id;
    window.currentBatchId = id;
    const st = jobSnapshots[id];
    if ($("renderProgressBlock")) $("renderProgressBlock").classList.remove("hidden");
    if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.add("hidden");
    if ($("facebookAdsPanel")) $("facebookAdsPanel").classList.add("hidden");
    if (st) renderBatchState(st);
  } else if (kind === "upload") {
    focusedUploadId = id;
    currentUploadId = id;
    if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.remove("hidden");
    if ($("renderProgressBlock")) $("renderProgressBlock").classList.add("hidden");
    if ($("facebookAdsPanel")) $("facebookAdsPanel").classList.add("hidden");
    const st = jobSnapshots[id];
    if (st) renderUploadState(st);
  } else if (kind === "facebook") {
    if ($("facebookAdsPanel")) $("facebookAdsPanel").classList.remove("hidden");
    if ($("renderProgressBlock")) $("renderProgressBlock").classList.add("hidden");
    if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.add("hidden");
    const st = jobSnapshots[id];
    if (st) renderFbAdsState(st);
  } else if (kind === "spark") {
    currentSparkJobId = id;
    const st = jobSnapshots[id] || lastFinishedSparkSt;
    if ($("sparkFetchPanel")) $("sparkFetchPanel").classList.remove("hidden");
    if ($("renderProgressBlock")) $("renderProgressBlock").classList.add("hidden");
    if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.add("hidden");
    if ($("facebookAdsPanel")) $("facebookAdsPanel").classList.add("hidden");
    if (st) renderSparkFetchState(st);
  } else if (kind === "provision") {
    currentProvisionDockSt = currentProvisionDockSt && currentProvisionDockSt.id === id
      ? currentProvisionDockSt
      : null;
    if ($("provisionProgressPanel")) $("provisionProgressPanel").classList.remove("hidden");
    if ($("renderProgressBlock")) $("renderProgressBlock").classList.add("hidden");
    if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.add("hidden");
    if ($("sparkFetchPanel")) $("sparkFetchPanel").classList.add("hidden");
    if ($("facebookAdsPanel")) $("facebookAdsPanel").classList.add("hidden");
    if (currentProvisionDockSt) renderProvisionDockState(currentProvisionDockSt);
  }
  refreshActiveJobsDock();
}

function wireActiveJobsDock() {
  if (_dockWired) return;
  _dockWired = true;

  const strip = $("dockStrip");
  const panel = $("progressPanel");
  const backdrop = $("dockBackdrop");
  const chips = $("dockChips");
  const actions = $("dockActions");

  if (panel) {
    setDockCollapsed(readDockCollapsedPref(), { persist: false });
  }

  const toggleDock = () => setDockCollapsed(!isDockCollapsed());

  if (strip) {
    strip.addEventListener("click", (ev) => {
      if (ev.target.closest(".dock-actions") || ev.target.closest(".dock-chips") || ev.target.closest(".dock-chip")) {
        return;
      }
      toggleDock();
    });
    strip.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggleDock();
      }
    });

    // Swipe-down to collapse (mobile sheet)
    let touchY0 = null;
    strip.addEventListener(
      "touchstart",
      (ev) => {
        if (!ev.touches[0]) return;
        touchY0 = ev.touches[0].clientY;
      },
      { passive: true }
    );
    strip.addEventListener(
      "touchend",
      (ev) => {
        if (touchY0 == null) return;
        const y = (ev.changedTouches[0] && ev.changedTouches[0].clientY) || touchY0;
        const dy = y - touchY0;
        touchY0 = null;
        if (dy > 56 && !isDockCollapsed()) setDockCollapsed(true);
      },
      { passive: true }
    );
  }

  if (actions) {
    actions.addEventListener("click", (ev) => ev.stopPropagation());
  }

  if (backdrop) {
    backdrop.addEventListener("click", () => setDockCollapsed(true));
  }

  document.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape" && !isDockCollapsed()) {
      const panelEl = $("progressPanel");
      if (panelEl && !panelEl.classList.contains("hidden")) {
        setDockCollapsed(true);
      }
    }
  });

  if (chips) {
    chips.addEventListener("click", (ev) => {
      ev.stopPropagation();
      const chip = ev.target.closest(".dock-chip[data-id]");
      if (!chip) return;
      focusDockJob(chip.dataset.kind, chip.dataset.id);
    });
  }

  $("dockDismiss")?.addEventListener("click", (ev) => {
    ev.stopPropagation();
    hideProgressDock();
  });
  $("cancelProvision")?.addEventListener("click", async (ev) => {
    ev.stopPropagation();
    const st = currentProvisionDockSt;
    const jobIds = [...new Set((st && st.jobIds) || [])];
    if (!jobIds.length) return;
    const btn = ev.currentTarget;
    btn.disabled = true;
    try {
      await Promise.all(jobIds.map((id) => window.api(`/api/provision/jobs/${encodeURIComponent(id)}/stop`, {
        method: "POST", body: "{}",
      })));
      if (typeof window.loadBcPage === "function") window.loadBcPage().catch(() => {});
    } catch (err) {
      alert(err.message || String(err));
    } finally {
      btn.disabled = false;
    }
  });
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// authHeaders / withAuth live in static/core.js
async function setAuthImage(img, path) {
  if (!img) return;
  if (!path) {
    if (window.setAuthSrc) await window.setAuthSrc(img, null);
    else {
      img.removeAttribute("src");
      img.classList.add("hidden");
    }
    return;
  }
  try {
    if (window.setAuthSrc) await window.setAuthSrc(img, path);
    else {
      const headers = await authHeaders();
      const res = await fetch(path, { headers });
      if (!res.ok) throw new Error(String(res.status));
      const blob = await res.blob();
      const obj = URL.createObjectURL(blob);
      if (img._authObjectUrl) {
        try { URL.revokeObjectURL(img._authObjectUrl); } catch { /* ignore */ }
      }
      img._authObjectUrl = obj;
      img.src = obj;
    }
    img.classList.remove("hidden");
  } catch {
    img.removeAttribute("src");
    img.classList.add("hidden");
  }
}

function loadStyle() {
  return { ...DEFAULT_STYLE };
}

function saveStyle() {
  try {
    window.dispatchEvent(new CustomEvent("captionToolStyleChanged"));
  } catch {}
}

function readUiState() {
  return {
    captions: $("captionsInput").value,
    urlInput: $("urlInput").value,
    preserveBreaks: $("preserveBreaks").checked,
    forceLower: $("forceLower").checked,
    mode: $("mode").value,
    nameTemplate: $("nameTemplate").value,
    skipExisting: $("skipExisting").checked,
    dedupe: $("dedupe").checked,
    strokePreset: $("strokePreset").value,
    captionMode: $("captionMode").value,
    positionPreset: $("positionPreset").value,
    previewSourceId: $("previewSource").value,
    previewCaptionIdx: $("previewCaption").value,
    sourceOrder: sources.map((s) => s.id),
  };
}

function saveUiState() {
  // Persistence is workspace PUT — keep call sites working.
  if (typeof scheduleWorkspaceSave === "function") scheduleWorkspaceSave();
}

function saveUiStateDebounced() {
  if (typeof scheduleWorkspaceSave === "function") scheduleWorkspaceSave();
}

function loadUiState() {
  return null;
}

function loadCropMap() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_CROPS));
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

function saveCropMap(map) {
  try {
    localStorage.setItem(LS_CROPS, JSON.stringify(map || {}));
  } catch {}
}

function rememberCropLocal(hash, crop) {
  if (!hash) return;
  const map = loadCropMap();
  if (crop && crop.enabled) {
    map[hash] = {
      enabled: true,
      x: Number(crop.x) || 0,
      y: Number(crop.y) || 0,
      w: Number(crop.w) || 1,
      h: Number(crop.h) || 1,
    };
  } else {
    delete map[hash];
  }
  saveCropMap(map);
}

function loadTrimMap() {
  try {
    const saved = JSON.parse(localStorage.getItem(LS_TRIMS));
    return saved && typeof saved === "object" ? saved : {};
  } catch {
    return {};
  }
}

function saveTrimMap(map) {
  try {
    localStorage.setItem(LS_TRIMS, JSON.stringify(map || {}));
  } catch {}
}

function rememberTrimLocal(hash, trim) {
  if (!hash) return;
  const map = loadTrimMap();
  if (trim) {
    map[hash] = { start: Number(trim.start) || 0, end: Number(trim.end) || 0 };
  } else {
    delete map[hash];
  }
  saveTrimMap(map);
}

function rememberedTrimLocal(hash) {
  if (!hash) return null;
  const t = loadTrimMap()[hash];
  if (!t) return null;
  return { start: Number(t.start) || 0, end: Number(t.end) || 0 };
}

function rememberedCropLocal(hash) {
  if (!hash) return null;
  const c = loadCropMap()[hash];
  if (!c || !c.enabled) return null;
  const x = clamp01(c.x);
  const y = clamp01(c.y);
  const w = Math.min(1 - x, Math.max(0, Number(c.w) || 0));
  const h = Math.min(1 - y, Math.max(0, Number(c.h) || 0));
  if (w < 0.02 || h < 0.02) return null;
  return { enabled: true, x, y, w, h };
}

/** Restore crops from localStorage onto sources missing them; mirror server crops into LS. */
async function syncCropsWithLocal(sourceList, { persistMissing = true } = {}) {
  if (!sourceList || !sourceList.length) return;
  for (const src of sourceList) {
    if (!src || !src.hash) continue;
    const active = getActiveCrop(src);
    if (active) {
      rememberCropLocal(src.hash, active);
      continue;
    }
    const remembered = rememberedCropLocal(src.hash);
    if (!remembered) continue;
    src.crop = remembered;
    if (!persistMissing) continue;
    try {
      const updated = await api(`/api/sources/${src.id}/crop`, {
        method: "PUT",
        body: JSON.stringify(remembered),
      });
      Object.assign(src, updated);
    } catch {}
  }
}

/** Restore trims from localStorage onto sources missing them. */
async function syncTrimsWithLocal(sourceList, { persistMissing = true } = {}) {
  if (!sourceList || !sourceList.length) return;
  for (const src of sourceList) {
    if (!src || !src.hash || !(src.duration > 0)) continue;
    if (src.trim && (Number(src.trim.start) > 0.05 || Number(src.trim.end) < src.duration - 0.05)) {
      rememberTrimLocal(src.hash, src.trim);
      continue;
    }
    const remembered = rememberedTrimLocal(src.hash);
    if (!remembered) continue;
    const start = Math.max(0, Number(remembered.start) || 0);
    const end = Math.min(src.duration, Number(remembered.end) || src.duration);
    if (start <= 0.05 && end >= src.duration - 0.05) continue;
    src.trim = { start, end };
    if (!persistMissing) continue;
    try {
      const updated = await api(`/api/sources/${src.id}/trim`, {
        method: "PUT",
        body: JSON.stringify(src.trim),
      });
      Object.assign(src, updated);
    } catch {}
  }
}

function applyUiState(ui) {
  if (!ui) return;
  restoring = true;
  try {
    if (typeof ui.captions === "string") $("captionsInput").value = ui.captions;
    if (typeof ui.urlInput === "string") $("urlInput").value = ui.urlInput;
    if (typeof ui.preserveBreaks === "boolean") $("preserveBreaks").checked = ui.preserveBreaks;
    if (typeof ui.forceLower === "boolean") $("forceLower").checked = ui.forceLower;
    if (ui.mode === "matrix" || ui.mode === "pair") $("mode").value = ui.mode;
    if (typeof ui.nameTemplate === "string" && ui.nameTemplate) {
      let tmpl = ui.nameTemplate;
      tmpl = tmpl.split("{caption01}").join("{caption-number}");
      $("nameTemplate").value = tmpl;
    }
    if (typeof ui.skipExisting === "boolean") $("skipExisting").checked = ui.skipExisting;
    if (typeof ui.dedupe === "boolean") $("dedupe").checked = ui.dedupe;
    if (ui.strokePreset) $("strokePreset").value = ui.strokePreset;
    if (ui.captionMode === "boxed" || ui.captionMode === "outline") {
      $("captionMode").value = ui.captionMode;
      style.captionMode = ui.captionMode;
    }
    if (ui.positionPreset) $("positionPreset").value = ui.positionPreset;
  } finally {
    restoring = false;
  }
}

function parseCaptionText(raw) {
  return String(raw || "")
    .split("\n")
    .map((s) => s.trim())
    .filter(Boolean);
}

function captionList() {
  if (isCreateCarouselMode()) {
    flushCarouselCaptionsFromStrip();
    migrateCarouselJobCaptionsIfNeeded();
    return parseCaptionText(carouselJobCaptionsText || "");
  }
  return parseCaptionText(($("captionsInput") && $("captionsInput").value) || "");
}

/** Global video-mode captions draft (carousel uses storyboard-level captions). */
let videoCaptionsText = "";
/** Captions for the whole carousel storyboard (each line = one multi-image post). */
let carouselJobCaptionsText = "";
/** @type {Record<string, string>} legacy per-image map — migrated into job captions */
let carouselCaptionsByImageId = {};
let carouselCaptionsFocusId = "";

function currentCarouselImageId() {
  const slides = getCarouselSlides();
  if (!slides.length) return "";
  const fromSel = $("previewCarouselImage") && $("previewCarouselImage").value;
  if (fromSel && slides.some((s) => s.id === fromSel)) return fromSel;
  return (slides[carouselSlideIndex] && slides[carouselSlideIndex].id) || "";
}

function flushCarouselCaptionsFromStrip() {
  if (!isCreateCarouselMode()) return;
  if (!$("captionsInput")) return;
  carouselJobCaptionsText = $("captionsInput").value;
}

function flushCaptionsStripToStore() {
  if (!$("captionsInput")) return;
  if (isCreateCarouselMode()) flushCarouselCaptionsFromStrip();
  else videoCaptionsText = $("captionsInput").value;
}

function migrateCarouselJobCaptionsIfNeeded() {
  if ((carouselJobCaptionsText || "").trim()) return;
  const focus = carouselCaptionsFocusId || currentCarouselImageId();
  if (focus && (carouselCaptionsByImageId[focus] || "").trim()) {
    carouselJobCaptionsText = carouselCaptionsByImageId[focus];
    return;
  }
  for (const img of getCarouselSlides()) {
    const t = carouselCaptionsByImageId[img.id];
    if ((t || "").trim()) {
      carouselJobCaptionsText = t;
      return;
    }
  }
}

function updateCaptionsContextHint() {
  const hint = $("captionsContextHint");
  if (!hint) return;
  if (!isCreateCarouselMode()) {
    hint.textContent = "One caption per line for the whole job.";
    return;
  }
  const slides = getCarouselSlides();
  const n = parseCaptionText(
    carouselJobCaptionsText || ($("captionsInput") && $("captionsInput").value) || ""
  ).length;
  if (!slides.length) {
    hint.textContent = "Add images to the storyboard, then write captions (one line = one carousel post).";
    return;
  }
  hint.textContent =
    `${slides.length} slide${slides.length === 1 ? "" : "s"} in this storyboard · ${n} caption line${n === 1 ? "" : "s"}` +
    ` (each line = one TikTok post with all ${slides.length} image${slides.length === 1 ? "" : "s"}).`;
}

function loadCaptionsStripForMode() {
  if (!$("captionsInput")) return;
  if (isCreateCarouselMode()) {
    migrateCarouselJobCaptionsIfNeeded();
    carouselCaptionsFocusId = currentCarouselImageId();
    $("captionsInput").value = carouselJobCaptionsText || "";
    $("captionsInput").placeholder =
      "One caption per line for this storyboard\n\neach line posts all slides as one carousel";
  } else {
    carouselCaptionsFocusId = "";
    $("captionsInput").value = videoCaptionsText;
    $("captionsInput").placeholder =
      "One caption per line\n\nmood bc that wallet pass be paying u $2k per week to play games 😁😂😏\nwhen the grind finally starts paying off 💸";
  }
  updateCaptionsContextHint();
}

function allCarouselCaptionPairs() {
  flushCarouselCaptionsFromStrip();
  migrateCarouselJobCaptionsIfNeeded();
  const slides = getCarouselSlides();
  if (!slides.length) return [];
  const slideIds = slides.map((s) => s.id).filter(Boolean);
  if (!slideIds.length) return [];
  const caps = parseCaptionText(carouselJobCaptionsText);
  const label =
    slides.length === 1
      ? slides[0].name || "image"
      : `${slides.length}-image carousel`;
  return caps.map((caption, i) => ({
    kind: "carousel",
    carouselId: `sb-${slideIds.join("-").slice(0, 48)}-${i + 1}`,
    sourceId: slideIds[0],
    slides: slideIds.slice(),
    caption,
    sourceName: label,
  }));
}

function settingsPayload() {
  return {
    ...style,
    preserveLineBreaks: $("preserveBreaks").checked,
    forceLowercase: $("forceLower").checked,
  };
}

function fmtDuration(sec) {
  const n = Math.max(0, Math.round(Number(sec) || 0));
  if (!n) return "";
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  if (h) return m ? `${h}h ${m}m` : `${h}h`;
  if (m >= 3) return `${m}m`;
  if (m) return s ? `${m}m ${s}s` : `${m}m`;
  return `${s}s`;
}

function fmtEtaLeft(sec) {
  const t = fmtDuration(sec);
  return t ? `about ${t} left` : "";
}

function estimateUploadSeconds(count, delaySec, engine) {
  const n = Math.max(0, Number(count) || 0);
  if (!n) return 0;
  const eng =
    engine ||
    ($("ttUploadEngine") && $("ttUploadEngine").value) ||
    "studio";
  const cfg = typeof config !== "undefined" && config ? config : {};
  const per =
    eng === "http"
      ? Number(cfg.tiktokHttpUploadSeconds) || 15
      : Number(cfg.tiktokUploadSeconds) || 55;
  const delay = Math.max(0, Number(delaySec) || 0);
  return n * per + Math.max(0, n - 1) * delay;
}

function bindPersist(ids, events = ["input", "change"]) {
  for (const id of ids) {
    const el = $(id);
    if (!el) continue;
    for (const ev of events) el.addEventListener(ev, saveUiStateDebounced);
  }
}

// ---------------------------------------------------------------------------
// Theme
// ---------------------------------------------------------------------------

function applyThemeChrome() {
  const theme = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = theme === "light" ? "#f1eadc" : "#09090b";
  document.querySelectorAll("[data-theme-set]").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.themeSet === theme);
  });
}

function initTheme() {
  const saved = localStorage.getItem(LS_THEME);
  if (saved) document.documentElement.dataset.theme = saved;
  applyThemeChrome();
  const toggle = () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem(LS_THEME, next);
    applyThemeChrome();
  };
  window.toggleAppTheme = toggle;
  if ($("themeToggle")) $("themeToggle").addEventListener("click", toggle);
  document.querySelectorAll("[data-theme-set]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.documentElement.dataset.theme = btn.dataset.themeSet;
      localStorage.setItem(LS_THEME, btn.dataset.themeSet);
      applyThemeChrome();
    });
  });
}

// ---------------------------------------------------------------------------
// App shell: sidebar + tab router
// ---------------------------------------------------------------------------

const PAGE_TITLES = {
  chat: "Chat",
  create: "Create",
  bcs: "Business Centers",
  sparks: "Sparks",
  settings: "Settings",
};

function currentPage() {
  const raw = (window.location.hash || "").replace(/^#\/?/, "");
  if (raw === "chat" || raw.startsWith("chat/")) return "chat";
  return PAGE_TITLES[raw] ? raw : "create";
}

function showPage(page) {
  const chatEl = document.querySelector('.page[data-page="chat"]');
  const chatWasOpen = !!(chatEl && !chatEl.classList.contains("hidden"));
  document.body.dataset.page = page;
  document.querySelectorAll(".page").forEach((sec) => {
    sec.classList.toggle("hidden", sec.dataset.page !== page);
  });
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.nav === page);
  });
  if ($("pageTitle")) $("pageTitle").textContent = PAGE_TITLES[page] || page;
  document.body.classList.remove("sidebar-open", "chat-usage-open");
  if ($("sidebarBackdrop")) $("sidebarBackdrop").classList.add("hidden");
  if ($("chatThreadsBackdrop")) $("chatThreadsBackdrop").classList.add("hidden");
  closeAllOverlays();
  if (page === "create") {
    // Library strip + plan name only — outputs load when Renders sheet opens.
    if (typeof refreshRecentMediaStrip === "function") refreshRecentMediaStrip().catch(() => {});
    if (typeof loadActiveWorkspacePlanName === "function") loadActiveWorkspacePlanName().catch(() => {});
  }
  if (page === "bcs" && typeof window.loadBcPage === "function") {
    window.loadBcPage().catch(() => {});
  } else if (typeof window.stopBcWatchers === "function") {
    window.stopBcWatchers();
  }
  if (page === "sparks" && typeof window.loadSparksPage === "function") {
    window.loadSparksPage().catch(() => {});
  }
  if (page === "chat" && typeof window.loadChatPage === "function") {
    // Do not leave a fresh Look change waiting in the debounce queue while Chat
    // starts a render plan. Chat also reads the live controls as a race-safe
    // fallback, but flushing here makes the shared workspace current too.
    cancelWorkspaceSave();
    saveWorkspaceRemote().catch(() => {});
    const hash = window.location.hash || "";
    const active = typeof window.getChatActiveThreadId === "function"
      ? window.getChatActiveThreadId()
      : "";
    const sameThread = active && hash.indexOf(active) !== -1;
    if (!(chatWasOpen && sameThread)) {
      window.loadChatPage().catch(() => {});
    }
  }
  if (page === "settings") {
    renderAiProvidersStatus();
    loadTtAccounts().catch(() => {});
    loadFacebookStatus().catch(() => {});
  }
}

function closeAllOverlays() {
  [
    "chatVideosModal",
    "chatImagesModal",
    "chatPreviewModal",
    "chatResultsModal",
    "chatCaptionPickModal",
    "chatMultipleModal",
    "chatOffersPanel",
    "videoLibraryModal",
    "cropModal",
    "cookieHelperModal",
    "remoteLoginModal",
    "fbAdsModal",
    "ttUploadSheet",
    "emojiPicker",
    "bcShotLightbox",
    "bcCreateModal",
    "bcFailModal",
    "bcAddressCreateModal",
    "bcVatCreateModal",
    "bcSparkCreateModal",
    "bcCardCreateModal",
    "bcHostCreateModal",
    "bcSuspendModal",
  ].forEach((id) => {
    const el = $(id);
    if (el) el.classList.add("hidden");
  });
  emojiOpen = false;
  if (typeof window.pauseChatPreviewMedia === "function") {
    window.pauseChatPreviewMedia();
  }
  if (typeof window.syncModalOpen === "function") window.syncModalOpen();
}
window.closeAllOverlays = closeAllOverlays;

function initNav() {
  document.querySelectorAll(".nav-item").forEach((btn) => {
    btn.addEventListener("click", () => {
      window.location.hash = "#/" + btn.dataset.nav;
      document.body.classList.remove("sidebar-open");
      if ($("sidebarBackdrop")) $("sidebarBackdrop").classList.add("hidden");
    });
  });
  window.addEventListener("hashchange", () => showPage(currentPage()));

  const syncSidebarChrome = () => {
    const collapsed = document.body.classList.contains("sidebar-collapsed");
    if ($("sidebarOpenBtn")) $("sidebarOpenBtn").classList.toggle("hidden", !collapsed);
  };
  const collapsed = localStorage.getItem("captionToolSidebar") === "collapsed";
  if (collapsed) document.body.classList.add("sidebar-collapsed");
  syncSidebarChrome();

  if ($("sidebarCollapse")) {
    $("sidebarCollapse").addEventListener("click", () => {
      document.body.classList.add("sidebar-collapsed");
      localStorage.setItem("captionToolSidebar", "collapsed");
      syncSidebarChrome();
      document.body.classList.remove("sidebar-open");
      if ($("sidebarBackdrop")) $("sidebarBackdrop").classList.add("hidden");
    });
  }
  if ($("sidebarOpenBtn")) {
    $("sidebarOpenBtn").addEventListener("click", () => {
      document.body.classList.remove("sidebar-collapsed");
      localStorage.setItem("captionToolSidebar", "open");
      syncSidebarChrome();
    });
  }

  if ($("hamburger")) {
    $("hamburger").addEventListener("click", () => {
      const open = document.body.classList.toggle("sidebar-open");
      $("sidebarBackdrop").classList.toggle("hidden", !open);
    });
  }
  if ($("sidebarBackdrop")) {
    $("sidebarBackdrop").addEventListener("click", () => {
      document.body.classList.remove("sidebar-open");
      $("sidebarBackdrop").classList.add("hidden");
    });
  }

  const profileBtn = $("profileMenuBtn");
  const profileMenu = $("profileMenu");
  const closeProfileMenu = () => {
    if (profileMenu) profileMenu.classList.add("hidden");
    if (profileBtn) profileBtn.setAttribute("aria-expanded", "false");
  };
  if (profileBtn && profileMenu) {
    profileBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = profileMenu.classList.toggle("hidden") === false;
      profileBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    profileMenu.addEventListener("click", (e) => {
      const item = e.target.closest("[data-profile-action]");
      if (!item) return;
      const act = item.dataset.profileAction;
      closeProfileMenu();
      if (act === "settings") window.location.hash = "#/settings";
      else if (act === "theme" && typeof window.toggleAppTheme === "function") {
        window.toggleAppTheme();
      } else if (act === "signout" && window.__auth && window.__auth.signOut) {
        window.__auth.signOut();
      }
    });
    document.addEventListener("mousedown", (e) => {
      if (!profileMenu.classList.contains("hidden")) {
        if (profileMenu.contains(e.target) || profileBtn.contains(e.target)) return;
        closeProfileMenu();
      }
    });
  }

  showPage(currentPage());
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      closeProfileMenu();
      closeAllOverlays();
    }
  });
}

// ---------------------------------------------------------------------------
// Sources: upload / URLs / list / drag-reorder
// ---------------------------------------------------------------------------

function isImageFile(file) {
  if (!file) return false;
  const t = (file.type || "").toLowerCase();
  if (t.startsWith("image/")) return true;
  return /\.(jpe?g|png|webp|gif|bmp)$/i.test(file.name || "");
}

function isVideoFile(file) {
  if (!file) return false;
  const t = (file.type || "").toLowerCase();
  if (t.startsWith("video/")) return true;
  return /\.(mp4|mov|webm|mkv|avi|m4v)$/i.test(file.name || "");
}

function initDropZone() {
  $("sourceList")?.addEventListener("dragover", onListDragOver);
  const zone = $("dropZone");
  const input = $("fileInput");
  if (!zone || !input) return;
  zone.addEventListener("click", () => input.click());
  zone.addEventListener("keydown", (e) => { if (e.key === "Enter") input.click(); });
  input.addEventListener("change", () => {
    ingestDroppedFiles([...input.files]);
    input.value = "";
  });
  ["dragenter", "dragover"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.add("drag"); }));
  ["dragleave", "drop"].forEach((ev) =>
    zone.addEventListener(ev, (e) => { e.preventDefault(); zone.classList.remove("drag"); }));
  zone.addEventListener("drop", (e) => ingestDroppedFiles([...e.dataTransfer.files]));
  refreshImageLibrary().catch(() => {});
}

async function ingestDroppedFiles(files) {
  const list = (files || []).filter(Boolean);
  if (!list.length) return;
  const images = list.filter(isImageFile);
  const videos = list.filter((f) => isVideoFile(f) || (!isImageFile(f) && !isVideoFile(f)));
  // Unknown extensions: try as video upload first (existing behavior).
  if (videos.length) await uploadFiles(videos);
  if (images.length) await uploadImageFiles(images);
}

function upsertSource(src) {
  if (!src || !src.id) return;
  const i = sources.findIndex((s) => s.id === src.id);
  if (i >= 0) sources[i] = src;
  else sources.push(src);
}

async function uploadFiles(files) {
  const list = [...(files || [])].filter(Boolean);
  if (!list.length) return;
  const status = $("urlStatus");
  const limit = 3;
  let i = 0;
  let failed = 0;

  async function one(file) {
    if (status) status.textContent = `Uploading ${file.name}…`;
    try {
      const fd = new FormData();
      fd.append("file", file);
      const src = await api("/api/sources/upload", { method: "POST", body: fd });
      upsertSource(src);
      await syncCropsWithLocal([src]);
      await syncTrimsWithLocal([src]);
      renderSources();
      saveUiState();
    } catch (err) {
      failed += 1;
      if (status) {
        status.innerHTML =
          `<span class="err">${escapeHtml(file.name)}: ${escapeHtml(err.message)}</span>`;
      }
    }
  }

  const workers = Array.from({ length: Math.min(limit, list.length) }, async () => {
    while (i < list.length) {
      const file = list[i++];
      await one(file);
    }
  });
  await Promise.all(workers);
  refreshLibraryGrids();
  if (status && !failed) status.textContent = "";
}

async function uploadImageFiles(files) {
  const list = [...(files || [])].filter(Boolean);
  if (!list.length) return;
  const status = $("imageLibraryStatus") || $("urlStatus");
  const limit = 4;
  let i = 0;
  let failed = 0;

  async function one(file) {
    if (status) status.textContent = `Uploading ${file.name}…`;
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api("/api/images/upload", { method: "POST", body: fd });
    } catch (err) {
      failed += 1;
      if (status) {
        status.innerHTML =
          `<span class="err">${escapeHtml(file.name)}: ${escapeHtml(err.message)}</span>`;
      }
    }
  }

  const workers = Array.from({ length: Math.min(limit, list.length) }, async () => {
    while (i < list.length) {
      const file = list[i++];
      await one(file);
    }
  });
  await Promise.all(workers);
  if (status && !failed) status.textContent = "";
  await refreshImageLibrary();
}

function initUrls() {
  if (!$("addUrls") || !$("urlInput")) return;
  $("addUrls").addEventListener("click", async () => {
    const urls = $("urlInput").value.split("\n").map((s) => s.trim()).filter(Boolean);
    if (!urls.length) return;
    await addSourceUrls(urls, $("urlStatus"));
    $("urlInput").value = "";
  });
}

async function addSourceUrls(urls, statusEl, opts = {}) {
  const addToJob = opts.addToJob !== false;
  const lines = [];
  const render = () => {
    if (statusEl) statusEl.innerHTML = lines.join("<br>");
  };
  for (const url of urls) {
    const v = await api("/api/sources/validate-url", {
      method: "POST", body: JSON.stringify({ url }),
    });
    lines.push(v.ok
      ? `<span class="ok">✓ ${escapeHtml(v.platform)}</span> ${escapeHtml(url)}`
      : `<span class="err">✗ ${escapeHtml(v.error)}</span>`);
  }
  render();
  for (let i = 0; i < urls.length; i++) {
    if (lines[i].includes("err")) continue;
    lines[i] = `⬇ downloading… ${escapeHtml(urls[i])}`;
    render();
    try {
      const src = await api("/api/sources/url", {
        method: "POST",
        body: JSON.stringify({ url: urls[i], addToJob }),
      });
      if (addToJob) {
        upsertSource(src);
        await syncCropsWithLocal([src]);
        await syncTrimsWithLocal([src]);
        renderSources();
      }
      lines[i] = `<span class="ok">✓ added</span> ${escapeHtml(src.name)}`;
      refreshLibraryGrids();
    } catch (err) {
      lines[i] = `<span class="err">✗ ${escapeHtml(err.message)}</span> ${escapeHtml(urls[i])}`;
    }
    render();
  }
  if (addToJob) saveUiState();
}
window.addSourceUrls = addSourceUrls;

async function refreshImageLibrary(opts = {}) {
  const render = opts.render !== false;
  try {
    if (!_imagesInflight) {
      _imagesInflight = api("/api/images")
        .then((res) => {
          imageLibraryCache = (res && res.items) || [];
          return imageLibraryCache;
        })
        .finally(() => {
          _imagesInflight = null;
        });
    }
    const items = await _imagesInflight;
    if (render) {
      renderImageLibraryGrid(items);
      if (typeof refreshCarouselStoryboard === "function") refreshCarouselStoryboard();
      else if (typeof syncCarouselStage === "function") syncCarouselStage();
      if (typeof paintRecentMediaStrip === "function") paintRecentMediaStrip();
      updateCreateLibraryLabels();
    }
    return items;
  } catch {
    return imageLibraryCache || [];
  }
}

function renderImageLibraryGrid(items) {
  const grid = $("imageLibraryGrid");
  if (!grid) return;
  const list = Array.isArray(items) ? items : [];
  const block = grid.closest(".create-storyboard-block");
  if (block) block.classList.toggle("is-empty", !list.length);
  grid.classList.remove("hidden");
  grid.classList.toggle("is-empty", !list.length);
  const activeId = typeof currentCarouselImageId === "function" ? currentCarouselImageId() : "";
  if (!list.length) {
    grid.innerHTML = `<div class="image-lib-empty-hint">Drop images or open Pinterest to build slides</div>`;
    return;
  }
  grid.innerHTML = list
    .slice(0, 60)
    .map((img, i) => {
      const on = img.id === activeId;
      return (
        `<div class="image-lib-tile${on ? " is-stage-slide" : ""}" draggable="true" data-id="${escapeHtml(img.id)}" data-index="${i}" title="${escapeHtml(img.name || "Slide")} — drag to reorder">` +
        `<img alt="" draggable="false" loading="lazy" decoding="async" data-auth-src="${escapeHtml(img.fileUrl || img.thumbUrl || "")}" />` +
        `<button type="button" class="image-lib-del" draggable="false" data-image-del="${escapeHtml(img.id)}" title="Remove from storyboard" aria-label="Remove">✕</button>` +
        `</div>`
      );
    })
    .join("");
  if (typeof hydrateAuthMedia === "function") hydrateAuthMedia(grid);
}

function updateCreateLibraryLabels() {
  const hint = $("createSourcesHint");
  if (!hint) return;
  if (isCreateCarouselMode()) {
    hint.textContent = "Carousel slides — drag to reorder";
  } else {
    hint.textContent = "In this project — drag to reorder";
  }
}

async function removeCarouselImage(imageId) {
  const id = String(imageId || "").trim();
  if (!id) return;
  // Optimistic UI — stale caches / double-clicks should not block remove.
  delete carouselCaptionsByImageId[id];
  if (carouselCaptionsFocusId === id) carouselCaptionsFocusId = "";
  imageLibraryCache = (imageLibraryCache || []).filter((it) => it && it.id !== id);
  renderImageLibraryGrid(imageLibraryCache);
  if (typeof refreshCarouselStoryboard === "function") refreshCarouselStoryboard();
  if (typeof paintRecentMediaStrip === "function") paintRecentMediaStrip();
  if (typeof updateCount === "function") updateCount();
  if (typeof scheduleWorkspaceSave === "function") scheduleWorkspaceSave();
  try {
    await api(`/api/images/${encodeURIComponent(id)}`, { method: "DELETE" });
  } catch (err) {
    const msg = String(err && err.message || err || "");
    // Already gone on the server — local remove is enough.
    if (/not found/i.test(msg)) return;
    alert(msg);
    refreshImageLibrary().catch(() => {});
    return;
  }
  // Reconcile with server in the background.
  refreshImageLibrary().catch(() => {});
}

async function removeAllCarouselImages() {
  const items = (imageLibraryCache || []).filter((it) => it && it.id);
  if (!items.length) return;
  if (!confirm(`Remove all ${items.length} storyboard image${items.length === 1 ? "" : "s"}?`)) return;
  const ids = items.map((it) => it.id);
  const failed = [];
  for (const id of ids) {
    try {
      await api(`/api/images/${encodeURIComponent(id)}`, { method: "DELETE" });
      delete carouselCaptionsByImageId[id];
    } catch {
      failed.push(id);
    }
  }
  carouselCaptionsFocusId = "";
  if (failed.length) {
    imageLibraryCache = (imageLibraryCache || []).filter((it) => it && failed.includes(it.id));
    alert(`Removed ${ids.length - failed.length}. ${failed.length} could not be deleted.`);
  } else {
    imageLibraryCache = [];
  }
  renderImageLibraryGrid(imageLibraryCache);
  if (typeof refreshCarouselStoryboard === "function") refreshCarouselStoryboard();
  if (typeof paintRecentMediaStrip === "function") paintRecentMediaStrip();
  if (typeof updateCount === "function") updateCount();
  if (typeof scheduleWorkspaceSave === "function") scheduleWorkspaceSave();
}

async function persistImageLibraryOrder(ids) {
  const cleaned = (ids || []).map((id) => String(id || "").trim()).filter(Boolean);
  if (!cleaned.length) return;
  const byId = Object.fromEntries((imageLibraryCache || []).filter((it) => it && it.id).map((it) => [it.id, it]));
  imageLibraryCache = cleaned.map((id) => byId[id]).filter(Boolean);
  renderImageLibraryGrid(imageLibraryCache);
  if (typeof refreshCarouselStoryboard === "function") refreshCarouselStoryboard();
  try {
    const res = await api("/api/images/order", {
      method: "PUT",
      body: JSON.stringify({ ids: cleaned }),
    });
    if (res && Array.isArray(res.items)) {
      imageLibraryCache = res.items;
      renderImageLibraryGrid(imageLibraryCache);
      if (typeof refreshCarouselStoryboard === "function") refreshCarouselStoryboard();
    }
  } catch (err) {
    alert(err.message || String(err));
    refreshImageLibrary().catch(() => {});
  }
}

function initImageLibraryGridDnD() {
  const grid = $("imageLibraryGrid");
  if (!grid || grid.dataset.dndBound === "1") return;
  grid.dataset.dndBound = "1";
  let dragId = "";

  const isDel = (el) => !!(el && el.closest && el.closest(".image-lib-del, [data-image-del]"));

  grid.addEventListener("pointerdown", (e) => {
    if (!isDel(e.target)) return;
    e.stopPropagation();
  });

  grid.addEventListener("mousedown", (e) => {
    if (!isDel(e.target)) return;
    e.stopPropagation();
  });

  grid.addEventListener("dragstart", (e) => {
    if (isDel(e.target)) {
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    const tile = e.target.closest(".image-lib-tile[data-id]");
    if (!tile) {
      e.preventDefault();
      return;
    }
    dragId = tile.getAttribute("data-id") || "";
    tile.classList.add("is-dragging");
    try {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", dragId);
    } catch {}
  });

  grid.addEventListener("dragend", () => {
    dragId = "";
    grid.classList.remove("is-drag-over");
    grid.querySelectorAll(".image-lib-tile").forEach((el) => {
      el.classList.remove("is-dragging", "is-drop-target");
    });
  });

  grid.addEventListener("dragover", (e) => {
    e.preventDefault();
    grid.classList.add("is-drag-over");
    const tile = e.target.closest(".image-lib-tile[data-id]");
    grid.querySelectorAll(".image-lib-tile.is-drop-target").forEach((el) => {
      if (el !== tile) el.classList.remove("is-drop-target");
    });
    if (tile && tile.getAttribute("data-id") !== dragId) tile.classList.add("is-drop-target");
  });

  grid.addEventListener("dragleave", (e) => {
    if (!grid.contains(e.relatedTarget)) grid.classList.remove("is-drag-over");
  });

  grid.addEventListener("drop", (e) => {
    e.preventDefault();
    grid.classList.remove("is-drag-over");
    const fromId = dragId || (e.dataTransfer && e.dataTransfer.getData("text/plain")) || "";
    const toTile = e.target.closest(".image-lib-tile[data-id]");
    if (!fromId || !toTile) return;
    const toId = toTile.getAttribute("data-id");
    if (!toId || toId === fromId) return;
    const ids = (imageLibraryCache || []).map((it) => it && it.id).filter(Boolean);
    const from = ids.indexOf(fromId);
    const to = ids.indexOf(toId);
    if (from < 0 || to < 0) return;
    ids.splice(from, 1);
    ids.splice(to, 0, fromId);
    persistImageLibraryOrder(ids).catch((err) => alert(err.message || String(err)));
  });

  grid.addEventListener("click", (e) => {
    const del = e.target.closest(".image-lib-del, [data-image-del]");
    if (del) {
      e.preventDefault();
      e.stopPropagation();
      const id = del.getAttribute("data-image-del") || del.closest("[data-id]")?.getAttribute("data-id");
      if (id) {
        removeCarouselImage(id).catch((err) => alert(err.message || String(err)));
      }
      return;
    }
    const tile = e.target.closest(".image-lib-tile[data-id]");
    if (!tile) return;
    if (!isCreateCarouselMode()) setCreateMode("carousel");
    syncCarouselStage({ preferId: tile.getAttribute("data-id") });
  });
}

function wireRemoveAllStoryboardBtn() {
  const btn = $("removeAllStoryboardBtn");
  if (!btn || btn.dataset.bound === "1") return;
  btn.dataset.bound = "1";
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    removeAllCarouselImages().catch((err) => alert(err.message || String(err)));
  });
}

function isCarouselRenderPair(p) {
  return !!(p && (p.kind === "carousel" || (Array.isArray(p.slides) && p.slides.length)));
}

/** True when every video pair still points at a source currently on Create. */
function workspaceRenderPairsSourcesOk(pairs = workspaceRenderPairs) {
  if (!pairs || !pairs.length) return false;
  const sourceIds = new Set((sources || []).map((s) => s && s.id).filter(Boolean));
  let sawVideo = false;
  for (const p of pairs) {
    if (isCarouselRenderPair(p)) continue;
    sawVideo = true;
    const sid = String(p.sourceId || "").trim();
    if (!sid || !sourceIds.has(sid)) return false;
  }
  return sawVideo || pairs.some(isCarouselRenderPair);
}

function clearStaleWorkspaceRenderPairs() {
  if (!workspaceRenderPairs.length) return false;
  if (workspaceRenderPairsSourcesOk(workspaceRenderPairs)) return false;
  workspaceRenderPairs = [];
  clearRenderPairsOnSave = true;
  syncCustomModeOption();
  return true;
}

function customPairsActive() {
  // Plan / Chat pairs drive Create when present — no "Custom" mode in Create UI.
  // Drop pairs that still reference removed/replaced sources (stale plan lock).
  clearStaleWorkspaceRenderPairs();
  return workspaceRenderPairsSourcesOk(workspaceRenderPairs);
}

function syncCustomModeOption() {
  const modeEl = $("mode");
  if (!modeEl) return;
  // Create settings only expose matrix / pair.
  if (modeEl.value === "custom") modeEl.value = "matrix";
  const opt = $("modeCustomOption");
  if (opt) opt.hidden = true;
}

let pinterestPlayingCard = null;
let hlsLibPromise = null;

function loadHlsLib() {
  if (window.Hls) return Promise.resolve(window.Hls);
  if (hlsLibPromise) return hlsLibPromise;
  hlsLibPromise = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = "https://cdn.jsdelivr.net/npm/hls.js@1.5.20/dist/hls.min.js";
    s.async = true;
    s.onload = () => (window.Hls ? resolve(window.Hls) : reject(new Error("Hls missing")));
    s.onerror = () => reject(new Error("Could not load video preview"));
    document.head.appendChild(s);
  });
  return hlsLibPromise;
}

function stopPinterestPreview(card) {
  if (!card) return;
  if (card._hoverTimer) {
    clearTimeout(card._hoverTimer);
    card._hoverTimer = null;
  }
  if (card._hls) {
    try { card._hls.destroy(); } catch {}
    card._hls = null;
  }
  const vid = card.querySelector("video");
  if (vid) {
    vid.pause();
    vid.removeAttribute("src");
    vid.load();
    vid.remove();
  }
  card.classList.remove("playing");
  if (pinterestPlayingCard === card) pinterestPlayingCard = null;
}

function pinterestMediaSrc(raw, token) {
  if (!raw) return "";
  let src = "/api/pinterest/media?u=" + encodeURIComponent(raw);
  if (token) src += "&auth=" + encodeURIComponent(token);
  return src;
}

function pinterestPreviewSrc(pin, token) {
  const raw = pin.previewUrl || "";
  if (!raw) return { src: "", kind: "" };
  const kind = pin.previewKind || (/\.m3u8(\?|$)/i.test(raw) ? "hls" : "mp4");
  return { src: pinterestMediaSrc(raw, token), kind: kind === "hls" ? "hls" : "mp4" };
}

async function playPinterestPreview(card, pin) {
  const auth = window.__auth;
  const token = auth && (auth.getToken ? await auth.getToken() : auth.token);
  const { src, kind } = pinterestPreviewSrc(pin, token);
  if (!src) return;
  if (pinterestPlayingCard && pinterestPlayingCard !== card) {
    stopPinterestPreview(pinterestPlayingCard);
  }
  pinterestPlayingCard = card;
  const media = card.querySelector(".pinterest-card-media");
  if (!media) return;
  let vid = card.querySelector("video");
  if (!vid) {
    vid = document.createElement("video");
    vid.muted = true;
    vid.defaultMuted = true;
    vid.loop = true;
    vid.playsInline = true;
    vid.setAttribute("muted", "");
    vid.setAttribute("playsinline", "");
    vid.preload = "auto";
    vid.referrerPolicy = "no-referrer";
    media.appendChild(vid);
  }
  const start = () => {
    vid.play().then(() => {
      if (pinterestPlayingCard === card) card.classList.add("playing");
    }).catch(() => {});
  };
  if (kind === "hls") {
    try {
      const Hls = await loadHlsLib();
      if (pinterestPlayingCard !== card) return;
      if (Hls.isSupported()) {
        const hls = new Hls({
          enableWorker: false,
          startLevel: 0,
          capLevelToPlayerSize: true,
          maxBufferLength: 8,
        });
        card._hls = hls;
        hls.loadSource(src);
        hls.attachMedia(vid);
        hls.on(Hls.Events.MANIFEST_PARSED, start);
        hls.on(Hls.Events.ERROR, (_ev, data) => {
          if (data && data.fatal && pinterestPlayingCard === card) {
            stopPinterestPreview(card);
          }
        });
        return;
      }
    } catch {
      card.classList.remove("playing");
      return;
    }
    const native = vid.canPlayType("application/vnd.apple.mpegurl");
    if (native) {
      vid.src = src;
      start();
      return;
    }
  }
  vid.src = src;
  start();
}

function bindPinterestHover(card, pin) {
  if (!pin.previewUrl) return;
  card.addEventListener("pointerenter", () => {
    if (card._hoverTimer) clearTimeout(card._hoverTimer);
    card._hoverTimer = setTimeout(() => {
      playPinterestPreview(card, pin).catch(() => {});
    }, 80);
  });
  card.addEventListener("pointerleave", () => stopPinterestPreview(card));
}

function renderPinterestResults(gridEl, pins, selectedSet, opts) {
  if (!gridEl) return;
  const onChange = opts && typeof opts.onChange === "function" ? opts.onChange : null;
  if (pinterestPlayingCard && !gridEl.contains(pinterestPlayingCard)) {
    stopPinterestPreview(pinterestPlayingCard);
  }
  if ((pins || []).some((p) => (p.previewKind || "") === "hls" || /\.m3u8(\?|$)/i.test(p.previewUrl || ""))) {
    loadHlsLib().catch(() => {});
  }
  gridEl.innerHTML = "";
  (pins || []).forEach((pin) => {
    const url = pin.url || pin.pinUrl || "";
    const card = document.createElement("button");
    card.type = "button";
    const landscape = Number(pin.width) > Number(pin.height);
    card.className = "pinterest-card" + (selectedSet.has(url) ? " selected" : "")
      + (landscape ? " landscape" : "");
    card.dataset.url = url;
    const dur = pin.duration ? fmtDuration(pin.duration) : "";
    card.innerHTML = `
      <span class="pinterest-card-media${landscape ? " is-landscape" : ""}">
        <img alt="" />
      </span>
      <span class="pinterest-card-title"></span>
      <span class="pinterest-card-meta"></span>`;
    const img = card.querySelector("img");
    if (pin.thumbnail) img.src = pin.thumbnail;
    card.querySelector(".pinterest-card-title").textContent = pin.title || url;
    card.querySelector(".pinterest-card-meta").textContent =
      dur || ((pin.kind || pin.previewKind) === "image" ? "image" : "video");
    card.addEventListener("click", () => {
      if (selectedSet.has(url)) selectedSet.delete(url);
      else selectedSet.add(url);
      const on = selectedSet.has(url);
      card.classList.toggle("selected", on);
      if (onChange) onChange(pin, on, selectedSet);
    });
    bindPinterestHover(card, pin);
    gridEl.appendChild(card);
  });
}

async function downloadPinUrls(urls, statusEl, opts = {}) {
  const addToJob = opts.addToJob !== false;
  let added = 0;
  let failed = 0;
  for (const url of urls) {
    if (statusEl) statusEl.innerHTML = `⬇ downloading… ${escapeHtml(url)}`;
    try {
      const src = await api("/api/sources/url", {
        method: "POST",
        body: JSON.stringify({ url, addToJob }),
      });
      if (addToJob) {
        upsertSource(src);
        await syncCropsWithLocal([src]);
        await syncTrimsWithLocal([src]);
        renderSources();
      }
      if (statusEl) statusEl.innerHTML = `<span class="ok">✓ added${addToJob ? "" : " to library"}</span> ${escapeHtml(src.name)}`;
      added++;
    } catch (err) {
      failed++;
      if (statusEl) statusEl.innerHTML = `<span class="err">${escapeHtml(err.message)}</span> ${escapeHtml(url)}`;
    }
  }
  if (added) await refreshLibraryGrids();
  if (statusEl && urls.length > 1) {
    statusEl.textContent = `${added} video(s) added${addToJob ? "" : " to library"}${failed ? `, ${failed} failed` : ""}`;
  }
  if (addToJob) saveUiState();
  return { added, failed };
}

function bindPinterestSearchPanel(cfg) {
  const q = $(cfg.queryId);
  const btn = $(cfg.btnId);
  if (!q || !btn) return;
  const selected = new Set();
  let pins = [];
  let searchedMediaType = "video";
  const mediaTypeOf = () => {
    if (typeof cfg.mediaType === "function") return cfg.mediaType() || "video";
    if (cfg.mediaType) return cfg.mediaType;
    return ($("pinterestMediaType") && $("pinterestMediaType").value) || "video";
  };
  const run = async () => {
    const query = q.value.trim();
    if (!query) return;
    const status = cfg.statusId ? $(cfg.statusId) : null;
    const grid = cfg.gridId ? $(cfg.gridId) : null;
    const addRow = cfg.addRowId ? $(cfg.addRowId) : null;
    const mediaType = mediaTypeOf();
    if (status) status.textContent = "Searching Pinterest…";
    selected.clear();
    try {
      const res = await api("/api/pinterest/search", {
        method: "POST",
        body: JSON.stringify({ query, limit: cfg.limit || 12, mediaType }),
      });
      pins = res.pins || [];
      searchedMediaType = mediaType;
      const kind = mediaType === "image" ? "image" : "video";
      if (status) {
        status.textContent = pins.length
          ? `${pins.length} ${kind} pin(s)`
          : `No ${kind} pins found`;
      }
      if (grid) grid.classList.toggle("hidden", !pins.length);
      if (addRow) addRow.classList.toggle("hidden", !pins.length);
      const libraryBtn = cfg.libraryBtnId ? $(cfg.libraryBtnId) : null;
      if (libraryBtn) libraryBtn.classList.toggle("hidden", !pins.length || mediaType !== "video");
      if (grid) renderPinterestResults(grid, pins, selected);
      if (typeof cfg.onResults === "function") cfg.onResults(pins);
    } catch (err) {
      pins = [];
      if (status) {
        status.innerHTML = `<span class="err">${escapeHtml(err.message || String(err))}</span>`;
      }
      if (grid) grid.classList.add("hidden");
      if (addRow) addRow.classList.add("hidden");
    }
  };
  btn.addEventListener("click", () => run().catch(() => {}));
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      run().catch(() => {});
    }
  });
  const addBtn = cfg.addBtnId ? $(cfg.addBtnId) : null;
  if (addBtn) {
    addBtn.addEventListener("click", async () => {
      const mediaType = searchedMediaType;
      const chosen = pins.filter((p) => selected.has(p.url || p.pinUrl));
      if (typeof cfg.onAddSelected === "function") {
        await cfg.onAddSelected(chosen, mediaType);
      }
    });
  }
  const libraryBtn = cfg.libraryBtnId ? $(cfg.libraryBtnId) : null;
  if (libraryBtn) {
    libraryBtn.addEventListener("click", async () => {
      if (searchedMediaType !== "video") return;
      const chosen = pins.filter((p) => selected.has(p.url || p.pinUrl));
      const status = cfg.statusId ? $(cfg.statusId) : null;
      if (!chosen.length) {
        if (status) status.textContent = "Select pins first";
        return;
      }
      libraryBtn.disabled = true;
      try {
        await downloadPinUrls(chosen.map((p) => p.url || p.pinUrl).filter(Boolean), status, { addToJob: false });
      } finally {
        libraryBtn.disabled = false;
      }
    });
  }
}

function initPinterestSearch() {
  bindPinterestSearchPanel({
    queryId: "pinterestQuery",
    btnId: "pinterestSearchBtn",
    statusId: "pinterestSearchStatus",
    gridId: "pinterestResults",
    addRowId: "pinterestAddRow",
    addBtnId: "pinterestAddSelected",
    libraryBtnId: "pinterestAddToLibrary",
    mediaType: () => ($("pinterestMediaType") && $("pinterestMediaType").value) || "video",
    onAddSelected: async (selectedPins, mediaType) => {
      if (!selectedPins.length) return;
      if (mediaType === "image") {
        for (const pin of selectedPins) {
          const imageUrl = pin.imageUrl || pin.previewUrl || "";
          if (!imageUrl) continue;
          if ($("pinterestSearchStatus")) {
            $("pinterestSearchStatus").innerHTML = `⬇ ${escapeHtml(pin.title || pin.url)}`;
          }
          try {
            await api("/api/images/from-url", {
              method: "POST",
              body: JSON.stringify({
                url: imageUrl,
                pinUrl: pin.url || pin.pinUrl,
                title: pin.title || "",
              }),
            });
          } catch (err) {
            if ($("pinterestSearchStatus")) {
              $("pinterestSearchStatus").innerHTML = `<span class="err">${escapeHtml(err.message)}</span>`;
            }
          }
        }
        await refreshImageLibrary();
        if ($("pinterestSearchStatus")) {
          $("pinterestSearchStatus").innerHTML = `<span class="ok">✓ images added</span>`;
        }
        return;
      }
      const urls = selectedPins.map((p) => p.url).filter(Boolean);
      await downloadPinUrls(urls, $("pinterestSearchStatus"));
    },
  });
}

function isPinterestSource(src) {
  if (!src) return false;
  if (src.platform === "pinterest") return true;
  const blob = `${src.name || ""} ${src.stem || ""} ${src.url || ""}`.toLowerCase();
  return blob.includes("pinterest");
}

function pinterestSourcesInJob() {
  return sources.filter(isPinterestSource);
}

function updateSourceListActions() {
  const row = $("sourceListActions");
  if (!row) return;
  const n = pinterestSourcesInJob().length;
  row.classList.toggle("hidden", n === 0);
  const btn = $("removePinterestSources");
  if (btn) {
    btn.textContent = n > 1 ? `Remove all Pinterest (${n})` : "Remove all Pinterest";
  }
}

async function unlinkSourcesFromJob(ids) {
  const idSet = new Set((ids || []).filter(Boolean));
  if (!idSet.size) return;
  await Promise.all([...idSet].map(async (id) => {
    try {
      await api(`/api/sources/${id}`, { method: "DELETE" });
    } catch (err) {
      console.error(`Failed to delete source ${id}:`, err);
    }
  }));
  sources = sources.filter((s) => !idSet.has(s.id));
  if (selectedSourceId && idSet.has(selectedSourceId)) {
    selectedSourceId = null;
    hideVideoDetail();
  }
  if (clearStaleWorkspaceRenderPairs()) scheduleWorkspaceSave();
  renderSources();
  saveUiState();
  refreshLibraryGrids();
  if (typeof window.renderChatSourceList === "function") window.renderChatSourceList();
}

function initSourceListActions() {
  const btn = $("removePinterestSources");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const pins = pinterestSourcesInJob();
    if (!pins.length) return;
    btn.disabled = true;
    try {
      await unlinkSourcesFromJob(pins.map((s) => s.id));
    } finally {
      btn.disabled = false;
    }
  });
}

function renderSources() {
  const list = $("sourceList");
  if (!list) return;
  list.innerHTML = "";
  sources.forEach((src, idx) => {
    const li = document.createElement("li");
    li.className = "source-item" + (src.id === selectedSourceId ? " selected" : "");
    li.draggable = true;
    li.dataset.idx = idx;
    li.dataset.id = src.id;
    const endOn = src.endCaption && src.endCaption.enabled && (src.endCaption.text || "").trim();
    const cropOn = !!(src.crop && src.crop.enabled);
    li.innerHTML = `
      <span class="grip">⠿</span>
      <span class="name"></span>
      ${src.duplicateOf ? '<span class="dup">duplicate</span>' : ""}
      <span class="meta">${endOn ? '<span class="end-badge" title="End caption on">END</span>' : ""}${cropOn ? '<span class="crop-badge" title="Crop on">CROP</span>' : ""}</span>
      <button class="end-cfg" type="button" title="End-of-video caption">End caption</button>
      <button class="remove" type="button" title="Remove from sources (stays in library)">✕</button>
    `;
    li.querySelector(".name").textContent = src.name || "";
    li.querySelector(".name").title = src.name || "";
    li.querySelector(".meta").append(
      document.createTextNode(`${src.width}×${src.height} · ${fmtDuration(src.duration)}`)
    );
    li.querySelector(".remove").addEventListener("click", async (e) => {
      e.stopPropagation();
      await unlinkSourcesFromJob([src.id]);
    });
    li.querySelector(".end-cfg").addEventListener("click", (e) => {
      e.stopPropagation();
      openVideoDetail(src.id);
    });
    li.addEventListener("click", (e) => {
      if (e.target.closest(".remove") || e.target.closest(".grip") || e.target.closest(".end-cfg")) return;
      selectSourceForPreview(src.id);
    });
    li.addEventListener("dragstart", () => li.classList.add("dragging"));
    li.addEventListener("dragend", () => {
      li.classList.remove("dragging");
      persistSourceOrder();
    });
    list.appendChild(li);
  });

  if (selectedSourceId && !sources.some((s) => s.id === selectedSourceId)) {
    selectedSourceId = null;
    hideVideoDetail();
  } else if (selectedSourceId) {
    // Keep form synced; only reopen drawer if it is already open.
    const drawerOpen = $("endCaptionDrawer") && !$("endCaptionDrawer").classList.contains("hidden");
    if (drawerOpen) openVideoDetail(selectedSourceId);
    else {
      const src = sources.find((s) => s.id === selectedSourceId);
      if (src) fillVideoDetail(src);
    }
  } else {
    hideVideoDetail();
  }

  updateSourceListActions();
  updateSourcesEmptyState();
  updateCount();
  updatePreviewSelectors();
  saveUiStateDebounced();
  if (typeof paintRecentMediaStrip === "function") paintRecentMediaStrip();
}

function updateSourcesEmptyState() {
  const empty = $("createSourcesEmpty");
  const list = $("sourceList");
  const n = (sources || []).length;
  if (empty) empty.classList.toggle("hidden", n > 0);
  if (list) list.classList.toggle("is-empty", n === 0);
}

function updateJobTabBadge() {
  updateSourcesEmptyState();
}

function setCreateJobOpen(_open) {
  /* Job popover removed — sources live in the left Sources panel */
}

function onListDragOver(e) {
  e.preventDefault();
  const dragging = document.querySelector(".source-item.dragging");
  if (!dragging) return;
  const siblings = [...e.currentTarget.querySelectorAll(".source-item:not(.dragging)")];
  const next = siblings.find((s) => e.clientY < s.getBoundingClientRect().top + s.offsetHeight / 2);
  e.currentTarget.insertBefore(dragging, next || null);
  // sync order back to `sources`
  const order = [...e.currentTarget.querySelectorAll(".source-item")].map(
    (el) => sources[Number(el.dataset.idx)]
  );
  if (order.every(Boolean)) {
    sources = order;
    [...e.currentTarget.querySelectorAll(".source-item")].forEach((el, i) => (el.dataset.idx = i));
  }
}

async function persistSourceOrder() {
  saveUiState();
  try {
    await api("/api/sources/order", {
      method: "PUT",
      body: JSON.stringify({ sourceIds: sources.map((s) => s.id) }),
    });
  } catch {}
}

// ---------------------------------------------------------------------------
// Video library picker (Create popup + Chat tab)
// ---------------------------------------------------------------------------

const LIB_PLATFORM_LABEL = {
  upload: "Upload",
  pinterest: "Pinterest",
  tiktok: "TikTok",
  youtube: "YouTube",
  url: "Link",
};

let libraryCache = [];
let imageLibraryCache = [];
let _libraryCacheInflight = null;
let _outputsInflight = null;
let _imagesInflight = null;
let libraryFilter = { create: "all", chat: "all" };
/** Create-library modal: ids selected for the job (applied only on Done). */
let libraryCreateSelection = new Set();
let recentMediaStripReq = 0;

// Authenticated media used to be converted to a fresh blob URL on every
// render, which bypassed the browser cache.  Keep content-addressed posters
// and tiny hover clips in Cache Storage instead.  The cache is per signed-in
// user and bounded so it stays fast over long-running libraries.
const LIB_MEDIA_CACHE_VERSION = 3;
const LIB_MEDIA_CACHE_LIMIT = 320;
const libraryMediaUrls = new Map();
const libraryMediaInflight = new Map();
let libraryMediaWrites = 0;
let libraryMediaNetworkActive = 0;
const libraryMediaNetworkQueue = [];

async function withLibraryMediaNetworkSlot(fn) {
  if (libraryMediaNetworkActive >= 3) {
    await new Promise((resolve) => libraryMediaNetworkQueue.push(resolve));
  }
  libraryMediaNetworkActive += 1;
  try {
    return await fn();
  } finally {
    libraryMediaNetworkActive -= 1;
    const next = libraryMediaNetworkQueue.shift();
    if (next) next();
  }
}

function libraryMediaCacheName() {
  const auth = window.__auth || {};
  const uid = (auth.user && auth.user.uid) || (auth.mode === "dev" ? "dev" : "signed-in");
  return `video-library-media-v${LIB_MEDIA_CACHE_VERSION}-${String(uid).replace(/[^a-z0-9_-]/gi, "_")}`;
}

async function pruneLibraryMediaCache(cache) {
  try {
    const keys = await cache.keys();
    const excess = keys.length - LIB_MEDIA_CACHE_LIMIT;
    if (excess > 0) {
      await Promise.all(keys.slice(0, excess).map((key) => cache.delete(key)));
    }
  } catch {}
}

async function cachedLibraryMediaUrl(path) {
  if (!path) return "";
  if (libraryMediaUrls.has(path)) return libraryMediaUrls.get(path);
  if (libraryMediaInflight.has(path)) return libraryMediaInflight.get(path);
  const work = (async () => {
    let response = null;
    let cache = null;
    if ("caches" in window) {
      try {
        cache = await caches.open(libraryMediaCacheName());
        response = await cache.match(path);
      } catch {}
    }
    if (!response) {
      const fresh = await withLibraryMediaNetworkSlot(async () => {
        const headers = await authHeaders();
        return fetch(path, { headers, credentials: "same-origin" });
      });
      if (!fresh.ok) throw new Error(`Preview failed (${fresh.status})`);
      response = fresh;
      if (cache) {
        try {
          await cache.put(path, fresh.clone());
          libraryMediaWrites += 1;
          if (libraryMediaWrites === 1 || libraryMediaWrites % 24 === 0) {
            pruneLibraryMediaCache(cache);
          }
        } catch {}
      }
    }
    const blob = await response.blob();
    const objectUrl = URL.createObjectURL(blob);
    libraryMediaUrls.set(path, objectUrl);
    return objectUrl;
  })();
  libraryMediaInflight.set(path, work);
  try {
    return await work;
  } finally {
    libraryMediaInflight.delete(path);
  }
}
window.cachedLibraryMediaUrl = cachedLibraryMediaUrl;

function libraryAssetPath(item, kind) {
  const version = encodeURIComponent(item.assetVersion || item.hash || item.id || "1");
  if (kind === "hover") {
    const base = item.hoverUrl || `/api/sources/${item.id}/hover`;
    return `${base}${base.includes("?") ? "&" : "?"}v=${version}`;
  }
  // Explicit generation fixes old library rows whose frame existed only on
  // the pre-Cloudflare machine. This fetch is asynchronous and browser-cached.
  const base = item.thumbUrl || `/api/sources/${item.id}/frame`;
  return `${base}${base.includes("?") ? "&" : "?"}generate=1&v=${version}`;
}

function stopLibraryHover(tile) {
  tile._libraryHovering = false;
  if (tile._libraryHoverTimer) clearTimeout(tile._libraryHoverTimer);
  tile._libraryHoverTimer = 0;
  const video = tile.querySelector(".video-lib-hover");
  if (video) {
    video.pause();
    video.remove();
  }
  tile.classList.remove("is-playing");
}

function bindLibraryMedia(tile, img, item) {
  const posterPath = libraryAssetPath(item, "poster");
  const hoverPath = libraryAssetPath(item, "hover");
  const loadPoster = async () => {
    if (img.dataset.loaded) return;
    img.dataset.loaded = "1";
    try {
      img.src = await cachedLibraryMediaUrl(posterPath);
      tile.classList.remove("thumb-missing");
      const idle = window.requestIdleCallback || ((fn) => setTimeout(fn, 250));
      idle(() => cachedLibraryMediaUrl(hoverPath).catch(() => {}));
    } catch {
      tile.classList.add("thumb-missing");
    }
  };
  if ("IntersectionObserver" in window) {
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer.disconnect();
        loadPoster();
      }
    }, { rootMargin: "320px" });
    observer.observe(tile);
  } else {
    loadPoster();
  }
  tile.addEventListener("pointerenter", () => {
    tile._libraryHovering = true;
    tile._libraryHoverTimer = setTimeout(async () => {
      try {
        const src = await cachedLibraryMediaUrl(hoverPath);
        if (!tile._libraryHovering || !tile.isConnected) return;
        let video = tile.querySelector(".video-lib-hover");
        if (!video) {
          video = document.createElement("video");
          video.className = "video-lib-hover";
          video.muted = true;
          video.defaultMuted = true;
          video.loop = true;
          video.playsInline = true;
          video.preload = "auto";
          tile.querySelector(".video-lib-thumb")?.prepend(video);
        }
        video.src = src;
        await video.play();
        tile.classList.add("is-playing");
      } catch {}
    }, 90);
  });
  tile.addEventListener("pointerleave", () => stopLibraryHover(tile));
}

function libraryPlatform(item) {
  return item.platform || (item.origin === "upload" ? "upload" : "url");
}

function matchesLibraryFilter(item, filter) {
  if (!filter || filter === "all") return true;
  return libraryPlatform(item) === filter;
}

function updateLibrarySelCount() {
  const el = $("videoLibrarySelCount");
  const btn = $("videoLibraryDone");
  if (!el) return;
  const n = libraryCreateSelection.size;
  el.textContent = n ? `${n} selected for this job` : "None selected";
  if (btn) btn.textContent = n ? `Done · ${n} selected` : "Done";
}

function toggleLibraryCreateSelection(item, tile) {
  const id = item && item.id;
  if (!id) return;
  if (libraryCreateSelection.has(id)) {
    libraryCreateSelection.delete(id);
    if (tile) tile.classList.remove("selected", "in-job");
  } else {
    libraryCreateSelection.add(id);
    if (tile) {
      tile.classList.add("selected");
      tile.classList.remove("in-job");
    }
  }
  updateLibrarySelCount();
}

function renderLibraryGrid(container, items, filterKey) {
  if (!container) return;
  const filter = libraryFilter[filterKey] || "all";
  const visible = (items || []).filter((it) => matchesLibraryFilter(it, filter))
    .sort((a, b) => (Number(b.addedAt) || 0) - (Number(a.addedAt) || 0));
  container.innerHTML = "";
  if (!visible.length) {
    const empty = document.createElement("div");
    empty.className = "video-lib-empty";
    empty.textContent = items && items.length
      ? "No videos in this filter."
      : "No uploaded or downloaded videos yet.";
    container.appendChild(empty);
    return;
  }
  for (const item of visible) {
    const tile = document.createElement("div");
    const selected = filterKey === "create"
      ? libraryCreateSelection.has(item.id)
      : !!(window.isChatAttachmentQueued && window.isChatAttachmentQueued(item.id));
    tile.className =
      "video-lib-tile" +
      (selected ? (filterKey === "create" ? " selected" : " in-job") : "");
    tile.dataset.id = item.id;
    const plat = libraryPlatform(item);
    const label = LIB_PLATFORM_LABEL[plat] || plat;
    tile.innerHTML = `
      <div class="video-lib-thumb">
        <img alt="" />
        <span class="video-lib-badge"></span>
        ${selected ? '<span class="video-lib-added">' + (filterKey === "create" ? "Selected" : "Added") + "</span>" : ""}
      </div>
      <span class="video-lib-name"></span>
      <span class="video-lib-meta"></span>
      <button type="button" class="video-lib-del" title="Permanently delete from library">✕</button>
    `;
    tile.querySelector(".video-lib-badge").textContent = label;
    tile.querySelector(".video-lib-name").textContent = item.name || "video";
    tile.querySelector(".video-lib-name").title = item.name || "";
    tile.title = filterKey === "chat"
      ? (selected ? "Click to remove from this chat" : "Click to attach to this chat")
      : selected
        ? "Click to deselect"
        : "Click to select for this job";
    tile.querySelector(".video-lib-meta").textContent =
      `${item.width || "?"}×${item.height || "?"} · ${fmtDuration(item.duration)}`;
    const img = tile.querySelector("img");
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = () => {
      img.removeAttribute("src");
      tile.classList.add("thumb-missing");
    };
    bindLibraryMedia(tile, img, item);
    tile.addEventListener("click", (e) => {
      if (e.target.closest(".video-lib-del")) return;
      if (filterKey === "chat") {
        if (window.isChatAttachmentQueued && window.isChatAttachmentQueued(item.id)) {
          window.unqueueChatAttachment?.({ sourceId: item.id });
          return;
        }
        if (typeof window.queueChatAttachment === "function") {
          window.queueChatAttachment({
            sourceId: item.id,
            optimisticAttachment: {
              kind: "source",
              sourceId: item.id,
              name: item.name || "video",
              thumbUrl: libraryAssetPath(item, "poster"),
              width: item.width || 0,
              height: item.height || 0,
              duration: item.duration || 0,
            },
          }).catch(() => {});
        }
        return;
      }
      // Instant local toggle — job is updated only when Done is pressed.
      toggleLibraryCreateSelection(item, tile);
      const on = libraryCreateSelection.has(item.id);
      const badge = tile.querySelector(".video-lib-added");
      if (on && !badge) {
        const span = document.createElement("span");
        span.className = "video-lib-added";
        span.textContent = "Selected";
        tile.querySelector(".video-lib-thumb").appendChild(span);
      } else if (!on && badge) {
        badge.remove();
      }
      tile.title = on ? "Click to deselect" : "Click to select for this job";
    });
    tile.querySelector(".video-lib-del").addEventListener("click", (e) => {
      e.stopPropagation();
      purgeLibraryItem(item).catch((err) => alert(err.message || String(err)));
    });
    container.appendChild(tile);
  }
  if (filterKey === "create") updateLibrarySelCount();
}

function syncChatVideoLibrarySelection() {
  const grid = $("chatLibraryGrid");
  if (!grid) return;
  grid.querySelectorAll(".video-lib-tile[data-id]").forEach((tile) => {
    const on = !!(window.isChatAttachmentQueued && window.isChatAttachmentQueued(tile.dataset.id));
    tile.classList.toggle("in-job", on);
    tile.title = on ? "Click to remove from this chat" : "Click to attach to this chat";
    let badge = tile.querySelector(".video-lib-added");
    if (on && !badge) {
      badge = document.createElement("span");
      badge.className = "video-lib-added";
      tile.querySelector(".video-lib-thumb")?.appendChild(badge);
    }
    if (badge) {
      if (on) badge.textContent = "Added";
      else badge.remove();
    }
  });
}
window.syncChatVideoLibrarySelection = syncChatVideoLibrarySelection;

async function loadLibraryCache() {
  if (_libraryCacheInflight) return _libraryCacheInflight;
  _libraryCacheInflight = (async () => {
    let items = [];
    try {
      const raw = await api("/api/library");
      items = Array.isArray(raw) ? raw : (raw && raw.items) || [];
    } catch {}
    const job = ((window.getSources && window.getSources()) || sources || []);
    const byId = Object.fromEntries(items.filter((it) => it && it.id).map((it) => [it.id, it]));
    for (const src of job) {
      if (!src || !src.id) continue;
      if (byId[src.id]) byId[src.id] = { ...byId[src.id], inJob: true };
      else byId[src.id] = { ...src, inJob: true };
    }
    libraryCache = Object.values(byId);
    return libraryCache;
  })();
  try {
    return await _libraryCacheInflight;
  } finally {
    _libraryCacheInflight = null;
  }
}

async function refreshLibraryGrids() {
  const libraryDrawer = $("libraryDrawer");
  const createOpen = libraryDrawer && !libraryDrawer.classList.contains("hidden");
  const chatLib = $("chatVideosLibraryPane");
  const chatOpen = chatLib && !chatLib.classList.contains("hidden");
  if (!createOpen && !chatOpen) return;
  try {
    await loadLibraryCache();
  } catch {
    return;
  }
  if (createOpen) renderLibraryGrid($("videoLibraryGrid"), libraryCache, "create");
  if (chatOpen) renderLibraryGrid($("chatLibraryGrid"), libraryCache, "chat");
}

async function reloadJobSources() {
  const list = await api("/api/sources");
  sources = list;
  await syncCropsWithLocal(sources);
  await syncTrimsWithLocal(sources);
  if (clearStaleWorkspaceRenderPairs()) scheduleWorkspaceSave();
  renderSources();
  saveUiState();
  if (window.renderChatSourceList) window.renderChatSourceList();
  if (typeof refreshRecentMediaStrip === "function") refreshRecentMediaStrip().catch(() => {});
}

async function applyLibraryCreateSelection() {
  const libIds = new Set(libraryCache.map((it) => it && it.id).filter(Boolean));
  const jobIds = new Set((sources || []).map((s) => s && s.id).filter(Boolean));
  const want = libraryCreateSelection;
  const toAdd = [...want].filter((id) => libIds.has(id) && !jobIds.has(id));
  const toRemove = [...jobIds].filter((id) => libIds.has(id) && !want.has(id));
  if (!toAdd.length && !toRemove.length) return;
  const btn = $("videoLibraryDone");
  if (btn) {
    btn.disabled = true;
    btn.textContent = "Updating…";
  }
  try {
    const res = await api("/api/library/apply", {
      method: "POST",
      body: JSON.stringify({ addIds: toAdd, removeIds: toRemove }),
    });
    if (Array.isArray(res && res.sources)) {
      sources = res.sources;
      await syncCropsWithLocal(sources.filter((s) => toAdd.includes(s.id)));
      await syncTrimsWithLocal(sources.filter((s) => toAdd.includes(s.id)));
      if (clearStaleWorkspaceRenderPairs()) scheduleWorkspaceSave();
      renderSources();
      saveUiState();
      if (window.renderChatSourceList) window.renderChatSourceList();
      if (typeof refreshRecentMediaStrip === "function") {
        refreshRecentMediaStrip().catch(() => {});
      }
    } else {
      await reloadJobSources();
    }
    const idSet = new Set(toRemove);
    if (selectedSourceId && idSet.has(selectedSourceId)) {
      selectedSourceId = null;
      hideVideoDetail();
    }
    for (const id of toAdd) {
      const row = libraryCache.find((it) => it && it.id === id);
      if (row) row.inJob = true;
      if (typeof window.queueChatAttachment === "function") {
        window.queueChatAttachment({ sourceId: id }).catch(() => {});
      }
    }
    for (const id of toRemove) {
      const row = libraryCache.find((it) => it && it.id === id);
      if (row) row.inJob = false;
      if (typeof window.unqueueChatAttachment === "function") {
        window.unqueueChatAttachment({ sourceId: id });
      }
    }
  } finally {
    if (btn) {
      btn.disabled = false;
      updateLibrarySelCount();
    }
  }
}

async function purgeLibraryItem(item) {
  const name = item.name || "this video";
  if (!confirm(`Permanently delete ${name} from the library?`)) return;
  await api(`/api/library/${item.id}`, { method: "DELETE" });
  libraryCreateSelection.delete(item.id);
  sources = sources.filter((s) => s.id !== item.id);
  if (selectedSourceId === item.id) {
    selectedSourceId = null;
    hideVideoDetail();
  }
  renderSources();
  saveUiState();
  if (window.renderChatSourceList) window.renderChatSourceList();
  await refreshLibraryGrids();
}

function setLibraryFilter(key, value) {
  libraryFilter[key] = value;
  const root = document.querySelector(`[data-lib-filters="${key}"]`);
  if (root) {
    root.querySelectorAll(".video-lib-filter").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.libFilter === value);
    });
  }
  const gridId = key === "chat" ? "chatLibraryGrid" : "videoLibraryGrid";
  renderLibraryGrid($(gridId), libraryCache, key);
}

async function openVideoLibraryModal() {
  openCreateDrawer("library");
  const grid = $("videoLibraryGrid");
  const imgGrid = $("createImageLibraryGrid");
  if (isCreateCarouselMode()) {
    if (imgGrid) {
      if (imageLibraryCache.length) renderCreateImageLibraryGrid(imageLibraryCache);
      else imgGrid.innerHTML = `<div class="video-lib-empty">Loading library…</div>`;
    }
    try {
      await refreshImageLibrary();
      renderCreateImageLibraryGrid(imageLibraryCache);
    } catch (err) {
      if (imgGrid && !imageLibraryCache.length) {
        imgGrid.innerHTML =
          `<div class="video-lib-empty">${escapeHtml(err.message || String(err))}</div>`;
      }
    }
    return;
  }
  // Instant paint from cache, then refresh in background.
  if (libraryCache.length && grid) {
    libraryCreateSelection = new Set(
      libraryCache.filter((it) => it && it.inJob && it.id).map((it) => it.id)
    );
    renderLibraryGrid(grid, libraryCache, "create");
  } else if (grid) {
    grid.innerHTML = `<div class="video-lib-empty">Loading library…</div>`;
  }
  try {
    await loadLibraryCache();
    libraryCreateSelection = new Set(
      libraryCache.filter((it) => it && it.inJob && it.id).map((it) => it.id)
    );
    renderLibraryGrid(grid, libraryCache, "create");
    if (typeof paintRecentMediaStrip === "function") paintRecentMediaStrip();
  } catch (err) {
    if (grid && !libraryCache.length) {
      grid.innerHTML =
        `<div class="video-lib-empty">${escapeHtml(err.message || String(err))}</div>`;
    }
  }
}

function closeVideoLibraryModal({ apply = false } = {}) {
  const finish = async () => {
    const modal = $("videoLibraryModal");
    // Close the drawer immediately so Done never feels stuck; apply is one
    // batch request and updates the job list when it returns.
    closeCreateDrawer("library");
    if (modal) modal.classList.add("hidden");
    if (!apply) return;
    try {
      await applyLibraryCreateSelection();
    } catch (err) {
      alert(err.message || String(err));
      updateLibrarySelCount();
    }
  };
  finish();
}

function renderCreateImageLibraryGrid(items) {
  const grid = $("createImageLibraryGrid");
  if (!grid) return;
  const filterRoot = document.querySelector('[data-image-lib-filters="create"]');
  const filter =
    (filterRoot && filterRoot.querySelector(".video-lib-filter.active")?.dataset.imageLibFilter) ||
    "all";
  let list = Array.isArray(items) ? items.filter((it) => it && it.id) : [];
  if (filter && filter !== "all") {
    list = list.filter((it) => String(it.origin || "").toLowerCase() === filter);
  }
  if (!list.length) {
    grid.innerHTML = `<div class="video-lib-empty">No images yet — drop files or search Pinterest.</div>`;
    return;
  }
  grid.innerHTML = list
    .map((img) => {
      const thumb = withAuth(img.thumbUrl || img.fileUrl || `/api/images/${img.id}/file`);
      return (
        `<button type="button" class="video-lib-tile" data-create-image-id="${escapeHtml(img.id)}" title="${escapeHtml(img.name || "")}">` +
        `<img alt="" loading="lazy" decoding="async" src="${escapeHtml(thumb)}" />` +
        `<span class="video-lib-badge">${escapeHtml(img.origin || "upload")}</span>` +
        `<span class="video-lib-name">${escapeHtml(img.name || "image")}</span>` +
        `</button>`
      );
    })
    .join("");
}

async function showChatLibraryTab() {
  const grid = $("chatLibraryGrid");
  // Cache-first paint (same as Create modal) — avoid blank "Loading…" when warm.
  if (libraryCache.length && grid) {
    renderLibraryGrid(grid, libraryCache, "chat");
  } else if (grid) {
    grid.innerHTML = `<div class="video-lib-empty">Loading library…</div>`;
  }
  try {
    await loadLibraryCache();
    renderLibraryGrid(grid, libraryCache, "chat");
  } catch (err) {
    if (grid && !libraryCache.length) {
      grid.innerHTML =
        `<div class="video-lib-empty">${escapeHtml(err.message || String(err))}</div>`;
    }
  }
}

function initVideoLibrary() {
  $("openLibraryBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openVideoLibraryModal();
  });
  $("browseVideoLibraryBtn")?.addEventListener("click", () => openVideoLibraryModal());
  const done = $("videoLibraryDone");
  if (done) {
    done.addEventListener("click", () => closeVideoLibraryModal({ apply: true }));
  }
  ["videoLibraryClose", "videoLibraryBackdrop", "libraryDrawerClose"].forEach((id) => {
    const el = $(id);
    if (el) el.addEventListener("click", () => closeVideoLibraryModal({ apply: false }));
  });
  document.querySelectorAll("[data-lib-filters]").forEach((root) => {
    root.addEventListener("click", (e) => {
      const btn = e.target.closest(".video-lib-filter");
      if (!btn) return;
      setLibraryFilter(root.dataset.libFilters, btn.dataset.libFilter);
    });
  });
  document.querySelectorAll('[data-image-lib-filters="create"]').forEach((root) => {
    root.addEventListener("click", (e) => {
      const btn = e.target.closest(".video-lib-filter");
      if (!btn) return;
      root.querySelectorAll(".video-lib-filter").forEach((b) => {
        b.classList.toggle("active", b === btn);
      });
      renderCreateImageLibraryGrid(imageLibraryCache);
    });
  });
  $("createImageLibraryGrid")?.addEventListener("click", (e) => {
    const tile = e.target.closest("[data-create-image-id]");
    if (!tile) return;
    const id = tile.getAttribute("data-create-image-id");
    if (!id) return;
    if (!isCreateCarouselMode()) setCreateMode("carousel");
    syncCarouselStage({ preferId: id });
    closeCreateDrawer("library");
  });
}

function setCreateLibraryPanelOpen(_open) {
  /* Library panel replaced by always-on Sources */
}

function toggleCreateLibraryPanel() {
  openVideoLibraryModal();
}

function isCreateLibraryPanelOpen() {
  return false;
}

// ---------------------------------------------------------------------------
// Captions
// ---------------------------------------------------------------------------

function initCaptions() {
  if (!$("captionsInput")) return;
  $("captionsInput").addEventListener("input", () => {
    if (isCreateCarouselMode()) {
      flushCarouselCaptionsFromStrip();
      updateCaptionsContextHint();
    } else {
      videoCaptionsText = $("captionsInput").value;
    }
    updateCount();
    updatePreviewSelectors({
      previewCarouselImageId: currentCarouselImageId(),
      previewCaptionIdx: $("previewCaption") && $("previewCaption").value,
      previewSourceId: $("previewSource") && $("previewSource").value,
    });
    saveUiStateDebounced();
    scheduleWorkspaceSave();
  });
  $("importCaptions").addEventListener("click", () => $("captionFile").click());
  $("captionFile").addEventListener("change", async () => {
    const file = $("captionFile").files[0];
    if (!file) return;
    let text = await file.text();
    if (file.name.toLowerCase().endsWith(".csv")) {
      // take first column, handle quoted cells
      text = text.split(/\r?\n/).map((row) => {
        const m = row.match(/^"((?:[^"]|"")*)"|^([^,]*)/);
        return m ? (m[1] !== undefined ? m[1].replace(/""/g, '"') : m[2]) : row;
      }).join("\n");
    }
    const existing = $("captionsInput").value.trim();
    $("captionsInput").value = (existing ? existing + "\n" : "") + text.trim();
    $("captionFile").value = "";
    if (isCreateCarouselMode()) flushCarouselCaptionsFromStrip();
    else videoCaptionsText = $("captionsInput").value;
    updateCount();
    updatePreviewSelectors();
    saveUiState();
  });
  $("preserveBreaks").addEventListener("change", () => { saveUiState(); refreshOverlay(); });
  $("forceLower").addEventListener("change", () => { saveUiState(); refreshOverlay(); });
}

// ---------------------------------------------------------------------------
// Emoji picker (Apple-style)
// ---------------------------------------------------------------------------

function insertAtCaret(el, text) {
  if (!el) return;
  const start = Number.isInteger(emojiSel.start) ? emojiSel.start : (el.selectionStart ?? el.value.length);
  const end = Number.isInteger(emojiSel.end) ? emojiSel.end : (el.selectionEnd ?? el.value.length);
  const before = el.value.slice(0, start);
  const after = el.value.slice(end);
  el.value = before + text + after;
  const pos = start + text.length;
  emojiSel = { start: pos, end: pos };
  try {
    el.focus();
    el.setSelectionRange(pos, pos);
  } catch {}
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

function rememberEmojiCaret() {
  const el = emojiTargetId ? $(emojiTargetId) : null;
  if (!el || el.selectionStart == null) return;
  emojiSel = { start: el.selectionStart, end: el.selectionEnd };
}

/** Convert emoji char(s) → Twemoji CDN code (always paints, even when OS font is missing glyphs). */
function emojiToTwemojiCode(emoji) {
  return Array.from(emoji)
    .map((c) => c.codePointAt(0).toString(16))
    .filter((c) => c !== "fe0f")
    .join("-");
}

function twemojiImg(emoji, cls = "emoji-img") {
  const code = emojiToTwemojiCode(emoji);
  if (!code) return emoji;
  const src = `https://cdn.jsdelivr.net/gh/jdecked/twemoji@15.1.0/assets/72x72/${code}.png`;
  // Escape for HTML attribute
  const alt = emoji.replace(/"/g, "");
  return `<img class="${cls}" src="${src}" alt="${alt}" loading="lazy" draggable="false" />`;
}

function initEmojiPicker() {
  const grid = $("emojiGrid");
  const cats = $("emojiCats");
  const search = $("emojiSearch");
  const picker = $("emojiPicker");
  if (!grid || !cats || !search || !picker) {
    console.error("Emoji picker DOM missing");
    return;
  }

  const data = window.EMOJI_DATA;
  if (!data || !data.cats || !data.items) {
    console.error("EMOJI_DATA failed to load — check /emoji-data.js");
    document.addEventListener("click", (e) => {
      const btn = e.target.closest("[data-emoji-target]");
      if (!btn) return;
      e.preventDefault();
      alert("Emoji list failed to load. Hard-refresh the page (Ctrl+F5).");
    });
    return;
  }

  cats.innerHTML = data.cats.map((c) =>
    `<button type="button" class="emoji-cat${c.id === emojiCat ? " active" : ""}" data-cat="${c.id}" title="${c.label}">${twemojiImg(c.icon)}</button>`
  ).join("");

  cats.addEventListener("click", (e) => {
    const btn = e.target.closest(".emoji-cat");
    if (!btn) return;
    emojiCat = btn.dataset.cat;
    cats.querySelectorAll(".emoji-cat").forEach((b) => b.classList.toggle("active", b.dataset.cat === emojiCat));
    search.value = "";
    renderEmojiGrid();
  });

  search.addEventListener("input", () => renderEmojiGrid());

  document.addEventListener("selectionchange", () => {
    if (!emojiOpen) return;
    const el = emojiTargetId ? $(emojiTargetId) : null;
    if (el && document.activeElement === el) rememberEmojiCaret();
  });

  grid.addEventListener("mousedown", (e) => { e.preventDefault(); });
  grid.addEventListener("click", (e) => {
    const cell = e.target.closest(".emoji-cell");
    if (!cell) return;
    const el = emojiTargetId ? $(emojiTargetId) : null;
    if (el) insertAtCaret(el, cell.dataset.emoji);
  });

  // Event delegation so the button always works.
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-emoji-target]");
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const target = btn.getAttribute("data-emoji-target");
    if (emojiOpen && emojiTargetId === target) {
      closeEmojiPicker();
      return;
    }
    openEmojiPicker(btn, target);
  });

  document.addEventListener("pointerdown", (e) => {
    if (!emojiOpen) return;
    // Ignore the same gesture that opened the picker.
    if (Date.now() - emojiOpenedAt < 200) return;
    if (e.target.closest("#emojiPicker")) return;
    if (e.target.closest("[data-emoji-target]")) return;
    closeEmojiPicker();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && emojiOpen) closeEmojiPicker();
  });
  window.addEventListener("resize", () => {
    if (emojiOpen) {
      positionEmojiPicker(document.querySelector(`[data-emoji-target="${emojiTargetId}"]`));
    }
  });

  function renderEmojiGrid() {
    const q = (search.value || "").trim().toLowerCase();
    let list = [];
    if (q) {
      for (const arr of Object.values(data.items)) {
        for (const row of arr) {
          const ch = row[0];
          const kw = row[1] || "";
          if (kw.includes(q) || ch.includes(q)) list.push(row);
        }
      }
    } else {
      list = data.items[emojiCat] || [];
    }
    if (!list.length) {
      grid.innerHTML = `<div class="emoji-empty">No matches</div>`;
      return;
    }
    const seen = new Set();
    const cells = [];
    for (const row of list) {
      const ch = row[0];
      if (seen.has(ch)) continue;
      seen.add(ch);
      const label = (row[1] || "").split(" ")[0] || ch;
      cells.push(
        `<button type="button" class="emoji-cell" data-emoji="${ch}" title="${label}">${twemojiImg(ch)}</button>`
      );
      if (cells.length >= 400) break;
    }
    grid.innerHTML = cells.join("");
  }

  initEmojiPicker.renderGrid = renderEmojiGrid;
  renderEmojiGrid();
}

function openEmojiPicker(anchorBtn, targetId) {
  try {
    emojiTargetId = targetId;
    emojiOpen = true;
    emojiOpenedAt = Date.now();
    const el = $(targetId);
    if (el) {
      try { el.focus({ preventScroll: true }); } catch { el.focus(); }
      emojiSel = {
        start: el.selectionStart ?? el.value.length,
        end: el.selectionEnd ?? el.value.length,
      };
    }
    document.querySelectorAll(".emoji-trigger").forEach((b) => b.classList.remove("active"));
    if (anchorBtn) anchorBtn.classList.add("active");
    const picker = $("emojiPicker");
    if (!picker) return;
    picker.classList.remove("hidden");
    picker.style.left = "12px";
    picker.style.top = "12px";
    if (initEmojiPicker.renderGrid) {
      $("emojiSearch").value = "";
      initEmojiPicker.renderGrid();
    }
    positionEmojiPicker(anchorBtn);
    setTimeout(() => {
      try { $("emojiSearch").focus({ preventScroll: true }); } catch { $("emojiSearch").focus(); }
    }, 0);
  } catch (err) {
    console.error("openEmojiPicker failed", err);
    alert("Emoji picker error: " + err.message);
  }
}

function positionEmojiPicker(anchorBtn) {
  const picker = $("emojiPicker");
  if (!picker) return;
  const pw = picker.offsetWidth || 340;
  const ph = picker.offsetHeight || 380;
  let left = 24;
  let top = 24;
  if (anchorBtn) {
    const r = anchorBtn.getBoundingClientRect();
    left = r.left;
    top = r.bottom + 8;
    if (left + pw > window.innerWidth - 12) left = window.innerWidth - pw - 12;
    if (left < 12) left = 12;
    if (top + ph > window.innerHeight - 12) top = Math.max(12, r.top - ph - 8);
  }
  picker.style.left = `${Math.round(left)}px`;
  picker.style.top = `${Math.round(top)}px`;
}

function closeEmojiPicker() {
  emojiOpen = false;
  emojiTargetId = null;
  const picker = $("emojiPicker");
  if (picker) picker.classList.add("hidden");
  document.querySelectorAll(".emoji-trigger").forEach((b) => b.classList.remove("active"));
}

// ---------------------------------------------------------------------------
// Per-video end caption detail panel
// ---------------------------------------------------------------------------

function hideVideoDetail() {
  selectedSourceId = null;
  document.querySelectorAll(".source-item.selected").forEach((el) => el.classList.remove("selected"));
  const detail = $("videoDetail");
  if (!detail) return;
  detail.classList.add("empty");
  detail.classList.remove("end-on");
  if ($("videoDetailName")) $("videoDetailName").textContent = "Per-video end caption";
  if ($("videoDetailEmpty")) $("videoDetailEmpty").classList.remove("hidden");
  if ($("videoDetailBody")) $("videoDetailBody").classList.add("hidden");
}

function selectSourceForPreview(sourceId) {
  const src = sources.find((s) => s.id === sourceId);
  if (!src) return;
  selectedSourceId = sourceId;
  document.querySelectorAll(".source-item").forEach((el) => {
    el.classList.toggle("selected", el.dataset.id === sourceId);
  });
  if ($("previewSource") && $("previewSource").value !== sourceId) {
    $("previewSource").value = sourceId;
    saveUiState();
    refreshOverlay();
  }
  const drawerOpen = $("endCaptionDrawer") && !$("endCaptionDrawer").classList.contains("hidden");
  if (drawerOpen) {
    fillVideoDetail(src);
    const detail = $("videoDetail");
    if (detail) {
      detail.classList.remove("empty");
      if ($("videoDetailEmpty")) $("videoDetailEmpty").classList.add("hidden");
      if ($("videoDetailBody")) $("videoDetailBody").classList.remove("hidden");
    }
  }
}

function openVideoDetail(sourceId) {
  const src = sources.find((s) => s.id === sourceId);
  if (!src) return;
  selectedSourceId = sourceId;
  document.querySelectorAll(".source-item").forEach((el) => {
    el.classList.toggle("selected", el.dataset.id === sourceId);
  });
  fillVideoDetail(src);
  const detail = $("videoDetail");
  if (detail) {
    detail.classList.remove("empty");
    if ($("videoDetailEmpty")) $("videoDetailEmpty").classList.add("hidden");
    if ($("videoDetailBody")) $("videoDetailBody").classList.remove("hidden");
  }
  const workbench = document.querySelector(".create-workbench");
  if (workbench && window.matchMedia("(max-width: 700px)").matches) {
    workbench.dataset.createView = "edit";
    document.querySelectorAll(".create-phone-chips [data-create-view]").forEach((chip) => {
      const on = chip.dataset.createView === "edit";
      chip.classList.toggle("active", on);
      chip.setAttribute("aria-selected", on ? "true" : "false");
    });
  }
  // Jump preview to this video so placement is easy to judge.
  if ($("previewSource") && $("previewSource").value !== sourceId) {
    $("previewSource").value = sourceId;
    saveUiState();
    refreshOverlay();
  }
  openCreateDrawer("endCaption");
}

function openEndCaptionFromStage() {
  if (isCreateCarouselMode()) {
    alert("End captions are for video mode.");
    return;
  }
  const pick =
    selectedSourceId ||
    ($("previewSource") && $("previewSource").value) ||
    (sources[0] && sources[0].id) ||
    "";
  if (!pick) {
    hideVideoDetail();
    openCreateDrawer("endCaption");
    return;
  }
  openVideoDetail(pick);
}

function fillVideoDetail(src) {
  if (!src) return;
  const ec = src.endCaption || {};
  if ($("videoDetailName")) $("videoDetailName").textContent = "End caption · " + src.name;
  restoring = true;
  try {
    if ($("endCapEnabled")) $("endCapEnabled").checked = !!ec.enabled;
    if ($("endCapText")) $("endCapText").value = ec.text || "";
    const dur = Math.min(5, Math.max(2, Number(ec.durationSeconds) || 3));
    if ($("endCapDuration")) $("endCapDuration").value = dur;
    if ($("endCapDurationOut")) $("endCapDurationOut").textContent = `${dur}s`;
    const xOff = Math.min(40, Math.max(-40, Number(ec.xOffset) || 0));
    const yOff = Math.min(40, Math.max(-40, Number(ec.yOffset) || 0));
    if ($("endCapX")) $("endCapX").value = xOff;
    if ($("endCapY")) $("endCapY").value = yOff;
    if ($("endCapXOut")) $("endCapXOut").textContent = formatOffset(xOff);
    if ($("endCapYOut")) $("endCapYOut").textContent = formatOffset(yOff);
  } finally {
    restoring = false;
  }
  $("videoDetail")?.classList.toggle("end-on", !!ec.enabled);
}

function formatOffset(v) {
  const n = Number(v) || 0;
  if (n === 0) return "0";
  return (n > 0 ? "+" : "") + n;
}

function readEndCaptionFromPanel() {
  return {
    enabled: $("endCapEnabled").checked,
    text: $("endCapText").value,
    durationSeconds: Number($("endCapDuration").value) || 3,
    xOffset: Number($("endCapX").value) || 0,
    yOffset: Number($("endCapY").value) || 0,
  };
}

function getActiveEndCaption(src) {
  if (!src || !src.endCaption) return null;
  const ec = src.endCaption;
  if (!ec.enabled) return null;
  const text = (ec.text || "").trim();
  if (!text) return null;
  return {
    text,
    durationSeconds: Math.min(5, Math.max(2, Number(ec.durationSeconds) || 3)),
    xOffset: Math.min(40, Math.max(-40, Number(ec.xOffset) || 0)),
    yOffset: Math.min(40, Math.max(-40, Number(ec.yOffset) || 0)),
  };
}

function clamp01(n) {
  return Math.min(1, Math.max(0, Number(n) || 0));
}

function getActiveCrop(src) {
  if (!src || !src.crop || !src.crop.enabled) return null;
  const x = clamp01(src.crop.x);
  const y = clamp01(src.crop.y);
  const w = Math.min(1 - x, Math.max(0, Number(src.crop.w) || 0));
  const h = Math.min(1 - y, Math.max(0, Number(src.crop.h) || 0));
  if (w < 0.02 || h < 0.02) return null;
  if (w >= 0.995 && h >= 0.995 && x <= 0.002 && y <= 0.002) return null;
  // Always expose a 9:16 rect (matches encode normalize_crop).
  const fw = Math.max(1, src.width || 1080);
  const fh = Math.max(1, src.height || 1920);
  const snapped = cropNorm916({ x, y, w, h }, fw, fh);
  return { enabled: true, ...snapped };
}

function getActiveTrim(src) {
  if (!src || !(src.duration > 0)) return null;
  const dur = Number(src.duration) || 0;
  const raw = src.trim || rememberedTrimLocal(src.hash);
  if (!raw) return null;
  let start = Math.max(0, Number(raw.start) || 0);
  let end = Number(raw.end);
  if (!Number.isFinite(end) || end <= 0) end = dur;
  end = Math.min(dur, end);
  start = Math.min(start, Math.max(0, end - 0.1));
  if (start <= 0.05 && end >= dur - 0.05) return null;
  return { start, end };
}

function effectiveTrim(src) {
  const dur = Number(src && src.duration) || 0;
  const t = getActiveTrim(src);
  if (!t) return { start: 0, end: dur };
  return t;
}

function fmtTimecode(sec) {
  sec = Math.max(0, Number(sec) || 0);
  const m = Math.floor(sec / 60);
  const s = Math.floor(sec % 60);
  const frac = Math.round((sec % 1) * 10);
  if (m > 0) return `${m}:${String(s).padStart(2, "0")}.${frac}`;
  return `${s}.${frac}s`;
}

/** Match server resolve_output_size: ≥720p, keep higher native res. */
function resolveOutputSize(width, height) {
  let w = Math.round(Number(width) || 0);
  let h = Math.round(Number(height) || 0);
  if (w < 2 || h < 2) return { w: 720, h: 1280 };
  const minW = h >= w ? 720 : 1280;
  const minH = h >= w ? 1280 : 720;
  let scale = 1;
  if (w < minW) scale = Math.max(scale, minW / w);
  if (h < minH) scale = Math.max(scale, minH / h);
  let ow = Math.round(w * scale);
  let oh = Math.round(h * scale);
  ow -= ow % 2;
  oh -= oh % 2;
  if (ow < minW) ow = minW;
  if (oh < minH) oh = minH;
  ow -= ow % 2;
  oh -= oh % 2;
  return { w: ow, h: oh };
}

function effectiveFrameSize(src) {
  const c = getActiveCrop(src);
  const fw = src ? src.width : 1080;
  const fh = src ? src.height : 1920;
  if (!c) return resolveOutputSize(fw, fh);
  return resolveOutputSize(
    Math.max(16, Math.round(fw * c.w)),
    Math.max(16, Math.round(fh * c.h)),
  );
}

function applyPreviewCrop(src) {
  const frame = $("previewFrame");
  const bg = $("previewBg");
  if (!frame || !bg) return;
  const c = getActiveCrop(src);
  if (!c || !src) {
    frame.classList.remove("is-cropped");
    frame.style.removeProperty("aspect-ratio");
    bg.style.width = "";
    bg.style.height = "";
    bg.style.left = "";
    bg.style.top = "";
    return;
  }
  // c.w/c.h are fractions of the source frame — pixel AR is (c.w*width)/(c.h*height).
  // Using c.w/c.h directly squashes landscape sources into a too-narrow preview.
  const pw = Math.max(1, c.w * src.width);
  const ph = Math.max(1, c.h * src.height);
  frame.classList.add("is-cropped");
  frame.style.aspectRatio = `${pw} / ${ph}`;
  bg.style.width = `${(1 / c.w) * 100}%`;
  bg.style.height = `${(1 / c.h) * 100}%`;
  bg.style.left = `${(-c.x / c.w) * 100}%`;
  bg.style.top = `${(-c.y / c.h) * 100}%`;
}

function syncCropButton(src) {
  const btn = $("previewCrop");
  if (!btn) return;
  const on = !!getActiveCrop(src);
  btn.textContent = on ? "Crop ✓" : "Crop";
  btn.title = on ? "Edit crop for this video" : "Crop this video for all renders";
}

async function persistCrop(sourceId, crop) {
  const src = sources.find((s) => s.id === sourceId);
  if (!src) return false;
  let payload;
  if (crop && crop.enabled !== false && Number(crop.w) > 0 && Number(crop.h) > 0) {
    const fw = Math.max(1, src.width || 1080);
    const fh = Math.max(1, src.height || 1920);
    const r = cropNorm916(crop, fw, fh);
    payload = { enabled: true, x: r.x, y: r.y, w: r.w, h: r.h };
  } else {
    payload = { enabled: false, x: 0, y: 0, w: 1, h: 1 };
  }
  src.crop = payload.enabled ? payload : null;
  rememberCropLocal(src.hash, src.crop);
  document.querySelectorAll(".source-item").forEach((el) => {
    if (el.dataset.id !== sourceId) return;
    const meta = el.querySelector(".meta");
    if (!meta) return;
    const badge = meta.querySelector(".crop-badge");
    if (payload.enabled && !badge) {
      const span = document.createElement("span");
      span.className = "crop-badge";
      span.title = "Crop on";
      span.textContent = "CROP";
      const endBadge = meta.querySelector(".end-badge");
      if (endBadge) endBadge.after(span);
      else meta.insertBefore(span, meta.firstChild);
    } else if (!payload.enabled && badge) {
      badge.remove();
    }
  });
  try {
    const updated = await api(`/api/sources/${sourceId}/crop`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    Object.assign(src, updated);
    syncCropButton(src);
    refreshOverlayDebounced();
    return true;
  } catch (err) {
    console.warn("Failed to persist crop:", err);
    syncCropButton(src);
    refreshOverlayDebounced();
    return false;
  }
}

/** Ensure every source crop is on the server + in a crops map for batch/preview. */
async function flushAllCrops() {
  const crops = {};
  for (const s of sources) {
    let crop = getActiveCrop(s) || rememberedCropLocal(s.hash);
    if (!crop) continue;
    s.crop = crop;
    rememberCropLocal(s.hash, crop);
    try {
      const updated = await api(`/api/sources/${s.id}/crop`, {
        method: "PUT",
        body: JSON.stringify(crop),
      });
      Object.assign(s, updated);
      crop = getActiveCrop(s) || crop;
    } catch (err) {
      console.warn("Failed to flush crop for", s.id, err);
    }
    if (crop) crops[s.id] = { enabled: true, ...crop };
  }
  return crops;
}

async function flushAllTrims() {
  const trims = {};
  for (const s of sources) {
    let trim = getActiveTrim(s) || rememberedTrimLocal(s.hash);
    if (!trim || !(s.duration > 0)) continue;
    const start = Math.max(0, Number(trim.start) || 0);
    const end = Math.min(s.duration, Number(trim.end) || s.duration);
    if (start <= 0.05 && end >= s.duration - 0.05) {
      s.trim = null;
      rememberTrimLocal(s.hash, null);
      try {
        await api(`/api/sources/${s.id}/trim`, {
          method: "PUT",
          body: JSON.stringify({ start: 0, end: s.duration }),
        });
      } catch {}
      continue;
    }
    trim = { start, end };
    s.trim = trim;
    rememberTrimLocal(s.hash, trim);
    try {
      const updated = await api(`/api/sources/${s.id}/trim`, {
        method: "PUT",
        body: JSON.stringify(trim),
      });
      Object.assign(s, updated);
      trim = getActiveTrim(s) || trim;
    } catch (err) {
      console.warn("Failed to flush trim for", s.id, err);
    }
    if (trim) trims[s.id] = trim;
  }
  return trims;
}

async function persistTrim(sourceId, trim) {
  const src = sources.find((s) => s.id === sourceId);
  if (!src) return;
  const dur = Number(src.duration) || 0;
  let payload = null;
  if (trim && dur > 0) {
    const start = Math.max(0, Number(trim.start) || 0);
    const end = Math.min(dur, Math.max(start + 0.1, Number(trim.end) || dur));
    if (start > 0.05 || end < dur - 0.05) payload = { start, end };
  }
  src.trim = payload;
  rememberTrimLocal(src.hash, payload);
  try {
    const updated = await api(`/api/sources/${sourceId}/trim`, {
      method: "PUT",
      body: JSON.stringify(payload || { start: 0, end: dur }),
    });
    Object.assign(src, updated);
  } catch (err) {
    console.warn("Failed to persist trim:", err);
  }
  paintTimeline();
}

function scheduleTrimSave() {
  const srcId = $("previewSource").value;
  if (!srcId) return;
  clearTimeout(trimSaveTimer);
  trimSaveTimer = setTimeout(() => {
    persistTrim(srcId, trimDraft);
  }, 200);
}

async function persistEndCaption(sourceId) {
  const src = sources.find((s) => s.id === sourceId);
  if (!src) return;
  const payload = readEndCaptionFromPanel();
  src.endCaption = payload;
  // refresh badge without full re-render thrash
  document.querySelectorAll(".source-item").forEach((el) => {
    if (el.dataset.id !== sourceId) return;
    const meta = el.querySelector(".meta");
    if (!meta) return;
    const badge = meta.querySelector(".end-badge");
    const on = payload.enabled && (payload.text || "").trim();
    if (on && !badge) {
      const span = document.createElement("span");
      span.className = "end-badge";
      span.title = "End caption on";
      span.textContent = "END";
      meta.insertBefore(span, meta.firstChild);
    } else if (!on && badge) {
      badge.remove();
    }
  });
  $("videoDetail").classList.toggle("end-on", !!payload.enabled);
  try {
    const updated = await api(`/api/sources/${sourceId}/end-caption`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    Object.assign(src, updated);
  } catch (err) {
    console.error("Failed to save end caption:", err);
  }
  refreshOverlayDebounced();
  paintTimeline();
}

function scheduleEndCapSave() {
  if (restoring || !selectedSourceId) return;
  clearTimeout(endCapSaveTimer);
  endCapSaveTimer = setTimeout(() => persistEndCaption(selectedSourceId), 250);
}

function initVideoDetail() {
  $("videoDetailClose")?.addEventListener("click", hideVideoDetail);
  $("endCapEnabled")?.addEventListener("change", () => {
    $("videoDetail")?.classList.toggle("end-on", $("endCapEnabled").checked);
    scheduleEndCapSave();
  });
  $("endCapText")?.addEventListener("input", scheduleEndCapSave);
  $("endCapDuration")?.addEventListener("input", () => {
    if ($("endCapDurationOut")) $("endCapDurationOut").textContent = `${$("endCapDuration").value}s`;
    scheduleEndCapSave();
  });
  $("endCapX")?.addEventListener("input", () => {
    if ($("endCapXOut")) $("endCapXOut").textContent = formatOffset($("endCapX").value);
    scheduleEndCapSave();
  });
  $("endCapY")?.addEventListener("input", () => {
    if ($("endCapYOut")) $("endCapYOut").textContent = formatOffset($("endCapY").value);
    scheduleEndCapSave();
  });
}

// ---------------------------------------------------------------------------
// Count chip + estimate
// ---------------------------------------------------------------------------

let estimateTimer = null;

function updateCount() {
  if (isCreateCarouselMode()) {
    const pairs = allCarouselCaptionPairs();
    const nImg = getCarouselSlides().length;
    $("countChip").textContent = nImg
      ? `${nImg} image${nImg === 1 ? "" : "s"} · ${pairs.length} caption${pairs.length === 1 ? "" : "s"} = ${pairs.length} carousel post${pairs.length === 1 ? "" : "s"}`
      : "0 images · 0 captions = 0 posts";
    clearTimeout(estimateTimer);
    const el = $("estimate");
    if (el) {
      el.textContent = pairs.length
        ? pairs.length === 1
          ? `One TikTok photo post with ${nImg} slide${nImg === 1 ? "" : "s"} (no burn-in render).`
          : `${pairs.length} TikTok photo posts · ${nImg} slides each (no burn-in render).`
        : "";
    }
    return;
  }
  const nV = sources.length;
  const nC = captionList().length;
  const mode = $("mode").value;
  if (customPairsActive()) {
    $("countChip").textContent =
      `${workspaceRenderPairs.length} planned encodes (custom)`;
  } else {
    const total = mode === "pair" ? (nV === nC ? nV : 0) : nV * nC;
    const eq = mode === "pair" && nV !== nC && nV && nC
      ? ` — pair mode needs equal counts`
      : "";
    $("countChip").textContent =
      mode === "pair"
        ? `${nV} videos ↔ ${nC} captions = ${total} videos${eq}`
        : `${nV} videos × ${nC} captions = ${total} videos`;
  }

  clearTimeout(estimateTimer);
  estimateTimer = setTimeout(refreshEstimate, 400);
}

async function refreshEstimate() {
  const el = $("estimate");
  const captions = captionList();
  const pairs = customPairsActive() ? workspaceRenderPairs : null;
  if (!sources.length || (!captions.length && !pairs)) { el.textContent = ""; return; }
  try {
    const body = {
      sourceIds: pairs
        ? [...new Set(pairs.map((p) => p.sourceId))]
        : sources.map((s) => s.id),
      captions: pairs ? pairs.map((p) => p.caption) : captions,
      mode: $("mode").value,
      dedupe: $("dedupe").checked,
    };
    if (pairs) body.pairs = pairs.map((p) => ({ sourceId: p.sourceId, caption: p.caption }));
    const est = await api("/api/batch/estimate", {
      method: "POST",
      body: JSON.stringify(body),
    });
    if (!est.jobs) { el.textContent = ""; return; }
    let txt = `${est.jobs} encodes · ~${fmtDuration(est.estimatedSeconds)} with ${est.encoder}`;
    if (est.overCap) txt += ` — <span class="warn">large batch (over ${config.softJobCap} jobs)</span>`;
    el.innerHTML = txt;
  } catch (err) {
    el.innerHTML = `<span class="warn">${escapeHtml(err.message)}</span>`;
  }
}

// ---------------------------------------------------------------------------
// Style controls
// ---------------------------------------------------------------------------

const SLIDERS = [
  { id: "sSize", out: "oSize", key: "fontSizeRatio", scale: 100, fmt: (v) => v.toFixed(1) + "%" },
  { id: "sStroke", out: "oStroke", key: "strokeRatio", scale: 100, fmt: (v) => v.toFixed(1) + "%", modes: ["outline"] },
  {
    id: "sBoxPad",
    out: "oBoxPad",
    key: "boxPadRatio",
    scale: 100,
    fmt: (v) => v.toFixed(0) + "%",
    modes: ["boxed"],
  },
  {
    id: "sBoxRadius",
    out: "oBoxRadius",
    key: "boxCornerRatio",
    scale: 100,
    fmt: (v) => v.toFixed(0) + "%",
    modes: ["boxed"],
  },
  { id: "sY", out: "oY", key: "yRatio", scale: 100, fmt: (v) => v.toFixed(0) + "%" },
  // Side padding = (1 - maxWidthRatio) / 2; higher padding → narrower text.
  {
    id: "sPad",
    out: "oPad",
    key: "maxWidthRatio",
    toSlider: (ratio) => ((1 - Number(ratio || 0.86)) / 2) * 100,
    fromSlider: (padPct) => 1 - 2 * (Number(padPct) / 100),
    fmt: (v) => v.toFixed(1) + "%",
  },
  { id: "sWeight", out: "oWeight", key: "fontWeight", scale: 1, fmt: (v) => String(v) },
  { id: "sMaxLines", out: "oMaxLines", key: "maxLines", scale: 1, fmt: (v) => String(v) },
];

function syncModeUi() {
  const mode = style.captionMode === "boxed" ? "boxed" : "outline";
  style.captionMode = mode;
  if ($("captionMode")) $("captionMode").value = mode;
  const boxed = mode === "boxed";
  $("strokePresetField")?.classList.toggle("hidden", boxed);
  $("strokeSliderRow")?.classList.toggle("hidden", boxed);
  $("boxPadSliderRow")?.classList.toggle("hidden", !boxed);
  $("boxRadiusSliderRow")?.classList.toggle("hidden", !boxed);
}

function syncSlidersFromStyle() {
  syncModeUi();
  for (const s of SLIDERS) {
    if (!$(s.id)) continue;
    let val;
    if (s.toSlider) {
      val = s.toSlider(style[s.key]);
    } else {
      val = style[s.key] * s.scale;
    }
    $(s.id).value = val;
    $(s.out).textContent = s.fmt(val);
  }
}

function initStyleControls() {
  if (style.captionMode !== "boxed" && style.captionMode !== "outline") {
    style.captionMode = "outline";
  }
  syncSlidersFromStyle();
  for (const s of SLIDERS) {
    if (!$(s.id)) continue;
    $(s.id).addEventListener("input", () => {
      const val = Number($(s.id).value);
      if (s.fromSlider) {
        style[s.key] = s.fromSlider(val);
      } else {
        style[s.key] = val / s.scale;
      }
      $(s.out).textContent = s.fmt(val);
      saveStyle();
      refreshOverlayDebounced();
    });
  }

  $("captionMode").addEventListener("change", () => {
    style.captionMode = $("captionMode").value === "boxed" ? "boxed" : "outline";
    syncSlidersFromStyle();
    saveStyle();
    saveUiState();
    refreshOverlay();
  });

  $("strokePreset").addEventListener("change", () => {
    const preset = config.stylePresets[$("strokePreset").value];
    if (preset) {
      style.strokeRatio = preset.strokeRatio;
      syncSlidersFromStyle();
      saveStyle();
      saveUiState();
      refreshOverlay();
    }
  });

  $("positionPreset").addEventListener("change", () => {
    const y = config.positionPresets[$("positionPreset").value];
    if (y !== undefined) {
      style.yRatio = y;
      syncSlidersFromStyle();
      saveStyle();
      saveUiState();
      refreshOverlay();
    }
  });

  $("resetStyle").addEventListener("click", () => {
    style = { ...DEFAULT_STYLE };
    $("captionMode").value = "outline";
    $("strokePreset").value = "thick";
    $("positionPreset").value = "center";
    syncSlidersFromStyle();
    saveStyle();
    saveUiState();
    refreshOverlay();
  });
}

// ---------------------------------------------------------------------------
// Live preview
// ---------------------------------------------------------------------------

let overlayTimer = null;

function refreshOverlayDebounced() {
  clearTimeout(overlayTimer);
  overlayTimer = setTimeout(refreshOverlay, 250);
}

function updatePreviewSelectors(preferred) {
  const srcSel = $("previewSource");
  const capSel = $("previewCaption");
  const imgSel = $("previewCarouselImage");
  const prevSrc = (preferred && preferred.previewSourceId) || (srcSel && srcSel.value) || "";
  const prevCap = (preferred && preferred.previewCaptionIdx) || (capSel && capSel.value);
  const prevImg =
    (preferred && preferred.previewCarouselImageId) ||
    (imgSel && imgSel.value) ||
    currentCarouselImageId();

  if (srcSel) {
    // Use textContent (not innerHTML) so filenames / emoji never break the menu layout.
    srcSel.replaceChildren();
    const srcEmpty = document.createElement("option");
    srcEmpty.value = "";
    srcEmpty.textContent = "— pick a video —";
    srcSel.appendChild(srcEmpty);
    for (const s of sources) {
      const opt = document.createElement("option");
      opt.value = s.id;
      opt.textContent = s.name || s.id;
      srcSel.appendChild(opt);
    }
    if (prevSrc && [...srcSel.options].some((o) => o.value === prevSrc)) srcSel.value = prevSrc;
    else if (sources.length) srcSel.value = sources[0].id;
  }

  if (imgSel) {
    const slides = getCarouselSlides();
    imgSel.replaceChildren();
    const imgEmpty = document.createElement("option");
    imgEmpty.value = "";
    imgEmpty.textContent = "— pick an image —";
    imgSel.appendChild(imgEmpty);
    slides.forEach((img, i) => {
      const opt = document.createElement("option");
      opt.value = img.id;
      opt.textContent = `${i + 1}. ${img.name || img.id}`;
      imgSel.appendChild(opt);
    });
    if (prevImg && [...imgSel.options].some((o) => o.value === prevImg)) imgSel.value = prevImg;
    else if (slides.length) imgSel.value = slides[0].id;
    const idx = slides.findIndex((s) => s.id === imgSel.value);
    if (idx >= 0) carouselSlideIndex = idx;
  }

  const caps = captionList();
  if (capSel) {
    capSel.replaceChildren();
    const capEmpty = document.createElement("option");
    capEmpty.value = "";
    capEmpty.textContent = "— pick a caption —";
    capSel.appendChild(capEmpty);
    caps.forEach((c, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      const oneLine = String(c).replace(/\s+/g, " ").trim();
      const short = oneLine.length > 48 ? oneLine.slice(0, 48) + "…" : oneLine;
      opt.textContent = `${i + 1}. ${short}`;
      opt.title = oneLine;
      capSel.appendChild(opt);
    });
    if (prevCap !== "" && prevCap != null && [...capSel.options].some((o) => o.value === String(prevCap)))
      capSel.value = String(prevCap);
    else if (caps.length) capSel.value = "0";
  }

  refreshOverlay();
}

async function refreshOverlay() {
  if (isCreateCarouselMode()) {
    await refreshCarouselOverlay();
    return;
  }
  const srcId = $("previewSource").value;
  const capIdx = $("previewCaption").value;
  const caps = captionList();
  const caption = capIdx !== "" ? caps[Number(capIdx)] : null;
  const src = sources.find((s) => s.id === srcId);
  const hint = $("previewHint");
  const bg = $("previewBg");
  const ov = $("previewOverlay");
  const end = getActiveEndCaption(src);
  applyPreviewCrop(src);
  syncCropButton(src);
  paintTimeline();

  if (hint) {
    hint.textContent = "Add a video and a caption to preview";
  }

  if (src) {
    // Show a near-end frame when end caption is active so placement is realistic.
    let frameUrl = `/api/sources/${src.id}/frame`;
    const trim = effectiveTrim(src);
    if (end && trim.end > trim.start) {
      const endDur = Math.min(end.durationSeconds, trim.end - trim.start);
      const at = Math.max(trim.start, trim.end - endDur / 2);
      frameUrl += `?at=${at.toFixed(2)}`;
    } else if (trim.start > 0.05) {
      frameUrl += `?at=${trim.start.toFixed(2)}`;
    }
    if (window.setAuthSrc) setAuthSrc(bg, frameUrl).catch(() => { bg.removeAttribute("src"); });
    else bg.src = frameUrl;
    bg.style.display = "";
  } else {
    bg.removeAttribute("src");
    bg.style.display = "none";
  }

  if (!caption) {
    ov.style.display = "none";
    hint.style.display = src ? "none" : "";
    return;
  }
  hint.style.display = "none";

  const size = effectiveFrameSize(src);
  const w = size.w;
  const h = size.h;
  try {
    const res = await fetch("/api/preview/overlay", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify({
        caption,
        width: w,
        height: h,
        settings: settingsPayload(),
        endCaption: end || null,
      }),
    });
    if (!res.ok) throw new Error("overlay failed");
    const blob = await res.blob();
    if (ov.dataset.url) URL.revokeObjectURL(ov.dataset.url);
    ov.dataset.url = URL.createObjectURL(blob);
    ov.src = ov.dataset.url;
    ov.style.display = "";
  } catch (err) {
    ov.style.display = "none";
    if (typeof showToast === "function") {
      showToast("Preview overlay failed", { error: true });
    }
  }
}

async function refreshCarouselOverlay() {
  const slides = getCarouselSlides();
  const slide = slides[carouselSlideIndex] || null;
  const capIdx = $("previewCaption").value;
  const caps = captionList();
  const caption = capIdx !== "" ? caps[Number(capIdx)] : null;
  const hint = $("previewHint");
  const bg = $("previewBg");
  const ov = $("previewOverlay");
  const frame = $("previewFrame");

  if (frame) {
    frame.classList.remove("is-cropped");
    frame.style.removeProperty("aspect-ratio");
  }
  if (bg) {
    bg.style.cssText = "";
    bg.style.objectFit = "cover";
  }
  syncCropButton(null);
  const panel = $("timelinePanel");
  if (panel) panel.classList.add("hidden");

  if (slide) {
    const url = slide.fileUrl || slide.thumbUrl || `/api/images/${slide.id}/file`;
    if (window.setAuthSrc) setAuthSrc(bg, url);
    else bg.src = url;
    bg.style.display = "";
  } else {
    bg.removeAttribute("src");
    bg.style.display = "none";
  }

  if (!caption) {
    ov.style.display = "none";
    if (hint) {
      hint.textContent = slide
        ? "Pick a caption to preview on this slide"
        : "Add images to preview the carousel";
      hint.style.display = "";
    }
    return;
  }
  if (hint) hint.style.display = "none";

  const w = 1080;
  const h = 1920;
  try {
    const res = await fetch("/api/preview/overlay", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify({
        caption,
        width: w,
        height: h,
        settings: settingsPayload(),
        endCaption: null,
      }),
    });
    if (!res.ok) throw new Error("overlay failed");
    const blob = await res.blob();
    if (ov.dataset.url) URL.revokeObjectURL(ov.dataset.url);
    ov.dataset.url = URL.createObjectURL(blob);
    ov.src = ov.dataset.url;
    ov.style.display = "";
  } catch (err) {
    ov.style.display = "none";
    if (typeof showToast === "function") {
      showToast("Carousel overlay failed", { error: true });
    }
  }
}

async function encodePreviewClip() {
  if (!$("previewSource") || !$("previewCaption") || !$("previewEncode")) return;
  const srcId = $("previewSource").value;
  const capIdx = $("previewCaption").value;
  const caps = captionList();
  if (!srcId || capIdx === "") { alert("Pick a video and a caption first."); return; }
  const src = sources.find((s) => s.id === srcId);
  const end = getActiveEndCaption(src);
  // Make sure server session has the crop before encoding.
  await flushAllCrops();
  await flushAllTrims();
  const crop = getActiveCrop(src) || rememberedCropLocal(src && src.hash);
  const trim = getActiveTrim(src);
  const btn = $("previewEncode");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Encoding preview…";
  try {
    const body = {
      sourceId: srcId,
      caption: caps[Number(capIdx)],
      settings: settingsPayload(),
      full: true,
    };
    if (end) body.endCaption = end;
    if (crop) body.crop = { enabled: true, ...crop };
    else body.crop = { enabled: false };
    if (trim) body.trim = trim;
    const res = await fetch("/api/preview/encode", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...(await authHeaders()) },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      let d = res.statusText;
      try { d = (await res.json()).detail || d; } catch {}
      throw new Error(d);
    }
    const blob = await res.blob();
    const video = $("previewVideo");
    if (video.dataset.url) URL.revokeObjectURL(video.dataset.url);
    video.dataset.url = URL.createObjectURL(blob);
    video.src = video.dataset.url;
    video.classList.toggle("is-cropped", !!crop);
    video.classList.remove("hidden");
    video.play().catch(() => {});
  } catch (err) {
    alert("Preview encode failed: " + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
}

function initPreview() {
  $("previewSource").addEventListener("change", () => { saveUiState(); refreshOverlay(); });
  $("previewCaption").addEventListener("change", () => { saveUiState(); refreshOverlay(); });
  $("previewCarouselImage")?.addEventListener("change", () => {
    const id = $("previewCarouselImage").value;
    if (!id) return;
    flushCarouselCaptionsFromStrip();
    syncCarouselStage({ preferId: id });
    loadCaptionsStripForMode();
    updateCount();
    saveUiState();
  });
  $("previewEncode").addEventListener("click", () => encodePreviewClip());
  $("previewCrop").addEventListener("click", openCropModal);
  initCropModal();
  initTimeline();
  initCarouselStageNav();
}

function initCarouselStageNav() {
  $("carouselPrevBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    stepCarouselSlide(-1);
  });
  $("carouselNextBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    stepCarouselSlide(1);
  });
  $("carouselStoryboardTrack")?.addEventListener("click", (e) => {
    const thumb = e.target.closest("[data-slide-index]");
    if (!thumb) return;
    jumpCarouselSlide(thumb.getAttribute("data-slide-index"));
  });
  document.addEventListener("keydown", (e) => {
    if (!isCreateCarouselMode()) return;
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || e.target.isContentEditable) return;
    if (e.key === "ArrowLeft") {
      e.preventDefault();
      stepCarouselSlide(-1);
    } else if (e.key === "ArrowRight") {
      e.preventDefault();
      stepCarouselSlide(1);
    }
  });
}

// ---------------------------------------------------------------------------
// Trim timeline
// ---------------------------------------------------------------------------

function paintTimeline() {
  const panel = $("timelinePanel");
  const srcId = $("previewSource").value;
  const src = sources.find((s) => s.id === srcId);
  if (!panel) return;
  if (!src || !(src.duration > 0)) {
    panel.classList.add("hidden");
    return;
  }
  panel.classList.remove("hidden");
  const dur = Number(src.duration) || 0;
  const t = effectiveTrim(src);
  trimDraft = { start: t.start, end: t.end };
  const leftPct = (t.start / dur) * 100;
  const widthPct = ((t.end - t.start) / dur) * 100;
  $("timelineRange").style.left = `${leftPct}%`;
  $("timelineRange").style.width = `${widthPct}%`;
  $("timelineHandleIn").style.left = `${leftPct}%`;
  $("timelineHandleOut").style.left = `${(t.end / dur) * 100}%`;
  $("timelineInLabel").textContent = fmtTimecode(t.start);
  $("timelineOutLabel").textContent = fmtTimecode(t.end);
  $("timelineDurLabel").textContent = `Clip ${fmtTimecode(t.end - t.start)}`;

  const end = getActiveEndCaption(src);
  const endEl = $("timelineEndCap");
  const endLeg = $("timelineEndCapLeg");
  if (end && t.end > t.start) {
    const endDur = Math.min(end.durationSeconds, t.end - t.start);
    const endStart = t.end - endDur;
    endEl.classList.remove("hidden");
    endLeg.classList.remove("hidden");
    endEl.style.left = `${(endStart / dur) * 100}%`;
    endEl.style.width = `${(endDur / dur) * 100}%`;
    $("timelineEndCapDur").textContent = `(${fmtTimecode(endDur)})`;
  } else {
    endEl.classList.add("hidden");
    endLeg.classList.add("hidden");
  }
}

function initTimeline() {
  const track = $("timelineTrack");
  if (!track) return;

  const ptrFrac = (e) => {
    const rect = track.getBoundingClientRect();
    return clamp01((e.clientX - rect.left) / Math.max(1, rect.width));
  };

  const applyFrac = (which, frac) => {
    const srcId = $("previewSource").value;
    const src = sources.find((s) => s.id === srcId);
    if (!src || !(src.duration > 0)) return;
    const dur = src.duration;
    const minLen = Math.min(0.5, dur);
    let { start, end } = trimDraft;
    const t = frac * dur;
    if (which === "in") {
      start = Math.min(t, end - minLen);
      start = Math.max(0, start);
    } else {
      end = Math.max(t, start + minLen);
      end = Math.min(dur, end);
    }
    trimDraft = { start, end };
    src.trim = (start <= 0.05 && end >= dur - 0.05) ? null : { ...trimDraft };
    paintTimeline();
    scheduleTrimSave();
  };

  const onDown = (which) => (e) => {
    if (e.button != null && e.button !== 0) return;
    trimDrag = which;
    track.setPointerCapture?.(e.pointerId);
    applyFrac(which, ptrFrac(e));
    e.preventDefault();
    e.stopPropagation();
  };

  $("timelineHandleIn").addEventListener("pointerdown", onDown("in"));
  $("timelineHandleOut").addEventListener("pointerdown", onDown("out"));

  track.addEventListener("pointermove", (e) => {
    if (!trimDrag) return;
    applyFrac(trimDrag, ptrFrac(e));
  });
  const endDrag = () => { trimDrag = null; };
  track.addEventListener("pointerup", endDrag);
  track.addEventListener("pointercancel", endDrag);

  $("timelineReset").addEventListener("click", () => {
    const srcId = $("previewSource").value;
    const src = sources.find((s) => s.id === srcId);
    if (!src) return;
    trimDraft = { start: 0, end: src.duration };
    src.trim = null;
    paintTimeline();
    persistTrim(srcId, null);
  });
}

// ---------------------------------------------------------------------------
// Crop modal (always 9:16)
// ---------------------------------------------------------------------------

const CROP_PIXEL_AR = 9 / 16; // width / height in pixels

function cropModalFrame() {
  const src = sources.find((s) => s.id === cropModalSourceId);
  return {
    w: Math.max(1, src?.width || 1080),
    h: Math.max(1, src?.height || 1920),
  };
}

/** Required normalized width/height for a 9:16 pixel crop on this frame. */
function cropNormAspect(frameW, frameH) {
  return CROP_PIXEL_AR * (frameH / Math.max(1, frameW));
}

/** Largest centered 9:16 crop that fits the frame. */
function maxCrop916(frameW, frameH) {
  const ar = cropNormAspect(frameW, frameH); // target w/h in normalized space
  let w;
  let h;
  if (ar <= 1) {
    // Portrait / square-ish source: full height, narrower width.
    h = 1;
    w = ar;
  } else {
    // Landscape source: full width, shorter height.
    w = 1;
    h = 1 / ar;
  }
  // Clamp for float noise.
  w = Math.min(1, Math.max(0.04, w));
  h = Math.min(1, Math.max(0.04, h));
  return {
    x: (1 - w) / 2,
    y: (1 - h) / 2,
    w,
    h,
  };
}

/** Snap any rect to a 9:16 pixel crop inside the frame. */
function cropNorm916(rect, frameW, frameH) {
  const ar = cropNormAspect(frameW, frameH);
  const max = maxCrop916(frameW, frameH);
  let w = Math.max(0.04, Number(rect?.w) || 0.04);
  let h = Math.max(0.04, Number(rect?.h) || 0.04);

  // Drive size from whichever edge change is larger, then lock AR.
  const wFromH = h * ar;
  const hFromW = w / ar;
  if (Math.abs(wFromH - w) >= Math.abs(hFromW - h)) {
    w = wFromH;
  } else {
    h = hFromW;
  }
  // Prefer width-derived size, then clamp to max 9:16 that fits.
  h = w / ar;
  if (w > max.w || h > max.h) {
    w = max.w;
    h = max.h;
  }
  let x = Number(rect?.x);
  let y = Number(rect?.y);
  if (!Number.isFinite(x)) x = (1 - w) / 2;
  if (!Number.isFinite(y)) y = (1 - h) / 2;
  x = Math.min(1 - w, Math.max(0, x));
  y = Math.min(1 - h, Math.max(0, y));
  return { x, y, w, h };
}

/** Size the stage so its box AR matches the video — crop % then = video %. */
function sizeCropStage() {
  const stage = $("cropStage");
  if (!stage) return;
  const { w: fw, h: fh } = cropModalFrame();
  const parent = stage.parentElement;
  const maxW = Math.max(120, (parent?.clientWidth || stage.clientWidth || 640) - 4);
  const maxH = Math.min(window.innerHeight * 0.58, 520);
  let dispW = maxW;
  let dispH = dispW * (fh / fw);
  if (dispH > maxH) {
    dispH = maxH;
    dispW = dispH * (fw / fh);
  }
  stage.style.width = `${Math.round(dispW)}px`;
  stage.style.height = `${Math.round(dispH)}px`;
}

function paintCropBox() {
  const box = $("cropBox");
  if (!box) return;
  sizeCropStage();
  const { w: fw, h: fh } = cropModalFrame();
  const r = cropNorm916(cropDraft, fw, fh);
  cropDraft = r;
  box.classList.remove("hidden");
  box.style.left = `${r.x * 100}%`;
  box.style.top = `${r.y * 100}%`;
  box.style.width = `${r.w * 100}%`;
  box.style.height = `${r.h * 100}%`;
}

function stagePointerFrac(e) {
  const stage = $("cropStage");
  const rect = stage.getBoundingClientRect();
  const x = (e.clientX - rect.left) / Math.max(1, rect.width);
  const y = (e.clientY - rect.top) / Math.max(1, rect.height);
  return { x: clamp01(x), y: clamp01(y) };
}

function openCropModal() {
  const srcId = $("previewSource").value;
  const src = sources.find((s) => s.id === srcId);
  if (!src) {
    alert("Pick a video in the live preview first.");
    return;
  }
  cropModalSourceId = src.id;
  const { w: fw, h: fh } = cropModalFrame();
  const existing = getActiveCrop(src);
  cropDraft = existing
    ? cropNorm916(existing, fw, fh)
    : maxCrop916(fw, fh);
  $("cropModalTitle").textContent = `Crop · ${src.name}`;
  const img = $("cropImage");
  img.onload = () => paintCropBox();
  if (window.setAuthSrc) setAuthSrc(img, `/api/sources/${src.id}/frame`);
  else img.src = `/api/sources/${src.id}/frame`;
  $("cropModal").classList.remove("hidden");
  paintCropBox();
}

function closeCropModal() {
  $("cropModal").classList.add("hidden");
  cropModalSourceId = null;
  cropDrag = null;
}

async function applyCropFromModal({ clear = false } = {}) {
  const sourceId = cropModalSourceId;
  if (!sourceId) return;
  if (clear) {
    await persistCrop(sourceId, null);
  } else {
    const { w: fw, h: fh } = cropModalFrame();
    const r = cropNorm916(cropDraft, fw, fh);
    // Video already 9:16 and selection is full frame → no crop needed.
    const nearFull = r.w >= 0.995 && r.h >= 0.995;
    await persistCrop(sourceId, nearFull ? null : { enabled: true, ...r });
  }
  closeCropModal();
}

function initCropModal() {
  const stage = $("cropStage");
  const box = $("cropBox");
  if (!stage || !box) return;

  $("cropModalClose").addEventListener("click", closeCropModal);
  $("cropModalBackdrop").addEventListener("click", closeCropModal);
  $("cropCancel").addEventListener("click", closeCropModal);
  $("cropApply").addEventListener("click", () => applyCropFromModal());
  $("cropClear").addEventListener("click", () => applyCropFromModal({ clear: true }));
  $("cropReset").addEventListener("click", () => {
    const { w: fw, h: fh } = cropModalFrame();
    cropDraft = maxCrop916(fw, fh);
    paintCropBox();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && !$("cropModal").classList.contains("hidden")) {
      closeCropModal();
    }
  });
  window.addEventListener("resize", () => {
    if (!$("cropModal").classList.contains("hidden")) paintCropBox();
  });

  stage.addEventListener("pointerdown", (e) => {
    if (e.button != null && e.button !== 0) return;
    const handle = e.target.closest?.(".crop-handle");
    const onBox = e.target.closest?.(".crop-box");
    const p = stagePointerFrac(e);
    const { w: fw, h: fh } = cropModalFrame();
    stage.setPointerCapture?.(e.pointerId);
    if (handle) {
      cropDrag = {
        mode: "resize",
        handle: handle.dataset.handle,
        start: { ...cropDraft },
        origin: p,
      };
    } else if (onBox) {
      cropDrag = {
        mode: "move",
        start: { ...cropDraft },
        origin: p,
      };
    } else {
      cropDrag = {
        mode: "new",
        origin: p,
        start: { ...p },
      };
      const ar = cropNormAspect(fw, fh);
      const minW = 0.08;
      cropDraft = cropNorm916(
        { x: p.x - minW / 2, y: p.y - (minW / ar) / 2, w: minW, h: minW / ar },
        fw,
        fh,
      );
      paintCropBox();
    }
    e.preventDefault();
  });

  stage.addEventListener("pointermove", (e) => {
    if (!cropDrag) return;
    const p = stagePointerFrac(e);
    const o = cropDrag.origin;
    const s = cropDrag.start;
    const { w: fw, h: fh } = cropModalFrame();
    const ar = cropNormAspect(fw, fh);

    if (cropDrag.mode === "new") {
      // Grow a 9:16 box from the origin; size from the dominant drag axis.
      const dx = p.x - o.x;
      const dy = p.y - o.y;
      let w = Math.max(0.04, Math.abs(dx));
      let h = w / ar;
      if (Math.abs(dy) * ar > Math.abs(dx)) {
        h = Math.max(0.04, Math.abs(dy));
        w = h * ar;
      }
      const x = dx >= 0 ? o.x : o.x - w;
      const y = dy >= 0 ? o.y : o.y - h;
      cropDraft = cropNorm916({ x, y, w, h }, fw, fh);
    } else if (cropDrag.mode === "move") {
      const dx = p.x - o.x;
      const dy = p.y - o.y;
      cropDraft = cropNorm916({
        x: s.x + dx,
        y: s.y + dy,
        w: s.w,
        h: s.h,
      }, fw, fh);
    } else if (cropDrag.mode === "resize") {
      const hnd = cropDrag.handle;
      let { x, y, w, h } = s;
      const right = x + w;
      const bottom = y + h;
      const cx = x + w / 2;
      const cy = y + h / 2;

      if (hnd === "e" || hnd === "w") {
        if (hnd === "e") w = Math.max(0.04, p.x - x);
        else {
          const nx = Math.min(right - 0.04, Math.max(0, p.x));
          w = right - nx;
          x = nx;
        }
        h = w / ar;
        y = cy - h / 2;
      } else if (hnd === "n" || hnd === "s") {
        if (hnd === "s") h = Math.max(0.04, p.y - y);
        else {
          const ny = Math.min(bottom - 0.04, Math.max(0, p.y));
          h = bottom - ny;
          y = ny;
        }
        w = h * ar;
        x = cx - w / 2;
      } else {
        // Corners: opposite corner stays fixed; size locked to 9:16.
        const fromW = hnd.includes("e") || hnd.includes("w");
        if (hnd.includes("e")) w = Math.max(0.04, p.x - x);
        if (hnd.includes("w")) {
          const nx = Math.min(right - 0.04, Math.max(0, p.x));
          w = right - nx;
          x = nx;
        }
        if (hnd.includes("s")) h = Math.max(0.04, p.y - y);
        if (hnd.includes("n")) {
          const ny = Math.min(bottom - 0.04, Math.max(0, p.y));
          h = bottom - ny;
          y = ny;
        }
        if (fromW) {
          h = w / ar;
          if (hnd.includes("n")) y = bottom - h;
        } else {
          w = h * ar;
          if (hnd.includes("w")) x = right - w;
        }
      }
      cropDraft = cropNorm916({ x, y, w, h }, fw, fh);
    }
    paintCropBox();
  });

  const endDrag = () => { cropDrag = null; };
  stage.addEventListener("pointerup", endDrag);
  stage.addEventListener("pointercancel", endDrag);
}

// ---------------------------------------------------------------------------
// Batch
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Output name template params
// ---------------------------------------------------------------------------

const NAME_PARAMS = [
  {
    token: "{video}",
    label: "video",
    help: "Source video file name (without extension).",
  },
  {
    token: "{video01}",
    label: "video01",
    help: "Video number in this batch, zero-padded (01, 02, …).",
  },
  {
    token: "{caption-name}",
    label: "caption-name",
    help: "Full caption text for this render (one file per caption × video).",
  },
  {
    token: "{caption-number}",
    label: "caption-number",
    help: "Caption number in the list, zero-padded (01, 02, …).",
  },
  {
    token: "{slug}",
    label: "slug",
    help: "Short filename-friendly snippet from the start of the caption.",
  },
  {
    token: "{date}",
    label: "date",
    help: "Batch date & time, same as the output folder (e.g. 08-03-2026 04-18 PM).",
  },
];

function initNameTemplateParams() {
  const sel = $("nameParam");
  const hint = $("nameParamHint");
  const input = $("nameTemplate");
  if (!sel || !input) return;

  sel.innerHTML = NAME_PARAMS.map(
    (p) => `<option value="${p.token}">${p.label}</option>`
  ).join("");

  const updateHint = () => {
    const p = NAME_PARAMS.find((x) => x.token === sel.value) || NAME_PARAMS[0];
    hint.textContent = p ? `${p.token} — ${p.help}` : "";
  };
  sel.addEventListener("change", updateHint);
  updateHint();

  $("insertNameParam").addEventListener("click", () => {
    const token = sel.value;
    if (!token) return;
    const start = input.selectionStart ?? input.value.length;
    const end = input.selectionEnd ?? input.value.length;
    input.value = input.value.slice(0, start) + token + input.value.slice(end);
    const pos = start + token.length;
    input.focus();
    try { input.setSelectionRange(pos, pos); } catch {}
    input.dispatchEvent(new Event("input", { bubbles: true }));
    saveUiStateDebounced();
  });

  // Migrate saved templates that still use {caption01}
  if (input.value.includes("{caption01}")) {
    input.value = input.value.split("{caption01}").join("{caption-number}");
  }
}

function initBatch() {
  if (!$("mode")) return;
  $("mode").addEventListener("change", () => {
    if ($("mode").value !== "custom") {
      workspaceRenderPairs = [];
      clearRenderPairsOnSave = true;
      syncCustomModeOption();
    }
    saveUiState();
    updateCount();
    scheduleWorkspaceSave();
  });
  $("dedupe").addEventListener("change", () => { saveUiState(); updateCount(); });
  $("skipExisting").addEventListener("change", saveUiState);
  $("nameTemplate").addEventListener("input", saveUiStateDebounced);
  initNameTemplateParams();

  $("startBatch").addEventListener("click", async () => {
    if (isCreateCarouselMode()) {
      alert("Carousel posts skip burn-in render — use Upload from the dock.");
      return;
    }
    try {
      await openCreateDockPlanReview("render");
    } catch (err) {
      alert(err.message || String(err));
    }
  });

  $("cancelBatch")?.addEventListener("click", async () => {
    const id = focusedBatchId || currentBatchId;
    if (!id) return;
    await api(`/api/batch/${id}/cancel`, { method: "POST" });
  });
}

function onBatchFinished(st) {
  if ($("startBatch")) $("startBatch").disabled = false;
  if ($("dockDismiss")) $("dockDismiss").classList.remove("hidden");
  if (st && st.status === "done") lastFinishedBatchSt = st;
  syncDockCancelButtons();
  // `st` is terminal. Let the dock resolve its live focus instead of painting
  // the completed render over an active Spark job.
  if (dockFocusKind === "batch") renderDockStrip(st);
  else refreshActiveJobsDock();
  if (st.status === "done") {
    setTimeout(() => watchAutoUpload(st.id), 800);
    setTimeout(() => watchAutoUpload(st.id), 2500);
  }
}

function watchBatch(batchId) {
  if (!batchId) return;
  rememberBatchJob(batchId);
  showProgressDock();
  if ($("renderProgressBlock")) $("renderProgressBlock").classList.remove("hidden");
  if ($("tiktokUploadPanel") && dockFocusKind === "batch") $("tiktokUploadPanel").classList.add("hidden");
  if ($("facebookAdsPanel") && dockFocusKind === "batch") $("facebookAdsPanel").classList.add("hidden");
  if ($("uploadToTikTok")) $("uploadToTikTok").disabled = false;
  if ($("dockDismiss")) $("dockDismiss").classList.add("hidden");
  syncDockCancelButtons();
  pollUntil(async () => {
    const st = await api(`/api/batch/${batchId}`);
    jobSnapshots[batchId] = st;
    refreshActiveJobsDock();
    if (batchId === focusedBatchId || batchId === currentBatchId || !focusedBatchId) {
      focusedBatchId = focusedBatchId || batchId;
      currentBatchId = focusedBatchId;
      renderBatchState(st);
    }
    if (st.status === "running" || st.status === "queued") return true;
    // Keep finished state before forget clears live focus, so the dock
    // doesn't flash into an empty RENDER shell.
    lastFinishedBatchSt = st;
    onBatchFinished(st);
    forgetBatchJob(batchId);
    maybeAutoExpandDock(batchId, st.status);
    const chatDone = window.onChatBatchDone;
    if (chatDone && window._chatBatchDoneFor === batchId) {
      window.onChatBatchDone = null;
      window._chatBatchDoneFor = null;
      chatDone(st);
    } else if (chatDone && !window._chatBatchDoneFor) {
      window.onChatBatchDone = null;
      chatDone(st);
    }
    return false;
  }, { interval: 1000, key: "batch:" + batchId });
}
window.watchBatch = watchBatch;

async function pollBatch() {
  if (focusedBatchId || currentBatchId) watchBatch(focusedBatchId || currentBatchId);
}

function watchSparkJob(jobId) {
  if (!jobId) return;
  rememberSparkJob(jobId);
  showProgressDock();
  if ($("sparkFetchPanel")) $("sparkFetchPanel").classList.remove("hidden");
  if ($("renderProgressBlock") && dockFocusKind === "spark") $("renderProgressBlock").classList.add("hidden");
  if ($("tiktokUploadPanel") && dockFocusKind === "spark") $("tiktokUploadPanel").classList.add("hidden");
  syncDockCancelButtons();
  pollUntil(async () => {
    const st = await api(`/api/spark/jobs/${encodeURIComponent(jobId)}`);
    jobSnapshots[jobId] = st;
    updateSparkFetchDock(st);
    if (st.status === "running" || st.status === "queued") return true;
    lastFinishedSparkSt = st;
    forgetSparkJob(jobId);
    maybeAutoExpandDock(jobId, st.status);
    if ($("dockDismiss")) $("dockDismiss").classList.remove("hidden");
    return false;
  }, { interval: 1500, key: "spark:" + jobId });
}
window.watchSparkJob = watchSparkJob;

function renderBatchState(st) {
  if (!st) return;
  if (st.outputDir) currentBatchOutputDir = st.outputDir;
  const failed = (st.items || []).filter((i) => i.status === "failed").length;
  const skipped = (st.items || []).filter((i) => i.status === "skipped").length;
  let summary = `${st.completed} / ${st.total} finished`;
  if (st.outputFolder) summary += ` · ${st.outputFolder}`;
  if (st.status === "queued") {
    summary = `Queued #${st.queuePosition || "?"}`;
    if (st.outputFolder) summary += ` · ${st.outputFolder}`;
  }
  if (skipped) summary += ` · ${skipped} skipped`;
  if (failed) summary += ` · ${failed} failed`;
  if (st.current) {
    const pct = st.current.durationSeconds
      ? Math.round((st.current.progressSeconds / st.current.durationSeconds) * 100)
      : 0;
    summary += ` — encoding ${st.current.video} (${pct}%)`;
  }
  if (st.status === "running") {
    const eta = fmtEtaLeft(st.etaSeconds);
    if (eta) summary += ` · ${eta}`;
  }
  if (st.status === "cancelled") summary += " — cancelled";
  if ($("progressSummary")) $("progressSummary").textContent = summary;

  let frac = st.completed / Math.max(st.total, 1);
  if (st.current && st.current.durationSeconds) {
    frac += (st.current.progressSeconds / st.current.durationSeconds) / st.total;
  }
  $("progressBar").style.width = `${Math.min(frac * 100, 100)}%`;
  if ($("stageTapeFill")) $("stageTapeFill").style.width = `${Math.min(frac * 100, 100)}%`;
  if ($("previewFrame")) $("previewFrame").classList.toggle("is-encoding", st.status === "running");

  $("jobList").innerHTML = (st.items || []).map((i) => `
    <li>
      <span class="st ${i.status}">${i.status}</span>
      <span class="cap" title="${escapeHtml(i.caption)}">${escapeHtml(i.output || i.video)} — ${escapeHtml(i.caption)}</span>
      ${i.error && i.error !== "cancelled" && i.error !== "batch cancelled"
        ? `<span class="err-msg">${escapeHtml(String(i.error).slice(0, 120))}</span>` : ""}
    </li>
  `).join("");
  renderDockStrip(st);
}

// ---------------------------------------------------------------------------
// TikTok accounts + upload
// ---------------------------------------------------------------------------

function formatTtTime(ts) {
  if (!ts) return "never";
  try {
    return new Date(ts * 1000).toLocaleString();
  } catch {
    return "—";
  }
}

async function loadTtAccounts() {
  ttAccounts = await api("/api/tiktok/accounts");
  renderTtAccounts();
}

async function loadTtSettings() {
  const s = await api("/api/tiktok/settings");
  $("ttCaptionTemplate").value = s.captionTemplate || "{caption}";
  if ($("ttHashtags")) $("ttHashtags").value = s.hashtags || "";
  $("ttDelay").value = s.delaySeconds ?? 10;
  if ($("ttMinGap")) $("ttMinGap").value = s.minSecondsBetweenPostsPerAccount ?? 60;
  if ($("ttUploadEngine")) $("ttUploadEngine").value = s.uploadEngine === "http" ? "http" : "studio";
  $("ttAutoUpload").checked = !!s.autoUpload;
  $("ttContinueOnError").checked = s.continueOnError !== false;
  $("ttCopyrightChecks").checked = s.automaticCopyrightChecks !== false;
  $("ttContentChecks").checked = s.contentChecks !== false;
  ttSelectedIds = new Set(Array.isArray(s.selectedAccountIds) ? s.selectedAccountIds : []);
  applyTtEngineUi();
  renderTtAccounts();
  refreshTtIncidentBanner().catch(() => {});
}

async function saveTtSettings(extra = {}) {
  const body = {
    captionTemplate: $("ttCaptionTemplate").value,
    hashtags: $("ttHashtags") ? $("ttHashtags").value : "",
    delaySeconds: Number($("ttDelay").value) || 0,
    minSecondsBetweenPostsPerAccount: $("ttMinGap")
      ? Number($("ttMinGap").value) || 0
      : undefined,
    uploadEngine: $("ttUploadEngine") ? $("ttUploadEngine").value : "studio",
    autoUpload: $("ttAutoUpload").checked,
    continueOnError: $("ttContinueOnError").checked,
    automaticCopyrightChecks: $("ttCopyrightChecks").checked,
    contentChecks: $("ttContentChecks").checked,
    selectedAccountIds: [...ttSelectedIds],
    ...extra,
  };
  const s = await api("/api/tiktok/settings", {
    method: "PUT",
    body: JSON.stringify(body),
  });
  ttSelectedIds = new Set(Array.isArray(s.selectedAccountIds) ? s.selectedAccountIds : []);
  if ($("ttUploadEngine") && s.uploadEngine) {
    $("ttUploadEngine").value = s.uploadEngine === "http" ? "http" : "studio";
  }
  applyTtEngineUi();
  return s;
}

async function loadMcpKeys() {
  const list = $("mcpKeyList");
  if (!list) return;
  try {
    const res = await api("/api/mcp/keys");
    const keys = res.keys || [];
    if (!keys.length) {
      list.innerHTML = `<li class="hint">No MCP keys yet.</li>`;
      return;
    }
    list.innerHTML = keys
      .map(
        (k) =>
          `<li data-id="${k.id}">` +
          `<strong>${escapeHtml(k.label || k.id)}</strong> · ${k.mode} · ${escapeHtml(k.prefix || "")}… ` +
          `<button type="button" class="btn ghost mcp-key-revoke" data-id="${k.id}">Revoke</button>` +
          `</li>`
      )
      .join("");
    list.querySelectorAll(".mcp-key-revoke").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const id = btn.getAttribute("data-id");
        if (!id || !confirm("Revoke this MCP key?")) return;
        await api(`/api/mcp/keys/${id}`, { method: "DELETE" });
        await loadMcpKeys();
      });
    });
  } catch (err) {
    list.innerHTML = `<li class="hint">${escapeHtml(err.message || "Failed to load keys")}</li>`;
  }
}

function wireMcpKeys() {
  const createBtn = $("mcpKeyCreate");
  if (!createBtn || createBtn.dataset.wired) return;
  createBtn.dataset.wired = "1";
  createBtn.addEventListener("click", async () => {
    const once = $("mcpKeyOnce");
    try {
      const res = await api("/api/mcp/keys", {
        method: "POST",
        body: JSON.stringify({
          label: ($("mcpKeyLabel") && $("mcpKeyLabel").value) || "MCP key",
          mode: ($("mcpKeyMode") && $("mcpKeyMode").value) || "trusted",
        }),
      });
      if (once) {
        once.textContent = `Copy now (shown once): ${res.key || ""}`;
        once.classList.remove("error");
      }
      await loadMcpKeys();
    } catch (err) {
      if (once) {
        once.textContent = err.message || "Create failed";
        once.classList.add("error");
      }
    }
  });
  loadMcpKeys().catch(() => {});
}

function applyTtEngineUi() {
  const eng = $("ttUploadEngine") ? $("ttUploadEngine").value : "studio";
  const isHttp = eng === "http";
  const wrapCopy = $("ttCopyrightChecksWrap");
  const wrapContent = $("ttContentChecksWrap");
  const hint = $("ttStudioChecksHint");
  const engHint = $("ttEngineHint");
  if (wrapCopy) wrapCopy.classList.toggle("is-disabled", isHttp);
  if (wrapContent) wrapContent.classList.toggle("is-disabled", isHttp);
  if ($("ttCopyrightChecks")) $("ttCopyrightChecks").disabled = isHttp;
  if ($("ttContentChecks")) $("ttContentChecks").disabled = isHttp;
  if (hint) {
    hint.textContent = isHttp
      ? "Copyright / content check toggles apply only to the Studio (browser) engine."
      : "Controls TikTok Studio's Music copyright check and Content check lite on upload.";
  }
  if (engHint) {
    engHint.textContent = isHttp
      ? "HTTP posts via TikTok’s web upload endpoints (faster). Accounts need a full cookie set from Remote browser — sessionid-only is not enough."
      : "Studio drives the TikTok Studio UI in a browser. Login is the same for both engines.";
  }
  renderTtHttpCookieBanner();
}

function renderTtHttpCookieBanner() {
  const eng = $("ttUploadEngine") ? $("ttUploadEngine").value : "studio";
  const banner = $("ttIncidentBanner");
  if (!banner || eng !== "http") return;
  const selected = ttAccounts.filter((a) => {
    if (ttSelectedIds.size === 0) return a.enabled && a.hasCookies;
    return ttSelectedIds.has(a.id);
  });
  const bad = selected.filter((a) => a.cookieHealth && !a.cookieHealth.httpReady);
  if (!bad.length) return;
  banner.classList.remove("hidden");
  banner.innerHTML = `<strong>${bad.length} selected account(s)</strong> need a full cookie refresh before HTTP upload.
    <button type="button" class="btn ghost" id="ttBannerGotoAccounts">View accounts</button>`;
  const btn = $("ttBannerGotoAccounts");
  if (btn) {
    btn.onclick = () => {
      const nav = document.querySelector('[data-settings="tiktok"]');
      if (nav) nav.click();
    };
  }
}

function saveTtSettingsDebounced() {
  clearTimeout(ttSettingsSaveTimer);
  ttSettingsSaveTimer = setTimeout(() => {
    saveTtSettings().catch(() => {});
  }, 400);
}

function normalizeHashtagsClient(raw) {
  const parts = [];
  const seen = new Set();
  for (const tok of String(raw || "").split(/[\s,]+/)) {
    const t = tok.trim();
    if (!t) continue;
    const tag = "#" + t.replace(/^#+/, "");
    if (tag === "#" || seen.has(tag.toLowerCase())) continue;
    seen.add(tag.toLowerCase());
    parts.push(tag);
  }
  return parts.join(" ");
}

function renderTtDescription(template, opts) {
  const o = opts || {};
  let text = template == null ? "{caption}" : String(template);
  const stem = String(o.filename || "").replace(/\.[^.]+$/, "");
  const idx = Number(o.index) || 0;
  const replacements = {
    "{caption}": o.caption || "",
    "{filename}": stem,
    "{account}": o.account || "",
    "{account_suffix}": o.accountSuffix || "",
    "{index}": String(idx + 1),
    "{index01}": String(idx + 1).padStart(2, "0"),
  };
  Object.keys(replacements).forEach((k) => {
    text = text.split(k).join(replacements[k]);
  });
  const tags = normalizeHashtagsClient(o.hashtags);
  if (tags) text = (String(text).trim() ? String(text).replace(/\s+$/, "") + " " : "") + tags;
  return String(text).slice(0, 2200);
}

function ttReadyPool(forceAccountId) {
  const ready = (a) => a.enabled && a.hasCookies && a.status !== "missing_cookies";
  const force = String(forceAccountId || "").trim();
  if (force) {
    const hit = ttAccounts.find((a) => a.id === force && ready(a));
    return hit ? [hit] : [];
  }
  if (ttSelectedIds.size === 0) return ttAccounts.filter(ready);
  return ttAccounts.filter((a) => ttSelectedIds.has(a.id) && ready(a));
}

function goToTtSettings() {
  closeTtUploadSheet();
  window.location.hash = "#/settings";
  const btn = document.querySelector('[data-settings="tiktok"]');
  if (btn) btn.click();
}

let ttSheetContext = null;
let ttSheetVideos = [];
/** When set, poll updates may drive the sheet progress UI for this upload only. */
let ttSheetBoundUploadId = null;
/** Avoid double-firing completion hooks / results for the same upload id. */
let ttSheetCompleteNotified = new Set();
/** Completed result sheets the user has dismissed; polling must not reopen them. */
let ttSheetResultsDismissed = new Set();
/** Latest spark codes keyed for the open results sheet (from spark job poll). */
let ttSheetResultSparks = [];

function closeTtUploadSheet() {
  const el = $("ttUploadSheet");
  const wasShowingResults = $("ttSheetResults") && !$("ttSheetResults").classList.contains("hidden");
  if (wasShowingResults && ttSheetBoundUploadId) {
    ttSheetResultsDismissed.add(ttSheetBoundUploadId);
  }
  if (el) el.classList.add("hidden");
  ttSheetBoundUploadId = null;
  setTtSheetMode("setup");
  if (typeof window.syncModalOpen === "function") window.syncModalOpen();
}

function setTtSheetMsg(msg, isError) {
  const el = $("ttSheetMsg");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError && !!msg);
}

async function loadTtSheetVideos(opts) {
  if (!outputItems.length) {
    try { await loadOutputs(); } catch {}
  }
  const byPath = Object.fromEntries((outputItems || []).map((it) => [it.path, it]));
  if (opts.paths && opts.paths.length) {
    return opts.paths.map((p) => {
      const cat = byPath[p] || {};
      return {
        path: p,
        filename: cat.filename || String(p).split("/").pop(),
        hook: cat.hook || "",
        tiktokStatus: cat.tiktokStatus || "",
        onTiktok: !!cat.onTiktok || cat.tiktokStatus === "done",
      };
    });
  }
  if (opts.batchId) {
    const st = await api(`/api/batch/${opts.batchId}`);
    let ttMap = {};
    try {
      const tt = await api(`/api/batch/${opts.batchId}/tiktok-status`);
      ttMap = Object.fromEntries(
        (tt.items || []).map((it) => [
          `${it.sourceId}\0${it.caption}`,
          it,
        ])
      );
    } catch {}
    return (st.items || [])
      .filter((i) => (i.status === "done" || i.status === "skipped") && i.output)
      .map((i) => {
        const cat = byPath[i.output] || {};
        const tt = ttMap[`${i.sourceId || ""}\0${i.caption || ""}`] || {};
        const onTt = !!cat.onTiktok || cat.tiktokStatus === "done" || !!tt.onTiktok;
        return {
          path: i.output,
          filename: cat.filename || String(i.output).split("/").pop(),
          hook: cat.hook || i.caption || "",
          sourceId: i.sourceId || "",
          caption: i.caption || "",
          tiktokStatus: onTt ? "done" : (cat.tiktokStatus || tt.tiktokStatus || ""),
          onTiktok: onTt,
        };
      });
  }
  return [];
}

function fillTtSheetFromSettings() {
  if ($("ttSheetTemplate")) $("ttSheetTemplate").value = $("ttCaptionTemplate")?.value || "{caption}";
  if ($("ttSheetHashtags")) $("ttSheetHashtags").value = $("ttHashtags")?.value || "";
  if ($("ttSheetDelay")) $("ttSheetDelay").value = $("ttDelay")?.value || 10;
  if ($("ttSheetContinueOnError")) $("ttSheetContinueOnError").checked = $("ttContinueOnError")?.checked !== false;
  if ($("ttSheetCopyrightChecks")) $("ttSheetCopyrightChecks").checked = $("ttCopyrightChecks")?.checked !== false;
  if ($("ttSheetContentChecks")) $("ttSheetContentChecks").checked = $("ttContentChecks")?.checked !== false;
  if ($("ttSheetSaveDefaults")) $("ttSheetSaveDefaults").checked = false;
}

function renderTtSheetAccounts() {
  const el = $("ttSheetAccountSummary");
  if (!el) return;
  const forceId = String((ttSheetContext && ttSheetContext.accountId) || "").trim();
  const pool = ttReadyPool(forceId);
  if (forceId) {
    if (!pool.length) {
      el.innerHTML = `Plan account is not ready. <button type="button" class="btn ghost" id="ttSheetOpenSettings">Open Settings</button>`;
    } else {
      const name =
        (ttSheetContext && ttSheetContext.accountName) || pool[0].name || forceId;
      el.textContent = `Posting as @${name} (plan account — ignores Settings round-robin)`;
    }
    const btn = $("ttSheetOpenSettings");
    if (btn) btn.addEventListener("click", goToTtSettings);
    return;
  }
  if (!pool.length) {
    el.innerHTML = `No TikTok accounts ready. <button type="button" class="btn ghost" id="ttSheetOpenSettings">Open Settings</button>`;
    const btn = $("ttSheetOpenSettings");
    if (btn) btn.addEventListener("click", goToTtSettings);
    return;
  }
  const names = pool.map((a) => a.name).join(" · ");
  el.textContent = `${pool.length} account${pool.length === 1 ? "" : "s"} in round-robin: ${names}`;
}

function renderTtSheetVideos() {
  const root = $("ttSheetVideos");
  if (!root) return;
  const forceId = String((ttSheetContext && ttSheetContext.accountId) || "").trim();
  const pool = ttReadyPool(forceId);
  const template = $("ttSheetTemplate")?.value || "{caption}";
  const hashtags = $("ttSheetHashtags")?.value || "";
  if (!ttSheetVideos.length) {
    root.innerHTML = `<p class="hint">No finished videos to upload.</p>`;
    refreshTtSheetEta();
    return;
  }
  root.innerHTML = ttSheetVideos.map((v, i) => {
    const acct = pool.length ? pool[forceId ? 0 : i % pool.length] : null;
    const desc = renderTtDescription(template, {
      caption: v.hook || "",
      filename: v.filename,
      account: acct?.name || "",
      accountSuffix: acct?.captionSuffix || "",
      hashtags,
      index: i,
    });
    const playUrl = `/api/output/${encodeURIComponent(v.path).replace(/%2F/g, "/")}`;
    const posted = v.onTiktok || v.tiktokStatus === "done";
    const ttBadge = posted
      ? `<span class="tt-badge tt-badge-posted">On TikTok</span>`
      : (v.tiktokStatus === "failed"
        ? `<span class="tt-badge tt-badge-failed">Failed</span>`
        : "");
    return `<div class="tt-sheet-video${posted ? " is-posted" : ""}">
      <video data-auth-src="${escapeHtml(playUrl)}" muted preload="metadata"></video>
      <div>
        <div class="tt-sheet-file" title="${escapeHtml(v.path)}">${escapeHtml(v.filename)} ${ttBadge}</div>
        <div class="tt-sheet-desc">${escapeHtml(desc || "(empty description)")}</div>
        <div class="tt-sheet-acct">${acct ? escapeHtml("→ " + acct.name) : "No account assigned"}</div>
      </div>
    </div>`;
  }).join("");
  if (typeof hydrateAuthMedia === "function") hydrateAuthMedia(root);
  refreshTtSheetEta();
}

function refreshTtSheetEta() {
  const el = $("ttSheetEta");
  if (!el) return;
  const n = ttSheetVideos.length;
  const delay = Number($("ttSheetDelay")?.value) || 0;
  const eng = ($("ttUploadEngine") && $("ttUploadEngine").value) || "studio";
  const sec = estimateUploadSeconds(n, delay, eng);
  const t = fmtDuration(sec);
  if (!n || !t) {
    el.textContent = "";
    return;
  }
  const via = eng === "http" ? "HTTP" : "Studio";
  el.textContent =
    `About ${t} to upload ${n} video${n === 1 ? "" : "s"} via ${via}` +
    (delay ? ` · ${delay}s between posts` : "");
}

async function openTtUploadSheet(opts) {
  ttSheetContext = opts || {};
  // Opening setup for a (possibly new) upload — do not hijack into an in-flight job's progress.
  ttSheetBoundUploadId = null;
  ttSheetResultSparks = [];
  setTtSheetMsg("");
  setTtSheetMode("setup");
  try {
    await Promise.all([
      loadTtAccounts().catch(() => {}),
      loadTtSettings().catch(() => {}),
    ]);
  } catch {}
  fillTtSheetFromSettings();
  try {
    ttSheetVideos = await loadTtSheetVideos(ttSheetContext);
  } catch (err) {
    ttSheetVideos = [];
    setTtSheetMsg(err.message || String(err), true);
  }
  renderTtSheetAccounts();
  renderTtSheetVideos();
  const modal = $("ttUploadSheet");
  if (modal) modal.classList.remove("hidden");
  if (typeof window.syncModalOpen === "function") window.syncModalOpen();
  const forceId = String((ttSheetContext && ttSheetContext.accountId) || "").trim();
  const canStart = !!ttSheetVideos.length && !!ttReadyPool(forceId).length;
  if ($("ttSheetStart")) {
    $("ttSheetStart").disabled = !canStart;
  }
  // Plan Upload flow says "then Upload…" — actually start, don't leave the sheet idle.
  if (ttSheetContext && ttSheetContext.autoStart && canStart) {
    ttSheetContext.autoStart = false;
    await startTtUploadFromSheet();
  } else if (ttSheetContext && ttSheetContext.autoStart && !canStart) {
    ttSheetContext.autoStart = false;
    const msg = !ttSheetVideos.length
      ? "No rendered videos ready to upload."
      : forceId
        ? "Plan account is not ready — sign in or pick another on the plan."
        : "Sign in a TikTok account first.";
    setTtSheetMsg(msg, true);
    if (typeof showToast === "function") showToast(msg, { error: true });
  }
}

async function startTtUploadFromSheet() {
  if (!ttSheetContext) return;
  const forceId = String((ttSheetContext && ttSheetContext.accountId) || "").trim();
  const pool = ttReadyPool(forceId);
  if (!pool.length) {
    setTtSheetMsg(
      forceId
        ? "Plan account is not ready — sign in or pick another on the plan."
        : "Sign in a TikTok account first.",
      true
    );
    return;
  }
  if (!ttSheetVideos.length && !ttSheetContext.batchId) {
    setTtSheetMsg("No videos to upload.", true);
    return;
  }
  const body = {
    captionTemplate: $("ttSheetTemplate")?.value || "{caption}",
    hashtags: $("ttSheetHashtags")?.value || "",
    delaySeconds: Number($("ttSheetDelay")?.value) || 0,
    continueOnError: !!$("ttSheetContinueOnError")?.checked,
    automaticCopyrightChecks: !!$("ttSheetCopyrightChecks")?.checked,
    contentChecks: !!$("ttSheetContentChecks")?.checked,
  };
  if (forceId) body.accountId = forceId;
  if (ttSheetContext.batchId) body.batchId = ttSheetContext.batchId;
  else body.paths = ttSheetContext.paths || ttSheetVideos.map((v) => v.path);

  try {
    if ($("ttSheetStart")) $("ttSheetStart").disabled = true;
    if ($("ttSheetSaveDefaults")?.checked) {
      if ($("ttCaptionTemplate")) $("ttCaptionTemplate").value = body.captionTemplate;
      if ($("ttHashtags")) $("ttHashtags").value = body.hashtags;
      if ($("ttDelay")) $("ttDelay").value = body.delaySeconds;
      if ($("ttContinueOnError")) $("ttContinueOnError").checked = body.continueOnError;
      if ($("ttCopyrightChecks")) $("ttCopyrightChecks").checked = body.automaticCopyrightChecks;
      if ($("ttContentChecks")) $("ttContentChecks").checked = body.contentChecks;
      await saveTtSettings();
    }
    const res = await api("/api/tiktok/upload", {
      method: "POST",
      body: JSON.stringify(body),
    });
    const id = res.uploadId || res.id;
    rememberUploadJob(id, res.sourceBatchId || body.batchId);
    currentUploadId = id;
    ttSheetBoundUploadId = id;
    setTtSheetMode("progress");
    startWatchingUpload();
  } catch (err) {
    setTtSheetMsg(err.message || String(err), true);
    if (typeof showToast === "function") {
      showToast(err.message || String(err), { error: true });
    }
  } finally {
    if ($("ttSheetStart")) $("ttSheetStart").disabled = false;
  }
}

function setTtSheetUploadMode(running) {
  setTtSheetMode(running ? "progress" : "setup");
}

function setTtSheetMode(mode) {
  const setup = $("ttSheetSetup");
  const progress = $("ttSheetProgress");
  const results = $("ttSheetResults");
  const title = $("ttSheetTitle");
  const isSetup = mode === "setup";
  const isProgress = mode === "progress";
  const isResults = mode === "results";
  if (setup) setup.classList.toggle("hidden", !isSetup);
  if (progress) progress.classList.toggle("hidden", !isProgress);
  if (results) results.classList.toggle("hidden", !isResults);
  if (title) {
    title.textContent = isProgress
      ? "Uploading to TikTok"
      : isResults
        ? "Upload results"
        : "Upload to TikTok";
  }
  if ($("ttSheetStart")) $("ttSheetStart").classList.toggle("hidden", !isSetup);
  if ($("ttSheetCancel")) $("ttSheetCancel").classList.toggle("hidden", !isSetup);
  if ($("ttSheetHide")) $("ttSheetHide").classList.toggle("hidden", isSetup || isResults);
  if ($("ttSheetDone")) $("ttSheetDone").classList.toggle("hidden", !isResults);
  if ($("ttSheetCopySparks")) $("ttSheetCopySparks").classList.toggle("hidden", true);
  if ($("ttSheetCopyLinks")) $("ttSheetCopyLinks").classList.toggle("hidden", true);
}

function ttSheetResultItemsFromUpload(st) {
  return (st && st.items ? st.items : []).map((i) => {
    const label = uploadItemStatusLabel(i);
    const sparkHit =
      ttSheetResultSparks.find(
        (s) =>
          (s.postUrl && i.postUrl && s.postUrl === i.postUrl) ||
          (s.filename && i.filename && s.filename === i.filename) ||
          (s.caption && i.caption && s.caption === i.caption)
      ) || null;
    return {
      filename: i.filename || "",
      caption: i.caption || i.description || "",
      accountName: i.accountName || "",
      status: label,
      error: i.error || "",
      postUrl: i.postUrl || (sparkHit && sparkHit.postUrl) || "",
      sparkCode: (sparkHit && sparkHit.sparkCode) || "",
      sparkStatus: (sparkHit && sparkHit.status) || "",
      sparkError: (sparkHit && sparkHit.error) || "",
    };
  });
}

function renderTtSheetResultsList(items) {
  const list = $("ttSheetResultsList");
  if (!list) return;
  list.innerHTML = (items || [])
    .map((i) => {
      const stClass = i.status || "pending";
      const spark = String(i.sparkCode || "").trim();
      const link = String(i.postUrl || "").trim();
      const sparkLine = spark
        ? `<span class="tt-result-spark" title="${escapeHtml(spark)}">${escapeHtml(spark)}</span>`
        : i.sparkStatus === "running"
          ? `<span class="tt-result-spark">Fetching spark…</span>`
          : i.sparkStatus === "failed"
            ? `<span class="tt-result-spark err">${escapeHtml(i.sparkError || "Spark failed")}</span>`
            : "";
      const linkLine = link
        ? `<a class="tt-result-link" href="${escapeHtml(link)}" target="_blank" rel="noopener">${escapeHtml(link)}</a>`
        : "";
      return `<li>
        <span class="st ${escapeHtml(stClass)}">${escapeHtml(stClass)}</span>
        <span class="tt-result-meta">
          <span class="cap" title="${escapeHtml(i.caption || "")}">${escapeHtml(i.filename || "")}${
            i.accountName ? " → " + escapeHtml(i.accountName) : ""
          }</span>
          ${sparkLine}
          ${linkLine}
          ${i.error && stClass !== "uploaded" && stClass !== "done" ? `<span class="err">${escapeHtml(String(i.error).slice(0, 200))}</span>` : ""}
        </span>
      </li>`;
    })
    .join("");
}

function syncTtSheetResultActions(items) {
  const codes = (items || [])
    .map((i) => String(i.sparkCode || "").trim())
    .filter(Boolean);
  const links = (items || [])
    .map((i) => String(i.postUrl || "").trim())
    .filter(Boolean);
  if ($("ttSheetCopySparks")) {
    $("ttSheetCopySparks").classList.toggle("hidden", !codes.length);
    $("ttSheetCopySparks").disabled = !codes.length;
  }
  if ($("ttSheetCopyLinks")) {
    $("ttSheetCopyLinks").classList.toggle("hidden", !links.length);
    $("ttSheetCopyLinks").disabled = !links.length;
  }
}

function showTtUploadResultsSheet(st, opts = {}) {
  const modal = $("ttUploadSheet");
  if (!modal || !st) return;
  ttSheetBoundUploadId = st.id || ttSheetBoundUploadId;
  const uploaded =
    st.uploaded ?? (st.items || []).filter((i) => uploadItemStatusLabel(i) === "uploaded" || i.status === "done").length;
  const failed = st.failed ?? (st.items || []).filter((i) => i.status === "failed").length;
  const total = st.total || (st.items || []).length;
  const cancelled = st.status === "cancelled";
  let outcome = "ok";
  let summary = `Upload complete — ${uploaded}/${total} posted`;
  if (cancelled) {
    outcome = failed || uploaded ? "warn" : "err";
    summary = `Upload cancelled — ${uploaded}/${total} posted`;
  } else if (failed && !uploaded) {
    outcome = "err";
    summary = `Upload failed — 0/${total} posted`;
  } else if (failed) {
    outcome = "warn";
    summary = `Upload finished with errors — ${uploaded}/${total} posted · ${failed} failed`;
  }
  if (opts.sparkMessage) summary += ` — ${opts.sparkMessage}`;

  const items = ttSheetResultItemsFromUpload(st);
  const results = $("ttSheetResults");
  if (results) results.dataset.outcome = outcome;
  if ($("ttSheetResultsSummary")) $("ttSheetResultsSummary").textContent = summary;
  if ($("ttSheetResultsSparkStatus")) {
    $("ttSheetResultsSparkStatus").textContent = opts.sparkStatusText || "";
  }
  renderTtSheetResultsList(items);
  syncTtSheetResultActions(items);
  setTtSheetMode("results");
  if ($("ttSheetTitle")) {
    $("ttSheetTitle").textContent =
      outcome === "err" ? "Upload failed" : cancelled ? "Upload cancelled" : "Upload complete";
  }
  modal.classList.remove("hidden");
  if (typeof window.syncModalOpen === "function") window.syncModalOpen();
}

function renderSparkFetchState(st) {
  if (!st) return;
  const sparkTotal = sparkJobTotal(st);
  const total = Math.max(sparkTotal, 1);
  const fetched = st.completed || 0;
  const failed = st.failed || 0;
  const settled = Math.min(fetched + failed, total);
  const summary = st.status === "queued"
    ? `Spark fetch queued — ${fetched}/${st.total || 0}`
    : st.status === "done"
      ? `Fetched ${fetched}/${sparkTotal} Spark code${fetched === 1 ? "" : "s"}`
      : `Fetching Spark codes ${fetched}/${sparkTotal}`;
  if ($("sparkFetchSummary")) $("sparkFetchSummary").textContent = summary;
  if ($("sparkFetchBar")) $("sparkFetchBar").style.width = `${Math.round((settled / total) * 100)}%`;
  if ($("sparkFetchList")) {
    $("sparkFetchList").innerHTML = (st.items || []).map((item, index) => {
      const status = item.status || "pending";
      const label = item.filename || item.caption || `Video ${index + 1}`;
      const detail = item.sparkCode || item.error || item.message || "";
      return `<li><span class="st ${escapeHtml(status)}">${escapeHtml(status)}</span><span class="cap">${escapeHtml(label)}</span>${detail ? `<span class="err-msg">${escapeHtml(detail)}</span>` : ""}</li>`;
    }).join("");
  }
}

function updateSparkFetchDock(sparkState) {
  if (!sparkState || !sparkState.id) return;
  const items = sparkState.items || [];
  const fetched = items.filter((item) => String(item.sparkCode || "").trim()).length;
  const failed = items.filter((item) => item.status === "failed").length;
  const st = {
    ...sparkState,
    kind: "spark",
    total: items.length || sparkJobTotal(sparkState),
    completed: fetched,
    failed,
  };
  jobSnapshots[st.id] = st;
  const running = st.status === "running" || st.status === "queued";
  if (running) {
    rememberSparkJob(st.id);
    const preferred = selectPreferredSparkJob();
    if (preferred) currentSparkJobId = preferred;
    lastFinishedSparkSt = null;
    showProgressDock();
  } else {
    forgetSparkJob(st.id);
    lastFinishedSparkSt = st;
    showProgressDock();
  }
  // Both Spark job pollers call this. Only the job selected in the dock may
  // paint the large detail panel; otherwise it appears to bounce between the
  // running job and the queued one even though the phone queue is correct.
  if (st.id === currentSparkJobId) renderSparkFetchState(st);
  refreshActiveJobsDock();
}

function renderProvisionDockState(st) {
  if (!st) return;
  const complete = st.completed || 0;
  if ($("provisionProgressSummary")) {
    $("provisionProgressSummary").textContent = st.message || "Business Center work in progress";
  }
  if ($("provisionProgressBar")) {
    $("provisionProgressBar").style.width = `${complete}%`;
  }
  if ($("provisionProgressList")) {
    $("provisionProgressList").innerHTML = (st.items || []).map((item) => {
      if (item.workflowId) {
        const action = item.workflowState === "paused" ? "resume" : "pause";
        return `<li class="bc-workflow-row" data-workflow="${escapeHtml(item.workflowId)}">
          <span class="st ${escapeHtml(item.workflowState)}">${escapeHtml(item.workflowState === "paused" ? "Paused" : item.stepStatus === "pending" ? "Queued" : "Running")}</span>
          <span class="cap"><strong>${escapeHtml(item.title)}</strong> · ${escapeHtml(item.profileLabel)}<br><small>${escapeHtml(item.purpose)}</small></span>
          <button type="button" class="btn bc-workflow-control" data-action="${action}">${action === "resume" ? "Resume" : "Pause"}</button>
          <button type="button" class="btn danger bc-workflow-control" data-action="cancel">Cancel</button>
        </li>`;
      }
      const phase = String(item.status || "pending");
      const step = item.step || phase.replace(/_/g, " ");
      const title = item.email || item.profileId || "New Business Center profile";
      const facts = [
        item.geo,
        item.adspowerProfileId && `AdsPower ${item.adspowerProfileId}`,
        item.proxyId && `Proxy ${item.proxyId}`,
        item.progressDetail,
      ].filter(Boolean).join(" · ");
      const error = item.error || "";
      return `<li title="${escapeHtml([title, facts, error].filter(Boolean).join(" — "))}">
        <span class="st ${escapeHtml(phase)}">${escapeHtml(step)}</span>
        <span class="cap">${escapeHtml(title)}${facts ? ` · ${escapeHtml(facts)}` : ""}</span>
        ${error ? `<span class="err-msg">${escapeHtml(error)}</span>` : ""}
      </li>`;
    }).join("");
  }
}

function updateProvisionDock({ active = [], profiles = [], workflows = [], source = "bc" } = {}) {
  // The global job sync has no inventory profiles. While the BC page is
  // polling, its richer snapshot owns the dock so the two pollers cannot
  // alternate between a real phase and a generic 0% waiting state.
  if (source === "background" && Date.now() - lastProvisionDetailAt < 10000) return;
  if (source === "bc") lastProvisionDetailAt = Date.now();
  if (source === "bc" && (profiles || []).length) {
    const finished = new Set((profiles || [])
      .filter((p) => p && (["done", "scheduled"].includes(String(p.bcDeactivateStatus || ""))))
      .map((p) => String(p.id || "")));
    workflows = (workflows || []).filter((w) =>
      !finished.has(String(w.profileId || "")) ||
      !["deactivate_ad_accounts", "deactivate_business_center"].includes(String(w.kind || ""))
    );
  }
  if (source === "bc") currentProvisionWorkflows = workflows;
  else workflows = currentProvisionWorkflows;
  const pipelineStates = new Set([
    "queued", "pending", "creating", "awaiting_worker", "browser_open",
    "awaiting_manual_login", "logging_in", "opening_bc", "billing", "adding_balance",
    "creating_accounts", "setting_budgets", "setup_bc", "white_hat", "sales_campaign",
  ]);
  const byProfileId = new Map((profiles || []).filter(Boolean).map((p) => [String(p.id || ""), p]));
  const workflowProfileIds = new Set((workflows || []).map((w) => String(w.profileId || "")).filter(Boolean));
  // The job list can lag the inventory refresh after a Spark/ad launch.  Keep
  // the owning job ids from the live profile too, so the bottom-bar Cancel is
  // available whenever a BC workflow is visibly active.
  const activeJobIds = new Set((active || []).map((job) => String(job.id || "")).filter(Boolean));
  const items = [];
  const seen = new Set();
  for (const profile of profiles || []) {
    const status = String(profile && profile.status || "");
    const profileId = String(profile && profile.id || "");
    const accountDeactivate = String(profile && profile.adAccountsDeactivateStatus || "");
    const bcDeactivate = String(profile && profile.bcDeactivateStatus || "");
    if (profile && !workflowProfileIds.has(profileId) &&
        (["needed", "running"].includes(accountDeactivate) || ["needed", "running"].includes(bcDeactivate))) {
      const closingBc = accountDeactivate === "done" || ["needed", "running"].includes(bcDeactivate);
      items.push({
        profileId,
        status: closingBc ? "deactivate_business_center" : "deactivate_ad_accounts",
        email: profile.profileNo ? `Profile ${profile.profileNo}` : profile.bcName || profile.email || profileId,
        progressDetail: closingBc
          ? "Close the Business Center after its ad accounts are deactivated"
          : "Close every ad account after Business Center disapproval",
      });
      continue;
    }
    if (!profile || !pipelineStates.has(status)) continue;
    if (profile.jobId) activeJobIds.add(String(profile.jobId));
    const id = String(profile.id || profile.email || `profile-${items.length}`);
    const activeAccounts = (profile.accounts || []).filter((account) => {
      const sales = String(account && account.salesStatus || "").toLowerCase();
      const whiteHat = String(account && account.whiteHatStatus || "").toLowerCase();
      return sales === "creating" || ["pending", "submitted", "creating"].includes(whiteHat);
    });
    if (activeAccounts.length && ["white_hat", "sales_campaign"].includes(status)) {
      for (const account of activeAccounts) {
        const sales = String(account.salesStatus || "").toLowerCase();
        const wh = String(account.whiteHatStatus || "").toLowerCase();
        // Posted (Publish clicked) → waiting on TikTok review, not launching.
        const inReview = sales !== "creating" && (wh === "submitted" || wh === "pending");
        const accountStatus = sales === "creating"
          ? "sales_campaign"
          : inReview ? "white_hat_review" : "white_hat";
        const accountId = String(account.accountId || account.accountName || items.length + 1);
        let detail = accountStatus === "sales_campaign" ? "Launching real ads" : "Launching white-hat";
        if (inReview) {
          const next = Date.parse(account.whiteHatNextCheckAt || "");
          const pub = Date.parse(account.whiteHatPublishedAt || "");
          const due = Number.isFinite(next) ? next : Number.isFinite(pub) ? pub + 600_000 : NaN;
          const mins = Number.isFinite(due) ? Math.max(0, Math.round((due - Date.now()) / 60_000)) : null;
          detail = `White-hat in review${mins === null ? "" : mins > 0 ? ` · next check in ${mins}m` : " · checking now"}`;
        }
        items.push({
          profileId: `${id}:${accountId}`,
          status: accountStatus,
          email: `${profile.email || id} · ${account.accountName || `Account ${accountId}`}`,
          geo: profile.geo || "",
          adspowerProfileId: account.accountId || profile.adspowerProfileId || "",
          proxyId: profile.proxyId || "",
          progressDetail: detail,
          error: account.salesError || account.whiteHatError || profile.lastError || "",
        });
      }
      seen.add(id);
      continue;
    }
    seen.add(id);
    items.push({
      profileId: id,
      status,
      email: profile.email || "",
      geo: profile.geo || "",
      adspowerProfileId: profile.adspowerProfileId || "",
      proxyId: profile.proxyId || "",
      progressDetail: profile.progressDetail || profile.progressAction || "",
      error: profile.lastError || profile.failReason || "",
    });
  }
  for (const job of active || []) {
    const before = items.length;
    for (const item of job.items || []) {
      const id = String(item.profileId || "");
      if (id && seen.has(id)) continue;
      const profile = byProfileId.get(id) || {};
      const status = String(profile.status || item.status || job.status || "pending");
      if (!pipelineStates.has(status)) continue;
      seen.add(id || `${job.id}-${item.index}`);
      items.push({
        profileId: id,
        status,
        email: profile.email || item.email || "",
        geo: profile.geo || item.geo || job.geo || "",
        adspowerProfileId: profile.adspowerProfileId || item.adspowerProfileId || "",
        proxyId: profile.proxyId || job.proxyId || "",
        progressDetail: profile.progressDetail || job.message || "",
        error: profile.lastError || item.error || "",
      });
    }
    // A queued job may have no profiles yet, and a running job can briefly
    // expose only generic item statuses. Keep its activity visible until the
    // inventory supplies a more specific phase.
    if (items.length === before) {
      items.push({
        profileId: String(job.id || ""),
        status: "pending",
        email: job.message || "Business Center job",
        geo: job.geo || "",
      });
    }
  }
  for (const workflow of workflows || []) {
    if (!workflow.id || !workflow.title) continue;
    items.push({
      workflowId: String(workflow.id),
      workflowState: String(workflow.state || "active"),
      stepStatus: String(workflow.stepStatus || ""),
      title: String(workflow.title),
      purpose: String(workflow.purpose || "Business Center worker step"),
      profileLabel: String(workflow.profileLabel || workflow.profileId || "Business Center"),
      status: "workflow",
    });
  }
  if (!items.length) {
    currentProvisionDockSt = null;
    // The provision job ids are only meaningful while this snapshot has live
    // work. Leaving them tracked keeps the dock open with no focused state,
    // which paints the default, empty "Render" strip.
    if (activeProvisionIds.size) {
      activeProvisionIds.clear();
      writeActiveJobsLs();
    }
    if (dockFocusKind === "provision") focusNextLiveDockJob();
    refreshActiveJobsDock();
    return;
  }
  const phasePct = {
    queued: 0, pending: 0, creating: 5, awaiting_worker: 8, browser_open: 15,
    awaiting_manual_login: 20, logging_in: 28, opening_bc: 35, billing: 45,
    adding_balance: 52, creating_accounts: 60, setting_budgets: 68, setup_bc: 72,
    white_hat: 82, white_hat_review: 88, sales_campaign: 92,
  };
  const completed = Math.round(items.reduce((sum, item) => sum + (phasePct[item.status] || 0), 0) / items.length);
  const phaseLabel = {
    queued: "Waiting for worker", pending: "Waiting for worker", creating: "Creating profile",
    awaiting_worker: "Waiting for worker", browser_open: "Opening browser",
    awaiting_manual_login: "Waiting for login", logging_in: "Signing in",
    opening_bc: "Opening Business Center", billing: "Adding payment", adding_balance: "Adding balance",
    creating_accounts: "Creating ad accounts", setting_budgets: "Setting budgets",
    setup_bc: "Finishing Business Center setup", white_hat: "Launching white-hat",
    white_hat_review: "White-hat in review (checked every 10 min)",
    sales_campaign: "Launching real ads",
    deactivate_ad_accounts: "Deactivate ad accounts",
    deactivate_business_center: "Deactivate Business Center",
  };
  const phaseCounts = new Map();
  for (const item of items) {
    if (item.workflowId) continue;
    const label = phaseLabel[item.status] || "Working";
    item.step = label;
    phaseCounts.set(label, (phaseCounts.get(label) || 0) + 1);
  }
  const workflowMessage = (workflows || []).map((w) => `${w.title} · ${w.profileLabel || w.profileId || "Business Center"}`);
  const deactivateMessage = items.filter((item) =>
    item.status === "deactivate_ad_accounts" || item.status === "deactivate_business_center"
  ).map((item) => `${item.step} · ${item.email} · ${item.progressDetail}`);
  const message = [
    ...workflowMessage,
    ...deactivateMessage,
    ...[...phaseCounts.entries()]
      .filter(([label]) => label !== "Deactivate ad accounts" && label !== "Deactivate Business Center")
      .map(([label, count]) => `${label}${count > 1 ? ` (${count})` : ""}`),
  ].join(" · ");
  currentProvisionDockSt = {
    id: "provision-activity",
    kind: "provision",
    status: "running",
    total: items.length,
    completed,
    message,
    pillLabel: `${items.length} active`,
    jobIds: [...activeJobIds],
    items,
  };
  // Reconcile tracking after the display state exists. Refreshing the dock
  // before this assignment briefly paints an empty "Render" strip.
  for (const jobId of [...activeProvisionIds]) {
    if (!activeJobIds.has(jobId)) activeProvisionIds.delete(jobId);
  }
  for (const jobId of activeJobIds) activeProvisionIds.add(jobId);
  writeActiveJobsLs();
  if (!activeBatchIds.size && !activeUploadIds.size && !currentSparkJobId) {
    dockFocusKind = "provision";
  }
  showProgressDock();
  renderProvisionDockState(currentProvisionDockSt);
  refreshActiveJobsDock();
}
window.updateProvisionDock = updateProvisionDock;
window.focusProvisionActivity = () => {
  if (!currentProvisionDockSt) return;
  dockFocusKind = "provision";
  showProgressDock();
  refreshActiveJobsDock();
};

function updateTtSheetSparkResults(sparkState) {
  updateSparkFetchDock(sparkState);
  const items = (sparkState && sparkState.items) || [];
  ttSheetResultSparks = items.map((i) => ({
    postUrl: i.postUrl || "",
    caption: i.caption || "",
    filename: i.filename || "",
    sparkCode: i.sparkCode || "",
    status: i.status || "",
    error: i.error || "",
  }));
  const st =
    (ttSheetBoundUploadId && jobSnapshots[ttSheetBoundUploadId]) ||
    (currentUploadId && jobSnapshots[currentUploadId]) ||
    null;
  // Spark polling continues after the upload completes.  Closing the results
  // sheet records its upload id in ttSheetResultsDismissed, but this updater
  // used to repaint and reopen it on every poll response.  Keep the dock
  // current while respecting an explicit dismissal; opening the completed
  // upload from the dock still clears this marker first.
  const resultUploadId = (st && st.id) || ttSheetBoundUploadId;
  if (resultUploadId && ttSheetResultsDismissed.has(resultUploadId)) return;
  if (!st) {
    // Still paint spark-only rows if upload snapshot is gone.
    const fake = {
      id: ttSheetBoundUploadId || "",
      status: "done",
      uploaded: items.length,
      failed: 0,
      total: items.length,
      items: items.map((i, idx) => ({
        filename: i.caption ? String(i.caption).slice(0, 48) : `Video ${idx + 1}`,
        caption: i.caption || "",
        accountName: i.accountName || "",
        status: "done",
        postUrl: i.postUrl || "",
        error: "",
      })),
    };
    const fetched = items.filter((i) => i.sparkCode).length;
    const running = sparkState && (sparkState.status === "running" || sparkState.status === "queued");
    showTtUploadResultsSheet(fake, {
      sparkMessage: running
        ? `fetching sparks ${fetched}/${items.length}`
        : `sparks ${fetched}/${items.length}`,
      sparkStatusText: running
        ? sparkState.message || "Fetching Spark codes…"
        : fetched
          ? "Spark codes ready — copy all below."
          : sparkState && sparkState.status === "done"
            ? "Spark fetch finished."
            : "",
    });
    return;
  }
  const fetched = items.filter((i) => String(i.sparkCode || "").trim()).length;
  const running = sparkState && (sparkState.status === "running" || sparkState.status === "queued");
  showTtUploadResultsSheet(st, {
    sparkMessage: running
      ? `fetching sparks ${fetched}/${items.length || st.total || 0}`
      : fetched
        ? `${fetched} spark code${fetched === 1 ? "" : "s"}`
        : "",
    sparkStatusText: running
      ? sparkState.message || "Fetching Spark codes…"
      : fetched
        ? "Spark codes ready — use Copy all sparks."
        : sparkState && sparkState.status === "done"
          ? "No new spark codes (check rows for skipped/failed)."
          : "",
  });
}

async function copyTtSheetSparks() {
  const codes = [];
  for (const s of ttSheetResultSparks) {
    const c = String(s.sparkCode || "").trim();
    if (c && !codes.includes(c)) codes.push(c);
  }
  if (!codes.length) {
    const fromDom = [...document.querySelectorAll("#ttSheetResultsList .tt-result-spark")]
      .map((el) => (el.textContent || "").trim())
      .filter((t) => t && !/^Fetching/i.test(t) && !/failed/i.test(t));
    codes.push(...fromDom);
  }
  if (!codes.length) {
    if (typeof showToast === "function") showToast("No spark codes yet", { error: true });
    return;
  }
  try {
    await navigator.clipboard.writeText(codes.join("\n"));
    if (typeof showToast === "function") {
      showToast(`Copied ${codes.length} spark code${codes.length === 1 ? "" : "s"}`);
    }
  } catch {
    alert(codes.join("\n"));
  }
}

async function copyTtSheetLinks() {
  const st =
    (ttSheetBoundUploadId && jobSnapshots[ttSheetBoundUploadId]) ||
    (currentUploadId && jobSnapshots[currentUploadId]) ||
    null;
  const links = [];
  const push = (u) => {
    const s = String(u || "").trim();
    if (s && !links.includes(s)) links.push(s);
  };
  for (const i of (st && st.items) || []) push(i.postUrl);
  for (const s of ttSheetResultSparks) push(s.postUrl);
  if (!links.length) {
    [...document.querySelectorAll("#ttSheetResultsList .tt-result-link")].forEach((a) =>
      push(a.getAttribute("href") || a.textContent)
    );
  }
  if (!links.length) {
    if (typeof showToast === "function") showToast("No video links yet", { error: true });
    return;
  }
  try {
    await navigator.clipboard.writeText(links.join("\n"));
    if (typeof showToast === "function") {
      showToast(`Copied ${links.length} link${links.length === 1 ? "" : "s"}`);
    }
  } catch {
    alert(links.join("\n"));
  }
}

function renderTtSheetProgress(st) {
  if (!st) return;
  const uploaded = st.uploaded ?? 0;
  const skippedAlready = st.skippedAlready ?? 0;
  const failed = st.failed ?? 0;
  const total = st.total || 0;
  let summaryFull = `${uploaded} uploaded`;
  if (skippedAlready) summaryFull += ` · ${skippedAlready} already on TikTok`;
  if (failed) summaryFull += ` · ${failed} failed`;
  summaryFull += ` · ${total} total`;
  if (st.uploadEngine) summaryFull += ` · ${st.uploadEngine}`;
  if (st.message) summaryFull += ` — ${st.message}`;
  if (st.current) {
    summaryFull += ` — ${st.current.status} ${st.current.filename || ""}`;
    if (st.current.accountName) summaryFull += ` → ${st.current.accountName}`;
  }
  if (st.status === "running") {
    const eta = fmtEtaLeft(st.etaSeconds);
    if (eta) summaryFull += ` · ${eta}`;
  }
  if (st.status === "cancelled") summaryFull += " — cancelled";
  if (st.status === "done") summaryFull += " — done";
  const doneCount = uploaded + skippedAlready + failed;
  const frac = doneCount / Math.max(total, 1);
  const listHtml = (st.items || [])
    .map(
      (i) => {
        const label = uploadItemStatusLabel(i);
        return `
    <li>
      <span class="st ${label}">${label}</span>
      <span class="cap" title="${escapeHtml(i.description || i.caption || "")}">
        ${escapeHtml(i.filename || "")}${i.accountName ? " → " + escapeHtml(i.accountName) : ""}
      </span>
      ${i.error && label !== "uploaded" ? `<span class="err">${escapeHtml(i.error)}</span>` : ""}
    </li>`;
      }
    )
    .join("");
  if ($("ttSheetProgressSummary")) $("ttSheetProgressSummary").textContent = summaryFull;
  if ($("ttSheetProgressBar")) {
    $("ttSheetProgressBar").style.width = `${Math.min(frac * 100, 100)}%`;
  }
  if ($("ttSheetProgressList")) $("ttSheetProgressList").innerHTML = listHtml;
  updateTtUploadActionButtons(st);
}

function showTtUploadProgressSheet(st) {
  const modal = $("ttUploadSheet");
  if (!modal) return;
  ttSheetBoundUploadId = (st && st.id) || focusedUploadId || currentUploadId || null;
  if (st && (st.status === "done" || st.status === "cancelled")) {
    // This is an explicit user action (for example from the dock), so allow a
    // previously dismissed result sheet to be viewed again.
    ttSheetResultsDismissed.delete(st.id);
    showTtUploadResultsSheet(st);
    return;
  }
  setTtSheetMode("progress");
  if (st) renderTtSheetProgress(st);
  modal.classList.remove("hidden");
  if (typeof window.syncModalOpen === "function") window.syncModalOpen();
}

function initTtUploadSheet() {
  if (!$("ttUploadSheet")) return;
  const refreshPreview = () => {
    renderTtSheetVideos();
    const forceId = String((ttSheetContext && ttSheetContext.accountId) || "").trim();
    if ($("ttSheetStart")) {
      $("ttSheetStart").disabled =
        !ttSheetVideos.length || !ttReadyPool(forceId).length;
    }
  };
  ["ttSheetTemplate", "ttSheetHashtags"].forEach((id) => {
    if ($(id)) $(id).addEventListener("input", refreshPreview);
  });
  if ($("ttSheetDelay")) $("ttSheetDelay").addEventListener("input", refreshTtSheetEta);
  const hideSheet = () => {
    const modal = $("ttUploadSheet");
    const wasShowingResults = $("ttSheetResults") && !$("ttSheetResults").classList.contains("hidden");
    if (wasShowingResults && ttSheetBoundUploadId) {
      ttSheetResultsDismissed.add(ttSheetBoundUploadId);
    }
    if (modal) modal.classList.add("hidden");
    // Unbind so a later setup open is not overwritten by the running job poller.
    // Keep bound id only while progress is showing; results can stay unbound after Done.
    if ($("ttSheetResults") && !$("ttSheetResults").classList.contains("hidden")) {
      /* leaving results closed is fine */
    } else {
      ttSheetBoundUploadId = null;
    }
    if (typeof window.syncModalOpen === "function") window.syncModalOpen();
  };
  $("ttUploadSheetClose").addEventListener("click", () => {
    // Hide only — never cancel a running upload by closing the sheet.
    hideSheet();
  });
  $("ttUploadSheetBackdrop").addEventListener("click", hideSheet);
  if ($("ttSheetHide")) $("ttSheetHide").addEventListener("click", hideSheet);
  if ($("ttSheetDone")) $("ttSheetDone").addEventListener("click", closeTtUploadSheet);
  if ($("ttSheetCopySparks")) {
    $("ttSheetCopySparks").addEventListener("click", () => {
      copyTtSheetSparks().catch(() => {});
    });
  }
  if ($("ttSheetCopyLinks")) {
    $("ttSheetCopyLinks").addEventListener("click", () => {
      copyTtSheetLinks().catch(() => {});
    });
  }
  $("ttSheetCancel").addEventListener("click", () => {
    if (currentUploadId) {
      hideSheet();
      return;
    }
    closeTtUploadSheet();
  });
  if ($("ttSheetCancelUpload")) {
    $("ttSheetCancelUpload").addEventListener("click", () => {
      cancelCurrentTikTokUpload().catch(() => {});
    });
  }
  if ($("ttSheetContinue")) {
    $("ttSheetContinue").addEventListener("click", () => {
      continueTikTokUpload().catch(() => {});
    });
  }
  $("ttSheetStart").addEventListener("click", () => {
    startTtUploadFromSheet().catch(() => {});
  });
}

function renderTtAccounts() {
  const list = $("ttAccountList");
  if (!list) return;
  if (!ttAccounts.length) {
    list.innerHTML = `<li class="tt-empty"><span class="tt-meta"><span class="tt-name">No accounts yet</span>
      <span class="tt-status">Log into a TikTok account to get started.</span></span></li>`;
    return;
  }
  const eng = $("ttUploadEngine") ? $("ttUploadEngine").value : "studio";
  list.innerHTML = ttAccounts.map((a) => {
    const isChecked =
      ttSelectedIds.size === 0
        ? a.enabled && a.hasCookies
        : ttSelectedIds.has(a.id);
    const uname = a.tiktokUsername ? ` @${escapeHtml(a.tiktokUsername)}` : "";
    const health = a.cookieHealth || {};
    let statusLabel =
      a.verified === true ? `verified${uname}`
        : a.status === "ok" ? `ready${uname}`
        : a.status === "needs_reauth" ? "needs cookies"
          : a.status === "missing_cookies" ? "no cookies"
            : a.status;
    let badge = "";
    if (a.status === "needs_reauth" || health.fixCode === "AUTH_EXPIRED") {
      badge = `<span class="tt-badge tt-badge-err">Session expired — re-upload cookies</span>`;
    } else if (health.completeness === "sessionid_only") {
      badge = `<span class="tt-badge tt-badge-warn">HTTP: incomplete cookies</span>`;
    } else if (health.httpReady && a.status === "ok") {
      badge = `<span class="tt-badge tt-badge-ok">cookies OK</span>`;
    }
    const fixHint =
      health.fixHint && (eng === "http" || health.fixCode === "AUTH_EXPIRED" || health.fixCode === "COOKIE_MISSING")
        ? `<div class="tt-fix-hint">${escapeHtml(health.fixHint)}</div>`
        : health.completeness === "sessionid_only"
          ? `<div class="tt-fix-hint">${escapeHtml(health.fixHint || "")}</div>`
          : "";
    return `
    <li data-id="${a.id}">
      <input type="checkbox" class="tt-select" data-id="${a.id}" ${isChecked ? "checked" : ""}
        title="Include in round-robin pool" ${a.hasCookies ? "" : "disabled"} />
      <div class="tt-meta">
        <div class="tt-name">${escapeHtml(a.name)} ${badge}</div>
        <div class="tt-status ${a.status}${a.verified === true ? " verified" : ""}">${statusLabel}
          ${a.enabled ? "" : " · disabled"}
          · last used ${formatTtTime(a.lastUsedAt)}
        </div>
        ${fixHint}
        <label class="tt-suffix-field">Caption suffix
          <input class="tt-suffix-input" data-id="${a.id}" type="text" maxlength="80"
            value="${escapeHtml(a.captionSuffix || "")}" placeholder="{account_suffix}" />
        </label>
      </div>
      <div class="tt-actions">
        <button type="button" class="btn ghost tt-recookies" data-id="${a.id}">Re-upload cookies</button>
        <button type="button" class="btn ghost tt-toggle" data-id="${a.id}" data-enabled="${a.enabled ? "0" : "1"}">
          ${a.enabled ? "Disable" : "Enable"}
        </button>
        <button type="button" class="btn danger tt-delete" data-id="${a.id}">Remove</button>
      </div>
    </li>`;
  }).join("");
  renderTtHttpCookieBanner();
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function setTtLoginStatus(msg, isError) {
  const el = $("ttLoginStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError && !!msg);
  el.classList.toggle("is-ok", !isError && !!msg);
}

// ---------------------------------------------------------------------------
// Remote-browser TikTok login: modal streams server-side screenshots and
// forwards clicks / keystrokes / scroll to the headless browser.
// ---------------------------------------------------------------------------

function setRemoteMsg(msg, isError) {
  const el = $("remoteLoginMsg");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError && !!msg);
}

function closeRemoteLoginModal() {
  stopPoll("remote-frame");
  stopPoll("remote-login");
  $("remoteLoginModal").classList.add("hidden");
  const img = $("remoteScreen");
  if (img.dataset.url) {
    URL.revokeObjectURL(img.dataset.url);
    delete img.dataset.url;
  }
  img.removeAttribute("src");
  $("remoteScreenHint").style.display = "";
  remoteLoginId = null;
  writeActiveJobsLs();
}

function pollRemoteFrame() {
  pollUntil(async () => {
    if (!remoteLoginId) return false;
    try {
      const res = await fetch(
        `/api/tiktok/accounts/login/${remoteLoginId}/screen`,
        { headers: await authHeaders(), cache: "no-store" }
      );
      if (res.ok) {
        const blob = await res.blob();
        const img = $("remoteScreen");
        if (img.dataset.url) URL.revokeObjectURL(img.dataset.url);
        img.dataset.url = URL.createObjectURL(blob);
        img.src = img.dataset.url;
        $("remoteScreenHint").style.display = "none";
      }
    } catch {}
    return !!remoteLoginId;
  }, { interval: 400, key: "remote-frame" });
}

async function sendRemoteInput(cmd) {
  if (!remoteLoginId) return;
  try {
    await api(`/api/tiktok/accounts/login/${remoteLoginId}/input`, {
      method: "POST",
      body: JSON.stringify(cmd),
    });
  } catch {}
}

function initRemoteLoginModal() {
  const modal = $("remoteLoginModal");
  if (!modal) return;
  const img = $("remoteScreen");
  const wrap = $("remoteScreenWrap");

  img.addEventListener("click", (e) => {
    const rect = img.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    sendRemoteInput({
      kind: "click",
      x: (e.clientX - rect.left) / rect.width,
      y: (e.clientY - rect.top) / rect.height,
    });
    wrap.focus();
  });

  wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    sendRemoteInput({ kind: "scroll", dy: e.deltaY });
  }, { passive: false });

  wrap.addEventListener("keydown", (e) => {
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      sendRemoteInput({ kind: "text", text: e.key });
    } else if (["Enter", "Backspace", "Tab", "Escape", "Delete",
                "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight",
                "Home", "End"].includes(e.key)) {
      e.preventDefault();
      sendRemoteInput({ kind: "key", key: e.key });
    }
  });

  const typeInput = $("remoteTypeInput");
  typeInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const text = typeInput.value;
      if (text) sendRemoteInput({ kind: "text", text });
      sendRemoteInput({ kind: "key", key: "Enter" });
      typeInput.value = "";
    }
  });
  typeInput.addEventListener("blur", () => {
    const text = typeInput.value;
    if (text) {
      sendRemoteInput({ kind: "text", text });
      typeInput.value = "";
    }
  });

  const cancel = async () => {
    if (remoteLoginId) {
      try {
        await api(`/api/tiktok/accounts/login/${remoteLoginId}/cancel`, { method: "POST" });
      } catch {}
    }
    closeRemoteLoginModal();
    setTtLoginStatus("Sign-in cancelled");
  };
  $("remoteLoginCancel").addEventListener("click", cancel);
  $("remoteLoginClose").addEventListener("click", cancel);
  $("remoteLoginBackdrop").addEventListener("click", cancel);
}

function pollLogin(loginId) {
  pollUntil(async () => {
    if (remoteLoginId !== loginId) return false;
    try {
      const st = await api(`/api/tiktok/accounts/login/${loginId}`);
      if (st.status === "waiting" || st.status === "starting") {
        setRemoteMsg(st.message || "Waiting for sign-in…");
        return true;
      }
      if (st.status === "done") {
        setTtLoginStatus(st.message || `Signed in: ${st.accountName || "account"}`);
        closeRemoteLoginModal();
        await loadTtAccounts();
        return false;
      }
      setRemoteMsg(st.error || st.message || "Login failed", true);
      setTtLoginStatus(st.error || st.message || "Login failed", true);
      return false;
    } catch (err) {
      setRemoteMsg(err.message || "Login status error", true);
      return { continue: true, interval: 2500 };
    }
  }, { interval: 1500, key: "remote-login" });
}

async function startCookieImport(accountId) {
  const name = ($("ttAccountName").value || "").trim() || "TikTok account";
  setTtLoginStatus("Starting remote browser…");
  const body = { name };
  if (accountId) body.accountId = accountId;
  const res = await api("/api/tiktok/accounts/login", {
    method: "POST",
    body: JSON.stringify(body),
  });
  remoteLoginId = res.loginId;
  writeActiveJobsLs();
  $("remoteLoginModal").classList.remove("hidden");
  $("remoteScreenHint").style.display = "";
  setRemoteMsg(res.message || "Starting…");
  $("remoteScreenWrap").focus();
  pollRemoteFrame();
  pollLogin(res.loginId);
}

const TT_LOGIN_TAB_URL = "https://www.tiktok.com/login/qrcode?lang=en";
let cookieHelperAccountId = null;

function formatVerifyStatus(res) {
  if (!res) return "Cookies saved";
  if (res.verified === true) {
    return res.verifyMessage || (res.tiktokUsername
      ? `Session works (@${res.tiktokUsername})`
      : "Session works");
  }
  if (res.verified === false) {
    return res.verifyMessage || "Saved, but TikTok rejected this sessionid";
  }
  return res.verifyMessage || "Cookies saved (could not reach TikTok to verify)";
}

function setCookieHelperMsg(msg, isError) {
  const el = $("cookieHelperMsg");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError && !!msg);
  el.classList.toggle("is-ok", !isError && !!msg);
}

function openTikTokLoginTab() {
  // No window-features string — that makes Chrome open a popup instead of a tab.
  const w = window.open(TT_LOGIN_TAB_URL, "_blank");
  if (!w) {
    setCookieHelperMsg(
      "Pop-up blocked. Allow pop-ups for this site, or click Reopen TikTok tab.",
      true
    );
    return false;
  }
  try {
    w.opener = null;
  } catch (e) {}
  return true;
}

function openCookieHelper(accountId) {
  cookieHelperAccountId = accountId || null;
  const modal = $("cookieHelperModal");
  if (!modal) return;
  const nameInput = $("cookieHelperName");
  const title = $("cookieHelperTitle");
  if (accountId) {
    const acc = ttAccounts.find((a) => a.id === accountId);
    nameInput.value = (acc && acc.name) || ($("ttAccountName").value || "").trim();
    nameInput.disabled = true;
    title.textContent = "Re-upload cookies";
  } else {
    nameInput.value = ($("ttAccountName").value || "").trim();
    nameInput.disabled = false;
    title.textContent = "Sign in with this Chrome";
  }
  $("cookieHelperText").value = "";
  setCookieHelperMsg("");
  modal.classList.remove("hidden");
  openTikTokLoginTab();
  $("cookieHelperText").focus();
}

function closeCookieHelper() {
  const modal = $("cookieHelperModal");
  if (modal) modal.classList.add("hidden");
  cookieHelperAccountId = null;
  setCookieHelperMsg("");
}

async function submitCookieHelper() {
  const text = ($("cookieHelperText").value || "").trim();
  if (!text) {
    setCookieHelperMsg("Paste the sessionid value (or cookies.txt) first", true);
    return;
  }
  const name =
    ($("cookieHelperName").value || $("ttAccountName").value || "").trim() ||
    "TikTok account";
  setCookieHelperMsg("Saving and verifying with TikTok…");
  try {
    const body = { name, cookiesText: text };
    if (cookieHelperAccountId) body.accountId = cookieHelperAccountId;
    const res = await api("/api/tiktok/accounts/import", {
      method: "POST",
      body: JSON.stringify(body),
    });
    $("cookieHelperText").value = "";
    $("ttAccountName").value = "";
    const ok = res.verified !== false;
    closeCookieHelper();
    setTtLoginStatus(formatVerifyStatus(res), !ok);
    await loadTtAccounts();
  } catch (err) {
    setCookieHelperMsg(err.message || "Could not save cookies", true);
  }
}

function initCookieHelperModal() {
  const modal = $("cookieHelperModal");
  if (!modal) return;
  $("cookieHelperClose").addEventListener("click", closeCookieHelper);
  $("cookieHelperBackdrop").addEventListener("click", closeCookieHelper);
  $("cookieHelperReopen").addEventListener("click", () => {
    setCookieHelperMsg("");
    openTikTokLoginTab();
  });
  $("cookieHelperSave").addEventListener("click", () => {
    submitCookieHelper().catch(() => {});
  });
  $("cookieHelperRemote").addEventListener("click", async () => {
    const accountId = cookieHelperAccountId;
    closeCookieHelper();
    try {
      await startCookieImport(accountId);
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });
  $("cookieHelperText").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
      e.preventDefault();
      submitCookieHelper().catch(() => {});
    }
  });
}

async function loadCookieStatus() {
  const statusEl = $("ttCookieStatus");
  if (!statusEl) return;

  try {
    const res = await api("/api/cookies/status");

    if (!res.hasLocalVault) {
      statusEl.textContent = "No cookies stored yet. Click 'Refresh Cookies' to get started.";
      statusEl.classList.remove("success");
      statusEl.classList.add("error");
      return;
    }

    const age = res.ageHours ? `${Math.round(res.ageHours)}h ago` : "unknown age";
    const freshness = res.isFresh ? "✓ Fresh" : "⚠ Stale";
    const sessionStatus = res.hasSessionId ? "✓ Valid" : "✗ No session";

    statusEl.textContent = `Cookies: ${res.cookieCount} stored (${age}) | ${freshness} | ${sessionStatus}`;
    statusEl.classList.remove("error");
    statusEl.classList.add(res.isFresh && res.hasSessionId ? "success" : "error");
  } catch (err) {
    statusEl.textContent = `Failed to load cookie status: ${err.message}`;
    statusEl.classList.add("error");
  }
}

async function checkCookieConfig() {
  const statusEl = $("ttCookieStatus");
  if (!statusEl) return;

  try {
    const res = await api("/api/cookies/config-check");

    if (!res.allOk) {
      const missing = res.recommendations || [];
      statusEl.innerHTML = `⚠ Setup required:<br>${missing.map(m => `• ${m}`).join('<br>')}`;
      statusEl.classList.add("error");
    }
  } catch (err) {
    console.error("Config check failed:", err);
  }
}

async function refreshAdsPowerCookies() {
  const btn = $("ttRefreshCookies");
  const statusEl = $("ttCookieStatus");

  if (!btn || !statusEl) return;

  const originalText = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Checking setup...";
  statusEl.textContent = "Checking configuration...";
  statusEl.classList.remove("success", "error");

  // First check if everything is configured
  try {
    const configCheck = await api("/api/cookies/config-check");

    if (!configCheck.allOk) {
      const missing = configCheck.recommendations || [];
      statusEl.innerHTML = `⚠ Setup incomplete:<br>${missing.map(m => `• ${m}`).join('<br>')}<br><br>See SETUP_ENV_VARS.md for instructions.`;
      statusEl.classList.add("error");
      btn.disabled = false;
      btn.textContent = originalText;
      return;
    }
  } catch (err) {
    statusEl.textContent = `✗ Config check failed: ${err.message}`;
    statusEl.classList.add("error");
    btn.disabled = false;
    btn.textContent = originalText;
    return;
  }

  btn.textContent = "Opening AdsPower browser...";
  statusEl.innerHTML = "Creating AdsPower profile with random datacenter proxy...<br><strong>Please wait...</strong>";

  try {
    // First show that we're opening the browser
    statusEl.innerHTML = "⏳ Opening AdsPower browser...<br><strong style='color: #f59e0b;'>A browser window will open. Sign into TikTok Ads there, then this will continue automatically.</strong>";

    const res = await api("/api/cookies/refresh", {
      method: "POST",
      body: JSON.stringify({
        hostUid: null,
        useExistingProfile: false
      }),
      timeoutMs: 1860000  // 31 minutes timeout for user to sign in
    });

    if (res.success) {
      statusEl.innerHTML = `✓ Success! ${res.cookieCount} cookies extracted from profile ${res.profileId}.<br>You can close the AdsPower window now.`;
      statusEl.classList.add("success");

      // Reload cookie status
      setTimeout(() => loadCookieStatus(), 2000);
    } else {
      const errorMsg = res.error || res.message;
      statusEl.innerHTML = `✗ Failed: ${errorMsg}<br><br>See SETUP_ENV_VARS.md for help.`;
      statusEl.classList.add("error");
    }
  } catch (err) {
    statusEl.innerHTML = `✗ Error: ${err.message}<br><br>Check console for details. See SETUP_ENV_VARS.md for setup help.`;
    statusEl.classList.add("error");
    console.error("Cookie refresh error:", err);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function initTikTok() {
  const list = $("ttAccountList");
  if (!list) return;

  initTtUploadSheet();

  $("ttRefreshCookies").addEventListener("click", async () => {
    await refreshAdsPowerCookies();
  });

  // Load initial cookie status and check config
  loadCookieStatus();
  checkCookieConfig();

  $("ttImportCookies").addEventListener("click", async () => {
    try {
      await startCookieImport(null);
      $("ttAccountName").value = "";
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });

  list.addEventListener("change", async (e) => {
    const t = e.target;
    if (t.classList.contains("tt-suffix-input")) {
      const id = t.dataset.id;
      if (!id) return;
      try {
        const res = await api(`/api/tiktok/accounts/${id}`, {
          method: "PUT",
          body: JSON.stringify({ captionSuffix: t.value }),
        });
        const acc = ttAccounts.find((a) => a.id === id);
        if (acc) acc.captionSuffix = res.captionSuffix || t.value;
      } catch (err) {
        setTtLoginStatus(err.message, true);
      }
      return;
    }
    if (!t.classList.contains("tt-select")) return;
    const boxes = [...list.querySelectorAll(".tt-select:not(:disabled)")];
    ttSelectedIds = new Set(
      boxes.filter((b) => b.checked).map((b) => b.dataset.id)
    );
    try {
      await saveTtSettings();
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });

  list.addEventListener("click", async (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    const id = btn.dataset.id;
    if (!id) return;
    try {
      if (btn.classList.contains("tt-delete")) {
        if (!confirm("Remove this TikTok account and its saved cookies?")) return;
        await api(`/api/tiktok/accounts/${id}`, { method: "DELETE" });
        ttSelectedIds.delete(id);
        await loadTtAccounts();
        await saveTtSettings();
      } else if (btn.classList.contains("tt-toggle")) {
        const enabled = btn.dataset.enabled === "1";
        await api(`/api/tiktok/accounts/${id}/enabled`, {
          method: "PUT",
          body: JSON.stringify({ enabled }),
        });
        await loadTtAccounts();
      } else if (btn.classList.contains("tt-recookies")) {
        const acc = ttAccounts.find((a) => a.id === id);
        if (acc) $("ttAccountName").value = acc.name || "";
        openCookieHelper(id);
      }
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });

  $("ttSaveSettings").addEventListener("click", async () => {
    try {
      await saveTtSettings();
      setTtLoginStatus("Upload settings saved");
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });
  wireMcpKeys();
  $("ttCaptionTemplate").addEventListener("input", saveTtSettingsDebounced);
  if ($("ttHashtags")) $("ttHashtags").addEventListener("input", saveTtSettingsDebounced);
  $("ttDelay").addEventListener("change", saveTtSettingsDebounced);
  if ($("ttMinGap")) $("ttMinGap").addEventListener("change", saveTtSettingsDebounced);
  if ($("ttUploadEngine")) {
    $("ttUploadEngine").addEventListener("change", () => {
      applyTtEngineUi();
      refreshTtSheetEta();
      saveTtSettingsDebounced();
    });
  }
  $("ttAutoUpload").addEventListener("change", saveTtSettingsDebounced);
  $("ttContinueOnError").addEventListener("change", saveTtSettingsDebounced);
  $("ttCopyrightChecks").addEventListener("change", saveTtSettingsDebounced);
  $("ttContentChecks").addEventListener("change", saveTtSettingsDebounced);
  wireTtIncidentModal();
  wireTtTestUpload();
  applyTtEngineUi();

  $("uploadToTikTok").addEventListener("click", async () => {
    if (!currentBatchId) {
      alert("No batch to upload. Finish rendering first.");
      return;
    }
    try {
      await openTtUploadSheet({ batchId: currentBatchId });
    } catch (err) {
      alert("Could not open TikTok upload: " + err.message);
    }
  });

  $("cancelTikTokUpload").addEventListener("click", cancelCurrentTikTokUpload);
  $("cancelTikTokUploadStandalone").addEventListener("click", cancelCurrentTikTokUpload);
  if ($("continueTikTokUpload")) {
    $("continueTikTokUpload").addEventListener("click", () => continueTikTokUpload().catch(() => {}));
  }
  if ($("continueTikTokUploadStandalone")) {
    $("continueTikTokUploadStandalone").addEventListener("click", () => continueTikTokUpload().catch(() => {}));
  }

  // Cookie import fallback (paste sessionid / cookies.txt).
  $("ttImportBtn").addEventListener("click", async () => {
    const text = ($("ttImportText").value || "").trim();
    if (!text) { setTtLoginStatus("Paste a sessionid or cookies first", true); return; }
    try {
      const name = ($("ttAccountName").value || "").trim() || "TikTok account";
      setTtLoginStatus("Saving and verifying with TikTok…");
      const res = await api("/api/tiktok/accounts/import", {
        method: "POST",
        body: JSON.stringify({ name, cookiesText: text }),
      });
      $("ttImportText").value = "";
      $("ttAccountName").value = "";
      setTtLoginStatus(formatVerifyStatus(res), res.verified === false);
      await loadTtAccounts();
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });
  $("ttImportFileBtn").addEventListener("click", () => $("ttImportFile").click());
  $("ttImportFile").addEventListener("change", async () => {
    const file = $("ttImportFile").files[0];
    $("ttImportFile").value = "";
    if (!file) return;
    try {
      const fd = new FormData();
      fd.append("file", file);
      fd.append("name", ($("ttAccountName").value || "").trim() || "TikTok account");
      setTtLoginStatus("Saving and verifying with TikTok…");
      const res = await api("/api/tiktok/accounts/import-file", { method: "POST", body: fd });
      setTtLoginStatus(formatVerifyStatus(res), res.verified === false);
      await loadTtAccounts();
    } catch (err) {
      setTtLoginStatus(err.message, true);
    }
  });

  initCookieHelperModal();
  initRemoteLoginModal();
}

// ---------------------------------------------------------------------------
// Facebook Ads connect + upload
// ---------------------------------------------------------------------------

function setFbStatus(text, isErr) {
  const el = $("fbStatus");
  if (!el) return;
  el.textContent = text || "";
  el.classList.toggle("err", !!isErr);
}

function fillSelect(sel, items, valueKey, labelKey, selected, emptyLabel) {
  if (!sel) return;
  const opts = [];
  if (emptyLabel) opts.push(`<option value="">${emptyLabel}</option>`);
  for (const it of items || []) {
    const v = String(it[valueKey] || "");
    const lab = escapeHtml(it[labelKey] || v);
    opts.push(`<option value="${escapeHtml(v)}">${lab}</option>`);
  }
  sel.innerHTML = opts.join("");
  if (selected) sel.value = selected;
}

async function loadFacebookStatus() {
  let st;
  try {
    st = await api("/api/facebook/status");
  } catch (err) {
    const msg = String(err.message || err);
    const stale = /not found/i.test(msg);
    setFbStatus(
      stale
        ? "Facebook API is not loaded. Restart the Python server (this page updated, but the backend is still an old process)."
        : msg,
      true
    );
    if ($("fbNotConfigured")) $("fbNotConfigured").classList.remove("hidden");
    if ($("fbConnectBlock")) $("fbConnectBlock").classList.remove("hidden");
    if ($("fbDefaults")) $("fbDefaults").classList.add("hidden");
    if ($("fbConnectBtn")) $("fbConnectBtn").disabled = true;
    if ($("fbCopyUrlBtn")) $("fbCopyUrlBtn").disabled = true;
    return { configured: false, connected: false };
  }
  fbStatus = st;
  const configured = !!(st.configured || (config && config.facebookConfigured));
  if ($("fbNotConfigured")) $("fbNotConfigured").classList.toggle("hidden", configured);
  if ($("fbConnectBlock")) $("fbConnectBlock").classList.remove("hidden");
  if ($("fbDisconnectBtn")) $("fbDisconnectBtn").classList.toggle("hidden", !st.connected);
  if ($("fbDefaults")) $("fbDefaults").classList.toggle("hidden", !st.connected);
  if ($("fbConnectBtn")) $("fbConnectBtn").disabled = !configured;
  if ($("fbCopyUrlBtn")) $("fbCopyUrlBtn").disabled = !configured;
  if (!st.connected) {
    setFbStatus(
      st.needsReauth
        ? "Facebook login expired — reconnect."
        : configured
          ? "Not connected."
          : "Add META_APP_ID and META_APP_SECRET to .env, then restart.",
      st.needsReauth || !configured
    );
    return st;
  }
  setFbStatus(`Connected as ${st.userName || "Facebook"}`);
  const s = st.settings || {};
  fillSelect($("fbPage"), st.pages || [], "id", "name", s.pageId, "— pick a Page —");
  if ($("fbEvent")) $("fbEvent").value = s.customEventType || "PURCHASE";
  if ($("fbBudget")) $("fbBudget").value = s.dailyBudget || 20;
  if ($("fbBidStrategy")) $("fbBidStrategy").value = s.bidStrategy || "LOWEST_COST_WITHOUT_CAP";
  if ($("fbBidAmount")) $("fbBidAmount").value = s.bidAmount || "";
  try {
    const acc = await api("/api/facebook/adaccounts");
    fillSelect($("fbAdAccount"), acc.items || [], "id", "name", s.adAccountId, "— pick an ad account —");
  } catch (err) {
    setFbStatus(err.message, true);
  }
  await loadFbPixelsInto($("fbPixel"), s.adAccountId, s.pixelId);
  return st;
}

async function loadFbPixelsInto(sel, adAccountId, selected) {
  try {
    const qs = adAccountId ? `?adAccountId=${encodeURIComponent(adAccountId)}` : "";
    const res = await api("/api/facebook/pixels" + qs);
    fillSelect(sel, res.items || [], "id", "name", selected, "— none (traffic) —");
  } catch {
    fillSelect(sel, [], "id", "name", "", "— none (traffic) —");
  }
}

async function fbOAuthUrl(copy) {
  return api("/api/facebook/oauth/url" + (copy ? "?copy=1" : ""));
}

function initFacebookSettings() {
  if (!$("fbConnectBtn")) return;
  $("fbConnectBtn").addEventListener("click", async () => {
    try {
      const res = await fbOAuthUrl(false);
      window.open(res.url, "_blank", "noopener");
      setFbStatus("Finish login in the Facebook tab, then return here (or paste the redirect URL).");
    } catch (err) {
      setFbStatus(err.message, true);
    }
  });
  $("fbCopyUrlBtn").addEventListener("click", async () => {
    try {
      const res = await fbOAuthUrl(true);
      await navigator.clipboard.writeText(res.url);
      setFbStatus(
        "Login link copied. Open it in the other browser, click Continue, then copy that browser’s address bar (it must contain code=) and paste it below — even if the page fails to load."
      );
    } catch (err) {
      setFbStatus(err.message, true);
    }
  });
  $("fbPasteBtn").addEventListener("click", async () => {
    const raw = ($("fbPasteCode").value || "").trim();
    if (!raw) return;
    try {
      await api("/api/facebook/oauth/code", { method: "POST", body: JSON.stringify({ url: raw }) });
      $("fbPasteCode").value = "";
      await loadFacebookStatus();
      setFbStatus("Facebook connected.");
    } catch (err) {
      setFbStatus(err.message, true);
    }
  });
  $("fbDisconnectBtn").addEventListener("click", async () => {
    if (!confirm("Disconnect Facebook Ads from this account?")) return;
    try {
      await api("/api/facebook", { method: "DELETE" });
      await loadFacebookStatus();
    } catch (err) {
      setFbStatus(err.message, true);
    }
  });
  $("fbAdAccount").addEventListener("change", async () => {
    await loadFbPixelsInto($("fbPixel"), $("fbAdAccount").value, "");
  });
  $("fbSaveDefaults").addEventListener("click", async () => {
    try {
      await api("/api/facebook/settings", {
        method: "PUT",
        body: JSON.stringify({
          adAccountId: $("fbAdAccount").value,
          pageId: $("fbPage").value,
          pixelId: $("fbPixel").value,
          customEventType: $("fbEvent").value,
          dailyBudget: Number($("fbBudget").value) || 20,
          bidStrategy: $("fbBidStrategy").value,
          bidAmount: $("fbBidAmount").value ? Number($("fbBidAmount").value) : null,
        }),
      });
      setFbStatus("Facebook defaults saved.");
      await loadFacebookStatus();
    } catch (err) {
      setFbStatus(err.message, true);
    }
  });
}

function fbAdsModeFields() {
  const mode = $("fbAdsMode") ? $("fbAdsMode").value : "new_campaign";
  const existingCamp = mode !== "new_campaign";
  const existingSet = mode === "existing_adset";
  const newSet = mode !== "existing_adset";
  if ($("fbAdsCampaignWrap")) $("fbAdsCampaignWrap").classList.toggle("hidden", !existingCamp);
  if ($("fbAdsAdsetWrap")) $("fbAdsAdsetWrap").classList.toggle("hidden", !existingSet);
  if ($("fbAdsCampaignNameWrap")) $("fbAdsCampaignNameWrap").classList.toggle("hidden", existingCamp);
  if ($("fbAdsAdsetNameWrap")) $("fbAdsAdsetNameWrap").classList.toggle("hidden", !newSet);
  if ($("fbAdsBudgetRow")) $("fbAdsBudgetRow").classList.toggle("hidden", existingSet);
  if ($("fbAdsPixelWrap")) $("fbAdsPixelWrap").classList.toggle("hidden", existingSet);
  if ($("fbAdsEventWrap")) $("fbAdsEventWrap").classList.toggle("hidden", existingSet);
}

async function openFbAdsModal(paths) {
  const modal = $("fbAdsModal");
  if (!modal) return;
  modal.dataset.paths = JSON.stringify(paths || []);
  $("fbAdsMsg").textContent = `${paths.length} video${paths.length === 1 ? "" : "s"} selected.`;
  modal.classList.remove("hidden");
  try {
    const st = await loadFacebookStatus();
    if (!st.connected) {
      $("fbAdsHint").textContent = "Connect Facebook in Settings first.";
      return;
    }
    $("fbAdsHint").textContent = "Ads are created paused. Review them in Ads Manager before turning spend on.";
    const s = st.settings || {};
    if ($("fbAdsBudget")) $("fbAdsBudget").value = s.dailyBudget || 20;
    if ($("fbAdsBidStrategy")) $("fbAdsBidStrategy").value = s.bidStrategy || "LOWEST_COST_WITHOUT_CAP";
    if ($("fbAdsBidAmount")) $("fbAdsBidAmount").value = s.bidAmount || "";
    if ($("fbAdsEvent")) $("fbAdsEvent").value = s.customEventType || "PURCHASE";
    await loadFbPixelsInto($("fbAdsPixel"), s.adAccountId, s.pixelId);
    const camps = await api("/api/facebook/campaigns");
    fbCampaigns = camps.items || [];
    fillSelect($("fbAdsCampaign"), fbCampaigns, "id", "name", "", "— pick a campaign —");
    fbAdsModeFields();
    if ($("fbAdsCampaign").value) await loadFbAdsetsForModal();
  } catch (err) {
    $("fbAdsMsg").textContent = err.message;
  }
}

async function loadFbAdsetsForModal() {
  const cid = $("fbAdsCampaign").value;
  if (!cid) {
    fillSelect($("fbAdsAdset"), [], "id", "name", "", "— pick an ad set —");
    return;
  }
  try {
    const res = await api(`/api/facebook/adsets?campaignId=${encodeURIComponent(cid)}`);
    fillSelect($("fbAdsAdset"), res.items || [], "id", "name", "", "— pick an ad set —");
  } catch (err) {
    $("fbAdsMsg").textContent = err.message;
  }
}

function closeFbAdsModal() {
  if ($("fbAdsModal")) $("fbAdsModal").classList.add("hidden");
}

function initFacebookAdsModal() {
  if (!$("fbAdsModal")) return;
  $("fbAdsClose").addEventListener("click", closeFbAdsModal);
  $("fbAdsCancel").addEventListener("click", closeFbAdsModal);
  $("fbAdsBackdrop").addEventListener("click", closeFbAdsModal);
  $("fbAdsMode").addEventListener("change", fbAdsModeFields);
  $("fbAdsCampaign").addEventListener("change", () => loadFbAdsetsForModal());
  $("fbAdsSubmit").addEventListener("click", submitFbAds);
  if ($("cancelFacebookAds")) {
    $("cancelFacebookAds").addEventListener("click", async () => {
      if (!currentFbJobId) return;
      try {
        await api(`/api/facebook/ads/${currentFbJobId}/cancel`, { method: "POST" });
      } catch {}
    });
  }
}

async function submitFbAds() {
  const paths = JSON.parse($("fbAdsModal").dataset.paths || "[]");
  if (!paths.length) return;
  const mode = $("fbAdsMode").value;
  const landingUrl = ($("fbAdsLanding").value || "").trim();
  if (!landingUrl) {
    $("fbAdsMsg").textContent = "Landing URL is required.";
    return;
  }
  const body = {
    paths,
    mode,
    landingUrl,
    campaignName: $("fbAdsCampaignName").value,
    adsetName: $("fbAdsAdsetName").value,
    dailyBudget: Number($("fbAdsBudget").value) || 20,
    bidStrategy: $("fbAdsBidStrategy").value,
    bidAmount: $("fbAdsBidAmount").value ? Number($("fbAdsBidAmount").value) : null,
    pixelId: $("fbAdsPixel").value || "",
    customEventType: $("fbAdsEvent").value,
  };
  if (mode !== "new_campaign") body.campaignId = $("fbAdsCampaign").value;
  if (mode === "existing_adset") body.adsetId = $("fbAdsAdset").value;
  if (mode === "new_adset" && !body.campaignId) {
    $("fbAdsMsg").textContent = "Pick a campaign.";
    return;
  }
  if (mode === "existing_adset" && !body.adsetId) {
    $("fbAdsMsg").textContent = "Pick an ad set.";
    return;
  }
  $("fbAdsSubmit").disabled = true;
  try {
    const res = await api("/api/facebook/ads", { method: "POST", body: JSON.stringify(body) });
    rememberFbJob(res.jobId || res.id);
    closeFbAdsModal();
    startWatchingFbAds();
  } catch (err) {
    $("fbAdsMsg").textContent = err.message;
  } finally {
    $("fbAdsSubmit").disabled = false;
  }
}

function startWatchingFbAds(jobId) {
  const id = jobId || currentFbJobId;
  if (!id) return;
  rememberFbJob(id);
  dockFocusKind = "facebook";
  dockManualCollapseDuringJob = false;
  dockAutoExpandedFor = null;
  showProgressDock();
  if ($("renderProgressBlock")) $("renderProgressBlock").classList.add("hidden");
  if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.add("hidden");
  if ($("facebookAdsPanel")) $("facebookAdsPanel").classList.remove("hidden");
  if ($("cancelTikTokUploadStandalone")) $("cancelTikTokUploadStandalone").classList.add("hidden");
  if ($("dockDismiss")) $("dockDismiss").classList.add("hidden");
  syncDockCancelButtons();
  pollUntil(async () => {
    if (!currentFbJobId || currentFbJobId !== id) return false;
    try {
      const st = await api(`/api/facebook/ads/${id}`);
      jobSnapshots[id] = st;
      renderFbAdsState(st);
      if (st.status === "running" || st.status === "queued") return true;
      maybeAutoExpandDock(id, st.status);
      if ($("dockDismiss")) $("dockDismiss").classList.remove("hidden");
      forgetFbJob(id);
      return false;
    } catch (err) {
      const msg = String(err && err.message ? err.message : err);
      if (/not found|404/i.test(msg)) {
        forgetFbJob(id);
        return false;
      }
      return { continue: true, interval: 2500 };
    }
  }, { interval: 1500, key: "fb-ads:" + id });
}

window.startWatchingFbAds = startWatchingFbAds;

function renderFbAdsState(st) {
  if (!st || !st.id) return;
  let summary = `${st.completed || 0} / ${st.total || 0} ads`;
  if (st.message) summary += ` — ${st.message}`;
  if (st.campaignId) summary += ` · campaign ${st.campaignId}`;
  if (st.adsetId) summary += ` · ad set ${st.adsetId}`;
  const frac = (st.completed || 0) / Math.max(st.total || 1, 1);
  if ($("facebookAdsSummary")) $("facebookAdsSummary").textContent = summary;
  if ($("facebookAdsBar")) $("facebookAdsBar").style.width = `${Math.min(frac * 100, 100)}%`;
  if ($("facebookAdsList")) {
    $("facebookAdsList").innerHTML = (st.items || []).map((i) => `
      <li>
        <span class="st ${i.status}">${i.status}</span>
        <span class="cap">${escapeHtml(i.filename || "")}${i.adId ? " · ad " + escapeHtml(i.adId) : ""}</span>
        ${i.error ? `<span class="err-msg">${escapeHtml(String(i.error).slice(0, 400))}</span>` : ""}
      </li>
    `).join("");
  }
  renderDockStrip(st);
}

async function pollFbAds() {
  if (currentFbJobId) startWatchingFbAds();
}

function initFacebook() {
  initFacebookSettings();
  initFacebookAdsModal();
}

// ---------------------------------------------------------------------------
// Outputs page
// ---------------------------------------------------------------------------

let outputItems = [];
let outputFilter = "all";

function selectedOutputPaths() {
  return [...document.querySelectorAll(".output-select:checked")].map((b) => b.dataset.path);
}

function updateOutputButtons() {
  const n = selectedOutputPaths().length;
  $("outputsUploadSel").disabled = n === 0;
  $("outputsDeleteSel").disabled = n === 0;
  $("outputsUploadSel").textContent = n
    ? `Upload ${n} to TikTok` : "Upload selected to TikTok";
  if ($("outputsUploadFb")) {
    $("outputsUploadFb").disabled = n === 0;
    $("outputsUploadFb").textContent = n
      ? `Upload ${n} to Facebook` : "Upload selected to Facebook";
  }
}

function fmtBytes(n) {
  if (!n) return "";
  if (n > 1 << 30) return (n / (1 << 30)).toFixed(1) + " GB";
  if (n > 1 << 20) return (n / (1 << 20)).toFixed(1) + " MB";
  return Math.round(n / 1024) + " KB";
}

function outputPosted(it) {
  return !!it.onTiktok || String(it.tiktokStatus || "") === "done";
}

function outputFailed(it) {
  return String(it.tiktokStatus || "") === "failed";
}

function outputMatchesFilter(it) {
  if (outputFilter === "posted") return outputPosted(it);
  if (outputFilter === "failed") return outputFailed(it);
  if (outputFilter === "unposted") return !outputPosted(it) && !outputFailed(it);
  return true;
}

function ttStatusBadge(it) {
  if (outputFailed(it)) {
    const err = it.tiktokError || "Upload failed";
    return `<span class="tt-badge tt-badge-failed" title="${escapeHtml(err)}">Failed</span>`;
  }
  if (outputPosted(it)) {
    const who = it.tiktokAccountName ? ` @${it.tiktokAccountName}` : "";
    return `<span class="tt-badge tt-badge-posted">Posted${escapeHtml(who)}</span>`;
  }
  return `<span class="tt-badge">Not posted</span>`;
}

function renderOutputs() {
  const list = $("outputsList");
  const meta = $("outputsMeta");
  if (!list) return;
  const visible = outputItems.filter(outputMatchesFilter);
  if (meta) {
    meta.textContent = outputItems.length
      ? `${visible.length} of ${outputItems.length} file${outputItems.length === 1 ? "" : "s"}`
      : "No rendered outputs yet — start a batch above.";
  }
  list.innerHTML = visible.map((it) => {
    const href = it.downloadUrl || `/api/output/${encodeURIComponent(it.path).replace(/%2F/g, "/")}`;
    const when = it.mtime ? new Date(it.mtime * 1000).toLocaleString() : "";
    const playUrl = `/api/output/${encodeURIComponent(it.path).replace(/%2F/g, "/")}`;
    const hook = (it.hook || "").trim();
    const postUrl = it.tiktokPostUrl || "";
    const spark = (it.tiktokSparkCode || "").trim();
    const sparkOk = spark && spark.toLowerCase() !== "fail" && spark.startsWith("#");
    return `
      <li class="output-item output-item-player">
        <label class="check output-pick">
          <input type="checkbox" class="output-select" data-path="${escapeHtml(it.path)}" />
        </label>
        <video class="output-thumb" data-auth-src="${escapeHtml(playUrl)}" controls preload="metadata"></video>
        <div class="output-meta">
          <div class="output-name-row">
            <div class="output-name" title="${escapeHtml(it.path)}">${escapeHtml(it.filename)}</div>
            ${ttStatusBadge(it)}
          </div>
          ${hook ? `<div class="output-hook" title="${escapeHtml(hook)}">${escapeHtml(hook)}</div>` : ""}
          <div class="output-sub">${escapeHtml(it.folder || "")}${it.folder ? " · " : ""}${fmtBytes(it.sizeBytes)}${when ? " · " + when : ""}</div>
          ${postUrl ? `<a class="output-post-link" href="${escapeHtml(postUrl)}" target="_blank" rel="noopener">View on TikTok</a>` : ""}
          ${sparkOk ? `<div class="output-spark"><span class="spark-code" title="${escapeHtml(spark)}">${escapeHtml(spark)}</span><button type="button" class="btn ghost spark-copy-output" data-spark="${escapeHtml(spark)}">Copy spark</button></div>` : ""}
        </div>
        <a class="btn ghost output-download" href="${href}" download="${escapeHtml(it.filename)}">Download</a>
      </li>`;
  }).join("");
  if (typeof hydrateAuthMedia === "function") hydrateAuthMedia(list);
  list.querySelectorAll("a.output-download").forEach((a) => {
    a.addEventListener("click", (ev) => {
      if (typeof window.authDownload !== "function") return;
      ev.preventDefault();
      window.authDownload(a.getAttribute("href"), a.getAttribute("download") || "output").catch((err) => alert(err.message || err));
    });
  });
  $("outputsSelectAll").checked = false;
  updateOutputButtons();
}

async function loadOutputs() {
  const meta = $("outputsMeta");
  if (!meta) return;
  if (_outputsInflight) return _outputsInflight;
  meta.textContent = "Loading…";
  _outputsInflight = (async () => {
    try {
      const res = await api("/api/outputs?limit=300");
      outputItems = res.items || [];
      renderOutputs();
    } catch (err) {
      meta.textContent = "Could not load outputs: " + err.message;
    }
  })();
  try {
    await _outputsInflight;
  } finally {
    _outputsInflight = null;
  }
}

function initOutputs() {
  if (!$("outputsRefresh")) return;
  $("outputsRefresh").addEventListener("click", () => loadOutputs());
  document.querySelectorAll("[data-output-filter]").forEach((btn) => {
    btn.addEventListener("click", () => {
      outputFilter = btn.dataset.outputFilter || "all";
      document.querySelectorAll("[data-output-filter]").forEach((other) => {
        other.classList.toggle("active", other === btn);
      });
      renderOutputs();
    });
  });
  if ($("outputsExportCsv")) {
    $("outputsExportCsv").addEventListener("click", async () => {
      try {
        const res = await fetch("/api/catalog.csv", { headers: await authHeaders() });
        if (!res.ok) throw new Error(await res.text() || res.statusText);
        const blob = await res.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = "renders.csv";
        a.click();
        URL.revokeObjectURL(url);
      } catch (err) {
        alert("Could not export CSV: " + (err.message || err));
      }
    });
  }
  $("outputsSelectAll").addEventListener("change", () => {
    const on = $("outputsSelectAll").checked;
    document.querySelectorAll(".output-select").forEach((b) => { b.checked = on; });
    updateOutputButtons();
  });
  $("outputsList").addEventListener("change", (e) => {
    if (e.target.classList.contains("output-select")) updateOutputButtons();
  });
  $("outputsList").addEventListener("click", (e) => {
    const btn = e.target && e.target.closest && e.target.closest(".spark-copy-output");
    if (!btn) return;
    const code = (btn.getAttribute("data-spark") || "").trim();
    if (!code) return;
    navigator.clipboard.writeText(code).catch(() => alert(code));
  });

  $("outputsUploadSel").addEventListener("click", async () => {
    const paths = selectedOutputPaths();
    if (!paths.length) return;
    try {
      await openTtUploadSheet({ paths });
    } catch (err) {
      alert("Could not upload: " + (err.message || err));
    }
  });

  if ($("outputsUploadFb")) {
    $("outputsUploadFb").addEventListener("click", () => {
      const paths = selectedOutputPaths();
      if (!paths.length) return;
      openFbAdsModal(paths);
    });
  }

  $("outputsDeleteSel").addEventListener("click", async () => {
    const paths = selectedOutputPaths();
    if (!paths.length) return;
    if (!confirm(`Delete ${paths.length} file${paths.length === 1 ? "" : "s"} permanently?`)) return;
    try {
      await api("/api/outputs/delete", {
        method: "POST",
        body: JSON.stringify({ paths }),
      });
      await loadOutputs();
    } catch (err) {
      alert("Delete failed: " + err.message);
    }
  });
}

// ---------------------------------------------------------------------------
// Settings page
// ---------------------------------------------------------------------------

function setYtCookiesStatus() {
  const el = $("ytCookiesStatus");
  if (!el || !config) return;
  el.textContent = config.hasYtdlpCookies
    ? "cookies.txt on file ✓"
    : "No cookies uploaded";
}

function renderAiProvidersStatus() {
  const el = $("aiProvidersStatus");
  if (!el) return;
  const providers = (config && config.aiProviders) || {};
  const order = ["anthropic", "openai", "xai", "gemini", "perplexity"];
  const labels = { anthropic: "Claude", openai: "OpenAI", xai: "Grok", gemini: "Gemini", perplexity: "Perplexity /research" };
  el.innerHTML = order.map((k) => {
    const p = providers[k] || {};
    const ok = !!p.configured;
    return `<div class="ai-provider-row ${ok ? "ok" : "missing"}">
      <strong>${labels[k] || k}</strong>
      <span>${ok ? "Configured on server ✓" : "Missing server key (" + (p.env || "") + ")"}</span>
    </div>`;
  }).join("") + `<div class="ai-provider-row ${config && config.pinterestSearchConfigured ? "ok" : "missing"}">
      <strong>Pinterest search</strong>
      <span>${config && config.pinterestSearchConfigured ? "Configured on server ✓" : "Missing server key (SCRAPE_CREATORS_API_KEY)"}</span>
    </div>`;
}

// ---------------------------------------------------------------------------
// Shared workspace sync (Create ↔ Chat)
// ---------------------------------------------------------------------------

let workspaceSaveTimer = null;
let workspaceApplying = false;

function collectWorkspacePatch() {
  flushCaptionsStripToStore();
  const mode = $("mode") ? $("mode").value : "matrix";
  const patch = {
    captions: videoCaptionsText || (($("captionsInput") && !isCreateCarouselMode()) ? $("captionsInput").value : videoCaptionsText) || "",
    preserveBreaks: !!($("preserveBreaks") && $("preserveBreaks").checked),
    forceLower: !!($("forceLower") && $("forceLower").checked),
    style: { ...style },
    mode,
    nameTemplate: $("nameTemplate") ? $("nameTemplate").value : "",
    skipExisting: !!($("skipExisting") && $("skipExisting").checked),
    dedupe: !!($("dedupe") && $("dedupe").checked),
    strokePreset: $("strokePreset") ? $("strokePreset").value : "thick",
    positionPreset: $("positionPreset") ? $("positionPreset").value : "center",
    createMode: isCreateCarouselMode() ? "carousel" : "video",
    carouselCaptionsByImage: { ...carouselCaptionsByImageId },
    carouselCaptions: carouselJobCaptionsText || "",
    endCaptionTemplate: ($("endCapTemplateEnabled") && $("endCapTemplateEnabled").checked)
      ? {
          enabled: true,
          text: ($("endCapTemplateText") && $("endCapTemplateText").value) || "",
          durationSeconds: 3,
          xOffset: 0,
          yOffset: 0,
        }
      : null,
  };
  if (!isCreateCarouselMode() && $("captionsInput")) {
    patch.captions = $("captionsInput").value;
    videoCaptionsText = patch.captions;
  }
  if (isCreateCarouselMode() && $("captionsInput")) {
    carouselJobCaptionsText = $("captionsInput").value;
    patch.carouselCaptions = carouselJobCaptionsText;
  }
  if (window.selectedHookOfferId) patch.offerId = window.selectedHookOfferId;
  if (customPairsActive()) {
    patch.renderPairs = workspaceRenderPairs.map((p) => ({
      sourceId: p.sourceId,
      caption: p.caption,
      kind: p.kind,
      slides: p.slides,
      carouselId: p.carouselId,
    }));
  } else if (clearRenderPairsOnSave) {
    patch.renderPairs = [];
    clearRenderPairsOnSave = false;
  }
  return patch;
}

function scheduleWorkspaceSave() {
  if (workspaceApplying || restoring) return;
  clearTimeout(workspaceSaveTimer);
  workspaceSaveTimer = setTimeout(() => {
    saveWorkspaceRemote().catch(() => {});
  }, 400);
}

function cancelWorkspaceSave() {
  clearTimeout(workspaceSaveTimer);
  workspaceSaveTimer = null;
}

async function saveWorkspaceRemote() {
  if (workspaceApplying) return;
  try {
    await api("/api/workspace", {
      method: "PUT",
      body: JSON.stringify(collectWorkspacePatch()),
    });
  } catch (err) {
    console.error("Failed to save workspace settings:", err);
    showToast("Failed to save workspace settings", { error: true });
  }
}

async function loadWorkspaceRemote() {
  try {
    const w = await api("/api/workspace");
    workspaceApplying = true;
    restoring = true;
    try {
      if ($("captionsInput") && typeof w.captions === "string") {
        videoCaptionsText = w.captions;
        if (!isCreateCarouselMode()) $("captionsInput").value = w.captions;
      }
      if (w.carouselCaptionsByImage && typeof w.carouselCaptionsByImage === "object") {
        carouselCaptionsByImageId = { ...w.carouselCaptionsByImage };
      }
      if (typeof w.carouselCaptions === "string") {
        carouselJobCaptionsText = w.carouselCaptions;
      } else {
        migrateCarouselJobCaptionsIfNeeded();
      }
      if ($("preserveBreaks") && "preserveBreaks" in w) $("preserveBreaks").checked = !!w.preserveBreaks;
      if ($("forceLower") && "forceLower" in w) $("forceLower").checked = !!w.forceLower;
      if ($("mode") && (w.mode === "matrix" || w.mode === "pair")) $("mode").value = w.mode;
      workspaceRenderPairs = Array.isArray(w.renderPairs) ? w.renderPairs : [];
      clearRenderPairsOnSave = false;
      // Drop plan locks that still point at videos no longer on Create.
      if (clearStaleWorkspaceRenderPairs()) {
        scheduleWorkspaceSave();
      }
      syncCustomModeOption();
      if ($("nameTemplate") && w.nameTemplate != null) $("nameTemplate").value = w.nameTemplate;
      if ($("skipExisting") && "skipExisting" in w) $("skipExisting").checked = !!w.skipExisting;
      if ($("dedupe") && "dedupe" in w) $("dedupe").checked = !!w.dedupe;
      if ($("strokePreset") && w.strokePreset) $("strokePreset").value = w.strokePreset;
      if ($("positionPreset") && w.positionPreset) $("positionPreset").value = w.positionPreset;
      if (w.offerId) window.selectedHookOfferId = w.offerId;
      if (w.style && typeof w.style === "object") {
        style = { ...DEFAULT_STYLE, ...w.style };
        saveStyle();
        syncSlidersFromStyle();
      }
      if ($("endCapTemplateEnabled")) {
        const t = w.endCaptionTemplate;
        $("endCapTemplateEnabled").checked = !!(t && t.enabled && (t.text || "").trim());
        if ($("endCapTemplateText")) $("endCapTemplateText").value = (t && t.text) || "";
      }
      if (w.createMode === "carousel" || w.createMode === "video") {
        // Defer mode apply until after restoring flags so strip load is clean.
        setTimeout(() => setCreateMode(w.createMode), 0);
      } else {
        loadCaptionsStripForMode();
      }
    } finally {
      restoring = false;
      workspaceApplying = false;
    }
    updateCount();
    updatePreviewSelectors();
    refreshOverlayDebounced();
    return w;
  } catch {
    return null;
  }
}

function initEndCapTemplate() {
  if (!$("endCapTemplateApply")) return;
  ["endCapTemplateEnabled", "endCapTemplateText"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", () => { saveUiState(); scheduleWorkspaceSave(); });
    el.addEventListener("change", () => { saveUiState(); scheduleWorkspaceSave(); });
  });
  $("endCapTemplateApply").addEventListener("click", async () => {
    const text = ($("endCapTemplateText") && $("endCapTemplateText").value || "").trim();
    const enabled = !!($("endCapTemplateEnabled") && $("endCapTemplateEnabled").checked) && !!text;
    const payload = {
      enabled,
      text,
      durationSeconds: 3,
      xOffset: 0,
      yOffset: 0,
    };
    try {
      await api("/api/workspace", {
        method: "PUT",
        body: JSON.stringify({ endCaptionTemplate: enabled ? payload : null }),
      });
      for (const src of sources) {
        const hasCustom = src.endCaption && src.endCaption.enabled && (src.endCaption.text || "").trim();
        if (hasCustom) continue;
        await api(`/api/sources/${src.id}/end-caption`, {
          method: "PUT",
          body: JSON.stringify(payload),
        });
        src.endCaption = { ...payload };
      }
      renderSources();
      refreshOverlay();
      alert(enabled ? "Applied end caption template to videos without a custom one." : "Cleared default end captions where no custom text was set.");
    } catch (err) {
      alert(err.message || String(err));
    }
  });
}

function openCreateDrawer(which) {
  const map = {
    look: "lookDrawer",
    pinterest: "pinterestDrawer",
    library: "libraryDrawer",
    endCaption: "endCaptionDrawer",
  };
  // Only one side drawer at a time (look / end caption / media drawers).
  Object.entries(map).forEach(([key, id]) => {
    if (key === which) return;
    const other = $(id);
    if (!other) return;
    other.classList.add("hidden");
    other.setAttribute("aria-hidden", "true");
  });
  const id = map[which];
  const el = id && $(id);
  if (!el) return;
  el.classList.remove("hidden");
  el.setAttribute("aria-hidden", "false");
}

function closeCreateDrawer(which) {
  const map = {
    look: "lookDrawer",
    pinterest: "pinterestDrawer",
    library: "libraryDrawer",
    endCaption: "endCaptionDrawer",
  };
  const ids = which ? [map[which]].filter(Boolean) : Object.values(map);
  ids.forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.classList.add("hidden");
    el.setAttribute("aria-hidden", "true");
  });
}

function openCreateSheet(which) {
  const id = which === "more" ? "createMoreSheet" : "rendersSheet";
  const el = $(id);
  if (!el) return;
  el.classList.remove("hidden");
  el.setAttribute("aria-hidden", "false");
  if (which === "renders") loadOutputs().catch(() => {});
}

function closeCreateSheet(which) {
  const map = { renders: "rendersSheet", more: "createMoreSheet" };
  const ids = which ? [map[which]].filter(Boolean) : Object.values(map);
  ids.forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.classList.add("hidden");
    el.setAttribute("aria-hidden", "true");
  });
}

function setCreateMode(mode) {
  const workbench = document.querySelector(".create-workbench");
  if (!workbench) return;
  const next = mode === "carousel" ? "carousel" : "video";
  const prev = workbench.dataset.createMode || "video";
  if (prev !== next) flushCaptionsStripToStore();
  workbench.dataset.createMode = next;
  document.querySelectorAll("[data-create-mode]").forEach((btn) => {
    if (!btn.classList.contains("create-mode-btn")) return;
    btn.classList.toggle("active", btn.dataset.createMode === next);
  });
  loadCaptionsStripForMode();
  updateCreateLibraryLabels();
  paintRecentMediaStrip();
  if (next === "carousel") {
    refreshCarouselStoryboard();
    refreshImageLibrary().catch(() => {});
  } else {
    updatePreviewSelectors();
    updateSourcesEmptyState();
  }
  updateCount();
}

let carouselSlideIndex = 0;

function isCreateCarouselMode() {
  const wb = document.querySelector(".create-workbench");
  return !!(wb && wb.dataset.createMode === "carousel");
}

function getCarouselSlides() {
  return (imageLibraryCache || []).filter((img) => img && img.id);
}

function syncCarouselStage(opts = {}) {
  const slides = getCarouselSlides();
  if (opts.preferId) {
    const idx = slides.findIndex((s) => s.id === opts.preferId);
    if (idx >= 0) carouselSlideIndex = idx;
  }
  if (!slides.length) carouselSlideIndex = 0;
  else if (carouselSlideIndex >= slides.length) carouselSlideIndex = slides.length - 1;
  else if (carouselSlideIndex < 0) carouselSlideIndex = 0;

  const nextId = slides[carouselSlideIndex] ? slides[carouselSlideIndex].id : "";
  if (isCreateCarouselMode()) {
    // Captions are storyboard-level — changing slides must not swap the caption box.
    carouselCaptionsFocusId = nextId;
    migrateCarouselJobCaptionsIfNeeded();
    if ($("captionsInput") && $("captionsInput").value !== (carouselJobCaptionsText || "")) {
      // Keep strip as source of truth while typing; only hydrate if empty.
      if (!(String($("captionsInput").value || "").trim()) && (carouselJobCaptionsText || "").trim()) {
        $("captionsInput").value = carouselJobCaptionsText;
      }
    }
  }

  const prev = $("carouselPrevBtn");
  const next = $("carouselNextBtn");
  const meta = $("carouselSlideMeta");
  if (prev) prev.disabled = slides.length < 2 || carouselSlideIndex <= 0;
  if (next) next.disabled = slides.length < 2 || carouselSlideIndex >= slides.length - 1;
  if (meta) {
    meta.textContent = slides.length
      ? `Slide ${carouselSlideIndex + 1} / ${slides.length}`
      : "No slides";
  }

  const imgSel = $("previewCarouselImage");
  if (imgSel && nextId && [...imgSel.options].some((o) => o.value === nextId)) {
    imgSel.value = nextId;
  }

  document.querySelectorAll("#carouselStoryboardTrack .carousel-slide-thumb").forEach((el, i) => {
    el.classList.toggle("active", i === carouselSlideIndex);
  });
  document.querySelectorAll("#imageLibraryGrid .image-lib-tile").forEach((el, i) => {
    el.classList.toggle("is-stage-slide", i === carouselSlideIndex);
  });

  updateCaptionsContextHint();
  if (isCreateCarouselMode()) {
    updatePreviewSelectors({
      previewCarouselImageId: nextId,
      previewCaptionIdx: $("previewCaption") && $("previewCaption").value,
    });
  }
}

function stepCarouselSlide(delta) {
  const slides = getCarouselSlides();
  if (slides.length < 2) return;
  carouselSlideIndex = Math.max(0, Math.min(slides.length - 1, carouselSlideIndex + delta));
  syncCarouselStage();
}

function jumpCarouselSlide(index) {
  const slides = getCarouselSlides();
  if (!slides.length) return;
  carouselSlideIndex = Math.max(0, Math.min(slides.length - 1, Number(index) || 0));
  syncCarouselStage();
}

function setCreateView(view) {
  const workbench = document.querySelector(".create-workbench");
  if (!workbench) return;
  const allowed = new Set(["media", "captions", "hooks", "edit", "more"]);
  let next = allowed.has(view) ? view : "edit";
  if (next === "hooks") next = "captions";
  workbench.dataset.createView = next;
  document.querySelectorAll(".create-phone-chips [data-create-view]").forEach((btn) => {
    const on = btn.dataset.createView === next;
    btn.classList.toggle("active", on);
    btn.setAttribute("aria-selected", on ? "true" : "false");
  });
  if (next === "more") openCreateSheet("more");
  else closeCreateSheet("more");
}

function recentMediaThumbHtml(item, kind) {
  let thumb = "";
  if (kind === "image") {
    thumb = withAuth(item.thumbUrl || item.fileUrl || (item.id ? `/api/images/${item.id}/file` : ""));
  } else {
    // Match library modal: frame endpoint is the reliable video thumb.
    thumb = withAuth(
      item.thumbUrl ||
        (item.id ? `/api/sources/${item.id}/frame` : "") ||
        item.fileUrl ||
        ""
    );
  }
  if (thumb) {
    return `<img alt="" loading="lazy" decoding="async" src="${escapeHtml(thumb)}" />`;
  }
  return `<div class="recent-thumb-fallback" aria-hidden="true"></div>`;
}

function paintRecentMediaStrip(opts = {}) {
  const strip = $("recentMediaStrip");
  if (!strip) return;
  updateCreateLibraryLabels();
  const maxItems = 36;
  const parts = [];

  if (isCreateCarouselMode()) {
    const images = Array.isArray(imageLibraryCache) ? imageLibraryCache : [];
    images.slice(0, maxItems).forEach((img) => {
      parts.push(
        `<button type="button" class="recent-media-item" data-recent-kind="image" data-id="${escapeHtml(img.id)}" title="${escapeHtml(img.name || "")}">` +
          recentMediaThumbHtml(img, "image") +
          `<div class="recent-label">${escapeHtml(img.name || "image")}</div></button>`
      );
    });
    if (parts.length) {
      strip.innerHTML = parts.join("");
      return;
    }
    if (opts.loading) {
      strip.innerHTML = `<span class="recent-media-empty">Loading images…</span>`;
      return;
    }
    strip.innerHTML = `<span class="recent-media-empty">No images yet — drop images or search Pinterest.</span>`;
    return;
  }

  const jobIds = new Set((sources || []).map((s) => s && s.id).filter(Boolean));
  const videos = [];
  const seenV = new Set();
  (libraryCache || []).forEach((v) => {
    if (!v || !v.id || seenV.has(v.id)) return;
    seenV.add(v.id);
    videos.push(v);
  });
  (sources || []).forEach((s) => {
    if (!s || !s.id || seenV.has(s.id)) return;
    seenV.add(s.id);
    videos.push(s);
  });
  videos.slice(0, maxItems).forEach((v) => {
    const inJob = jobIds.has(v.id) || !!v.inJob;
    parts.push(
      `<button type="button" class="recent-media-item${inJob ? " is-in-job" : ""}" data-recent-kind="video" data-id="${escapeHtml(v.id)}" title="${escapeHtml(v.name || "")}">` +
        recentMediaThumbHtml(v, "video") +
        `<div class="recent-label">${escapeHtml(v.name || "video")}</div></button>`
    );
  });
  if (parts.length) {
    strip.innerHTML = parts.join("");
    return;
  }
  if (opts.loading) {
    strip.innerHTML = `<span class="recent-media-empty">Loading library…</span>`;
    return;
  }
  strip.innerHTML = `<span class="recent-media-empty">No past videos yet — drop files or open Library.</span>`;
}

async function refreshRecentMediaStrip({ background = true } = {}) {
  const strip = $("recentMediaStrip");
  if (!strip) return;
  const req = ++recentMediaStripReq;
  const hasCache = (libraryCache && libraryCache.length) || (imageLibraryCache && imageLibraryCache.length) || (sources && sources.length);
  if (hasCache) paintRecentMediaStrip();
  else paintRecentMediaStrip({ loading: true });

  const fetchFresh = async () => {
    const [lib, imgs] = await Promise.all([
      loadLibraryCache().catch(() => libraryCache || []),
      api("/api/images").catch(() => ({ items: imageLibraryCache || [] })),
    ]);
    if (req !== recentMediaStripReq) return;
    const items = (imgs && imgs.items) || (Array.isArray(imgs) ? imgs : []) || [];
    imageLibraryCache = items;
    // loadLibraryCache already updated libraryCache
    void lib;
    paintRecentMediaStrip();
  };

  if (background && hasCache) {
    fetchFresh().catch(() => {});
    return;
  }
  try {
    await fetchFresh();
  } catch {
    if (req === recentMediaStripReq) paintRecentMediaStrip();
  }
}

function refreshCarouselStoryboard() {
  const track = $("carouselStoryboardTrack");
  if (!track) return;
  const slides = getCarouselSlides();
  if (!slides.length) {
    track.innerHTML = `<span class="param-hint">Add images to the storyboard</span>`;
    syncCarouselStage();
    return;
  }
  track.innerHTML = slides
    .map((img, i) => {
      const src = withAuth(img.fileUrl || img.thumbUrl || `/api/images/${img.id}/file`);
      return `<button type="button" class="carousel-slide-thumb${i === carouselSlideIndex ? " active" : ""}" data-slide-index="${i}" title="Slide ${i + 1}">` +
        `<img src="${escapeHtml(src)}" alt="Slide ${i + 1}" /></button>`;
    })
    .join("");
  syncCarouselStage();
}

async function gatherCreateUploadPairs() {
  const bySrc = Object.fromEntries((sources || []).filter((s) => s && s.id).map((s) => [s.id, s]));
  const byImg = Object.fromEntries(
    (imageLibraryCache || []).filter((i) => i && i.id).map((i) => [i.id, i])
  );
  const nameOf = (id) =>
    (bySrc[id] && (bySrc[id].name || bySrc[id].stem)) ||
    (byImg[id] && (byImg[id].name || byImg[id].stem)) ||
    id;

  if (isCreateCarouselMode()) {
    return allCarouselCaptionPairs();
  }
  // Stale plan locks (missing source ids) are cleared inside customPairsActive().
  if (customPairsActive()) {
    return workspaceRenderPairs.map((p) => ({
      sourceId: p.sourceId,
      caption: p.caption,
      sourceName: p.sourceName || nameOf(p.sourceId),
      pinUrl: p.pinUrl,
    }));
  }
  const sourceIds = (sources || []).map((s) => s.id).filter(Boolean);
  const caps = captionLines();
  if (!sourceIds.length || !caps.length) return [];
  const mode = ($("mode") && $("mode").value) || "matrix";
  if (mode === "pair") {
    const n = Math.min(sourceIds.length, caps.length);
    return Array.from({ length: n }, (_, i) => ({
      sourceId: sourceIds[i],
      caption: caps[i],
      sourceName: nameOf(sourceIds[i]),
    }));
  }
  const out = [];
  sourceIds.forEach((sid) =>
    caps.forEach((c) => out.push({ sourceId: sid, caption: c, sourceName: nameOf(sid) }))
  );
  return out;
}

async function openCreateDockPlanReview(intent) {
  if (!isCreateCarouselMode()) {
    if (selectedSourceId) {
      clearTimeout(endCapSaveTimer);
      try { await persistEndCaption(selectedSourceId); } catch {}
    }
    try {
      await flushAllCrops();
      await flushAllTrims();
    } catch {}
  }
  const pairs = await gatherCreateUploadPairs();
  if (!pairs.length) {
    alert(
      isCreateCarouselMode()
        ? "Add images and captions (per image) first."
        : "Add videos and captions (or activate a Plan) first."
    );
    return;
  }
  const mode = isCreateCarouselMode()
    ? "custom"
    : (($("mode") && $("mode").value) || "matrix");
  if (typeof window.openCreatePlanReview === "function") {
    await window.openCreatePlanReview({ intent, pairs, mode, autoExecute: true });
  } else {
    console.error("openCreatePlanReview not available from chat.js");
    alert("Plan review module not loaded. Try refreshing the page.");
  }
}

function captionLines() {
  if (isCreateCarouselMode()) {
    flushCarouselCaptionsFromStrip();
    migrateCarouselJobCaptionsIfNeeded();
    return parseCaptionText(carouselJobCaptionsText || "");
  }
  const raw = ($("captionsInput") && $("captionsInput").value) || "";
  return parseCaptionText(raw);
}

let createUploadWantSparks = false;

async function runCreateDockUpload(mode) {
  createUploadWantSparks = mode === "sparks";
  const menu = $("createUploadMenu");
  if (menu) menu.classList.add("hidden");
  const btn = $("createUploadBtn");
  if (btn) btn.setAttribute("aria-expanded", "false");
  if (mode === "sparks") {
    const ok = await ensureSparkCapableAccountAvailable();
    if (!ok) return;
  }
  await openCreateDockPlanReview(mode === "sparks" ? "sparks" : "upload");
}

async function ensureSparkCapableAccountAvailable() {
  try {
    const accounts = await api("/api/tiktok/accounts");
    const ready = (accounts || []).filter(
      (a) => a.enabled && a.hasCookies && a.sparkCapable && a.status !== "missing_cookies"
    );
    if (!ready.length) {
      alert("No DuoPlus-linked TikTok accounts. Sparks upload needs an account like Gibby with cloud-phone login.");
      return false;
    }
    return true;
  } catch (err) {
    alert(err.message || String(err));
    return false;
  }
}

async function syncCreateSparksUploadOption() {
  const opts = document.querySelectorAll(
    "#createUploadMenu [data-upload-mode='sparks'], #chatPreviewUploadMenu [data-upload-mode='sparks']"
  );
  if (!opts.length) return;
  let show = false;
  try {
  let cfg = config || window.config;
  if (!cfg || typeof cfg.duoPlusConfigured === "undefined") {
    cfg = await api("/api/config");
    if (cfg && !config) {
      config = cfg;
      window.config = cfg;
    }
  }
  if (!cfg.duoPlusConfigured) {
      opts.forEach((el) => {
        el.classList.add("hidden");
        el.hidden = true;
      });
      return;
    }
    const accounts = await api("/api/tiktok/accounts");
    show = (accounts || []).some(
      (a) => a.enabled && a.hasCookies && a.sparkCapable && a.status !== "missing_cookies"
    );
  } catch {
    show = false;
  }
  opts.forEach((el) => {
    el.classList.toggle("hidden", !show);
    el.hidden = !show;
  });
}

async function maybeFetchSparksForPendingUpload() {
  const pending = window.__pendingWorkspaceSpark;
  if (!pending) return;
  const threadId =
    (typeof window.getChatActiveThreadId === "function" && window.getChatActiveThreadId()) || "";
  if (!threadId) return;
  try {
    await api(`/api/chat/threads/${encodeURIComponent(threadId)}/plan`, {
      method: "PUT",
      body: JSON.stringify({
        renderPairs: pending.pairs,
        title: ($("workspacePlanName") && $("workspacePlanName").value) || "Workspace plan",
      }),
    });

    // Show immediate feedback BEFORE the API request
    showAsyncStartFeedback({
      message: `Fetching Spark codes for ${pending.pairs.length} post${pending.pairs.length === 1 ? "" : "s"}…`,
      showDock: true,
      expandDock: false,
      toastMs: 4000
    });

    const job = await api(`/api/chat/threads/${encodeURIComponent(threadId)}/plan/fetch-spark`, {
      method: "POST",
      body: JSON.stringify({ pairs: pending.pairs }),
    });
    // Update dock and start watching for persistence across refresh
    if (job && job.id && typeof watchSparkJob === "function") {
      watchSparkJob(job.id);
    }
    window.__pendingWorkspaceSpark = null;
  } catch {
    /* user can retry from Plan */
  }
}

async function activateWorkspacePlanFromUi() {
  const name = ($("workspacePlanName") && $("workspacePlanName").value.trim()) || "Untitled plan";
  let pairs = await gatherCreateUploadPairs();
  if (!pairs.length && workspaceRenderPairsSourcesOk(workspaceRenderPairs)) {
    pairs = workspaceRenderPairs.map((p) => ({ sourceId: p.sourceId, caption: p.caption }));
  }
  const list = await api("/api/plans");
  let planId = list.activePlanId || "";
  if (planId) {
    await api(`/api/plans/${encodeURIComponent(planId)}`, {
      method: "PUT",
      body: JSON.stringify({ name, renderPairs: pairs.length ? pairs : undefined }),
    });
    await api(`/api/plans/${encodeURIComponent(planId)}/activate`, { method: "POST" });
  } else {
    const created = await api("/api/plans", {
      method: "POST",
      body: JSON.stringify({ name, renderPairs: pairs, activate: true }),
    });
    planId = created.id;
  }
  if (pairs.length) {
    workspaceRenderPairs = pairs;
    syncCustomModeOption();
    await api("/api/workspace", {
      method: "PUT",
      body: JSON.stringify({
        renderPairs: pairs,
        mode: ($("mode") && $("mode").value) || "matrix",
        activePlanId: planId,
      }),
    });
  } else if (clearStaleWorkspaceRenderPairs() || clearRenderPairsOnSave) {
    await api("/api/workspace", {
      method: "PUT",
      body: JSON.stringify({ renderPairs: [], activePlanId: planId }),
    });
  }
  if ($("workspacePlanName")) $("workspacePlanName").value = name;
  alert(`“${name}” is the active plan.`);
}

/**
 * Single handler for #workspacePlanActivateBtn.
 * Create page → Create pairs; Chat (or preview opened from chat) → chat preview pairs.
 */
async function onThisIsThePlanClick() {
  const page = document.body.dataset.page || currentPage();
  const modal = $("chatPreviewModal");
  const previewOpen = !!(modal && !modal.classList.contains("hidden"));
  if (page === "create") {
    await activateWorkspacePlanFromUi();
    return;
  }
  await activateWorkspacePlanFromUi();
}

function wireThisIsThePlanButton() {
  const btn = $("workspacePlanActivateBtn");
  if (!btn || btn.dataset.planActivateWired) return;
  btn.dataset.planActivateWired = "1";
  btn.addEventListener("click", () => {
    onThisIsThePlanClick().catch((err) => alert(err.message || String(err)));
  });
}
window.wireThisIsThePlanButton = wireThisIsThePlanButton;

function initCreateRacks() {
  const workbench = document.querySelector(".create-workbench");
  if (!workbench) return;

  initImageLibraryGridDnD();
  wireRemoveAllStoryboardBtn();

  document.querySelectorAll(".create-mode-btn[data-create-mode]").forEach((btn) => {
    btn.addEventListener("click", () => setCreateMode(btn.dataset.createMode));
  });
  document.querySelectorAll(".create-phone-chips [data-create-view]").forEach((btn) => {
    btn.addEventListener("click", () => setCreateView(btn.dataset.createView));
  });
  // Note: Offers panel functionality removed (was undefined)

  const batchBtn = $("createBatchBtn");
  const batchPop = $("createBatchPopover");
  if (batchBtn && batchPop) {
    batchBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = batchPop.classList.toggle("hidden") === false;
      batchBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
  }

  $("createRendersBtn")?.addEventListener("click", () => openCreateSheet("renders"));
  $("createPlanBtn")?.addEventListener("click", () => {
    const modal = $("chatPreviewModal");
    if (modal) {
      modal.classList.remove("hidden");
      return;
    }
    alert("Plan is still loading — try again in a moment.");
  });
  $("openPinterestBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openCreateDrawer("pinterest");
  });
  $("openLookBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openCreateDrawer("look");
  });
  $("openEndCaptionBtn")?.addEventListener("click", (e) => {
    e.preventDefault();
    e.stopPropagation();
    openEndCaptionFromStage();
  });

  document.querySelectorAll("[data-close-drawer]").forEach((el) => {
    el.addEventListener("click", () => {
      const which = el.getAttribute("data-close-drawer");
      if (which === "library") {
        closeVideoLibraryModal({ apply: false });
        return;
      }
      closeCreateDrawer(which);
    });
  });
  document.querySelectorAll("[data-close-sheet]").forEach((el) => {
    el.addEventListener("click", () => closeCreateSheet(el.getAttribute("data-close-sheet")));
  });

  $("morePinterestBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    openCreateDrawer("pinterest");
  });
  $("moreLookBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    openCreateDrawer("look");
  });
  $("moreEndCaptionBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    openEndCaptionFromStage();
  });
  $("moreBatchBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    batchBtn?.click();
  });
  $("moreRendersBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    openCreateSheet("renders");
  });
  $("morePlanBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    $("createPlanBtn")?.click();
  });
  $("moreLibraryBtn")?.addEventListener("click", () => {
    closeCreateSheet("more");
    openVideoLibraryModal();
  });

  const uploadBtn = $("createUploadBtn");
  const uploadMenu = $("createUploadMenu");
  if (uploadBtn && uploadMenu) {
    uploadBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      const open = uploadMenu.classList.toggle("hidden") === false;
      uploadBtn.setAttribute("aria-expanded", open ? "true" : "false");
    });
    uploadMenu.querySelectorAll("[data-upload-mode]").forEach((opt) => {
      opt.addEventListener("click", () => runCreateDockUpload(opt.dataset.uploadMode));
    });
  }

  document.addEventListener("click", (e) => {
    const t = e.target;
    if (batchPop && !batchPop.classList.contains("hidden")) {
      if (!t.closest || !t.closest(".create-batch-wrap")) {
        batchPop.classList.add("hidden");
        batchBtn?.setAttribute("aria-expanded", "false");
      }
    }
    if (uploadMenu && !uploadMenu.classList.contains("hidden")) {
      if (!t.closest || !t.closest(".create-upload-wrap")) {
        uploadMenu.classList.add("hidden");
        uploadBtn?.setAttribute("aria-expanded", "false");
      }
    }
    const chatMenu = $("chatPreviewUploadMenu");
    if (chatMenu && !chatMenu.classList.contains("hidden")) {
      if (!t.closest || !t.closest(".chat-plan-upload-wrap")) {
        chatMenu.classList.add("hidden");
        $("chatPreviewUploadBtn")?.setAttribute("aria-expanded", "false");
      }
    }
    const chatSparksMenu = $("chatPreviewSparksMenu");
    if (chatSparksMenu && !chatSparksMenu.classList.contains("hidden")) {
      if (!t.closest || !t.closest(".chat-plan-sparks-wrap")) {
        chatSparksMenu.classList.add("hidden");
        $("chatPreviewSparksBtn")?.setAttribute("aria-expanded", "false");
      }
    }
  });

  // Keep Facebook UI dormant even if initFacebook runs.
  const fbBtn = $("outputsUploadFb");
  if (fbBtn) {
    fbBtn.hidden = true;
    fbBtn.classList.add("hidden");
  }

  setCreateMode("video");
  setCreateView("edit");
  updateSourcesEmptyState();
  wireThisIsThePlanButton();
}

async function loadActiveWorkspacePlanName() {
  try {
    const list = await api("/api/plans");
    const active = (list.plans || []).find((p) => p.active || p.id === list.activePlanId);
    if (active && $("workspacePlanName")) $("workspacePlanName").value = active.name || "";
  } catch {
    /* ignore */
  }
}

function initSettingsNav() {
  document.querySelectorAll("[data-settings]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const id = btn.dataset.settings;
      document.querySelectorAll("[data-settings]").forEach((other) => {
        other.classList.toggle("active", other === btn);
      });
      document.querySelectorAll("[data-settings-pane]").forEach((pane) => {
        const on = pane.dataset.settingsPane === id;
        pane.classList.toggle("hidden", !on);
        pane.classList.toggle("is-active", on);
      });
      if (id === "bc") {
        loadBcFinance().catch(() => {});
      }
    });
  });
}

function initVisualViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const sync = () => {
    const collapsed = window.innerHeight - vv.height > 80;
    document.body.classList.toggle("keyboard-open", collapsed);
    document.body.style.height = collapsed ? `${Math.round(vv.height)}px` : "";
  };
  vv.addEventListener("resize", sync);
  vv.addEventListener("scroll", sync);
}

const BC_CREATE_OPTION = "__create__";
const BC_FINANCE_CACHE_KEY = "captionToolBcFinance_v2";
const BC_FINANCE_TTL_MS = 10 * 60 * 1000; // 10 minutes — only refetch when stale or after save
let bcFinanceState = { vatId: "", sparkCodeId: "", cardId: "", hostId: "", hosts: [] };
globalThis.bcFinanceState = bcFinanceState;
let _bcFinanceInflight = null;
let _bcFinanceLoadedAt = 0;

function readBcFinanceCache() {
  try {
    const raw = localStorage.getItem(BC_FINANCE_CACHE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || !parsed.data) return null;
    return parsed;
  } catch {
    return null;
  }
}

function writeBcFinanceCache(data) {
  try {
    localStorage.setItem(
      BC_FINANCE_CACHE_KEY,
      JSON.stringify({ savedAt: Date.now(), data })
    );
  } catch {
    /* quota */
  }
  _bcFinanceLoadedAt = Date.now();
}

function clearBcFinanceCache() {
  try {
    localStorage.removeItem(BC_FINANCE_CACHE_KEY);
  } catch {}
  _bcFinanceLoadedAt = 0;
}

function setBcFinanceLoading(on, msg, { soft = false } = {}) {
  const panel = $("bcFinancePanel");
  const loading = $("bcFinanceLoading");
  if (panel) {
    panel.classList.toggle("is-loading", !!on && !soft);
    panel.classList.toggle("is-refreshing", !!on && soft);
    if (!on) {
      panel.classList.remove("is-loading", "is-refreshing");
    }
  }
  if (loading) {
    loading.classList.toggle("hidden", !on);
    const label = loading.querySelector("span:not(.spin)");
    if (label && msg) label.textContent = msg;
  }
}

function fillSelectWithCreate(sel, options, selectedId, emptyLabel) {
  if (!sel) return;
  const rows = options || [];
  const opts = rows.map(
    (r) =>
      `<option value="${escapeHtml(r.id)}">${escapeHtml(r.label || r.number || r.code || r.id)}</option>`
  );
  if (!rows.length && emptyLabel) {
    opts.unshift(`<option value="">${escapeHtml(emptyLabel)}</option>`);
  }
  opts.push(`<option value="${BC_CREATE_OPTION}">+ Create…</option>`);
  sel.innerHTML = opts.join("");
  if (selectedId && rows.some((r) => r.id === selectedId)) {
    sel.value = selectedId;
  } else if (rows.length) {
    sel.value = rows[0].id;
  } else {
    sel.value = "";
  }
}

/** Offer URL options for Sales default landing dropdown. */
let _bcLandingOffers = [];
let _bcLandingOffersPrefetch = null;

function prefetchBcLandingOffers(force) {
  if (!force && _bcLandingOffersPrefetch) return _bcLandingOffersPrefetch;
  _bcLandingOffersPrefetch = (async () => {
    const data = await api("/api/hooks/offers", { timeoutMs: 20000 });
    const rows = Array.isArray(data) ? data : data.offers || [];
    _bcLandingOffers = Array.isArray(rows) ? rows : [];
    return _bcLandingOffers;
  })().catch((err) => {
    _bcLandingOffersPrefetch = null;
    throw err;
  });
  return _bcLandingOffersPrefetch;
}

function fillBcSalesLandingSelect(selectedUrl) {
  const sel = $("bcSalesLanding");
  if (!sel) return;
  const want = String(selectedUrl || "").trim();
  const seen = new Set();
  const opts = [
    `<option value="">(blank — use offer URL at load time)</option>`,
  ];
  for (const o of _bcLandingOffers) {
    const url = String((o && (o.url || o.landingUrl)) || "").trim();
    if (!url || seen.has(url)) continue;
    seen.add(url);
    const name = String((o && o.name) || o.id || "Offer").trim();
    opts.push(
      `<option value="${escapeHtml(url)}">${escapeHtml(name)} — ${escapeHtml(url)}</option>`
    );
  }
  // Preserve a custom saved URL that is not in the offer list.
  if (want && !seen.has(want)) {
    opts.push(
      `<option value="${escapeHtml(want)}">Custom — ${escapeHtml(want)}</option>`
    );
  }
  sel.innerHTML = opts.join("");
  sel.value = want && [...sel.options].some((o) => o.value === want) ? want : "";
}

function setFinanceModalOpen(id, open) {
  const el = $(id);
  if (!el) return;
  el.classList.toggle("hidden", !open);
}

function syncBcHostActionButtons() {
  const hostId =
    $("bcHostId") && $("bcHostId").value !== BC_CREATE_OPTION ? $("bcHostId").value : "";
  const has = Boolean(hostId);
  if ($("bcHostEditBtn")) $("bcHostEditBtn").disabled = !has;
  if ($("bcHostDeleteBtn")) $("bcHostDeleteBtn").disabled = !has;
}

async function refreshBcWorkerHostHint() {
  const el = $("bcWorkerHostHint");
  if (!el) return;
  try {
    const st = await api("/api/provision/worker-status");
    const host = st.activeHost || {};
    const embed = st.embed || {};
    const name = host.name || st.hostName || st.activeHostId || "—";
    const parts = [
      st.online ? "Worker online" : "Worker offline",
      `host ${name}`,
    ];
    if (embed.running && embed.pid) parts.push(`embed pid ${embed.pid}`);
    el.textContent = parts.join(" · ");
  } catch {
    el.textContent = "";
  }
}

function applyBcFinanceState(s) {
  if (!s || typeof s !== "object") return;
  bcFinanceState = {
    vatId: s.vatId || "",
    sparkCodeId: s.sparkCodeId || "",
    cardId: s.cardId || "",
    hostId: s.activeHostId || "",
    hosts: Array.isArray(s.hosts) ? s.hosts : [],
    extraAdAccounts: Math.max(0, Math.min(20, Number(s.extraAdAccounts) || 0)),
    businessCentersPerProfile: 1,
  };
  globalThis.bcFinanceState = bcFinanceState;
  const hosts = (s.hosts || []).map((h) => ({
    id: h.id,
    label: h.name || h.id,
  }));
  fillSelectWithCreate($("bcHostId"), hosts, s.activeHostId, "No hosts yet");
  syncBcHostActionButtons();
  const activeHost = s.activeHost || {};
  if ($("bcHostHint")) {
    if (activeHost.id) {
      const keyHint =
        activeHost.apiKey && activeHost.apiKey.configured
          ? `key ${activeHost.apiKey.hint || "…"}`
          : "no key";
      $("bcHostHint").textContent = `${activeHost.name || activeHost.id} · ${
        activeHost.adspowerUrl || ""
      } · ${keyHint}`;
    } else {
      $("bcHostHint").textContent = "No AdsPower host saved yet. Use + Create… to add this PC, VPS, or Mac.";
    }
  }
  // Worker hint is secondary — don't block form paint.
  refreshBcWorkerHostHint().catch(() => {});

  const vats = (s.vats || []).map((v) => ({
    id: v.id,
    label: v.label || v.number || v.id,
  }));
  fillSelectWithCreate($("bcVatId"), vats, s.vatId, "No VATs yet");

  const sparks = (s.sparkCodes || []).map((r) => ({
    id: r.id,
    label: r.label || r.code || r.id,
  }));
  fillSelectWithCreate($("bcSparkCodeId"), sparks, s.sparkCodeId, "No Spark codes yet");

  let cards = (s.cards || []).map((c) => ({
    id: c.id,
    label: c.label || (c.last4 ? `•••• ${c.last4}` : c.id),
  }));
  // Stale caches / older API payloads may expose card.configured without cards[].
  if (!cards.length && s.card && s.card.configured && (s.card.id || s.card.last4)) {
    cards = [
      {
        id: s.card.id || s.cardId || "primary",
        label: s.card.label || (s.card.last4 ? `•••• ${s.card.last4}` : "Primary card"),
      },
    ];
  }
  fillSelectWithCreate($("bcCardId"), cards, s.cardId || (s.card && s.card.id) || "", "No cards yet");

  if ($("bcAddAmount")) $("bcAddAmount").value = s.addBalanceAmount ?? 10;
  if ($("bcDailyLimit")) $("bcDailyLimit").value = s.mercuryDailyLimit ?? 50;
  if ($("bcExtraAdAccounts")) {
    $("bcExtraAdAccounts").value = 1 + Math.max(0, Math.min(20, Number(s.extraAdAccounts) || 0));
    $("bcExtraAdAccounts").disabled = false;
  }
  const oneBcRadio = document.querySelector('input[name="bcBusinessCentersPerProfile"][value="1"]');
  if (oneBcRadio) oneBcRadio.checked = true;
  if ($("bcAdBudget")) $("bcAdBudget").value = s.adAccountBudgetUsd ?? 5;
  if ($("bcSalesBid")) $("bcSalesBid").value = s.salesConversionBidUsd ?? 0.4;
  if ($("bcSalesBudget")) $("bcSalesBudget").value = s.salesDailyBudgetUsd ?? 20;
  fillBcSalesLandingSelect(s.salesLandingUrl || "");
  prefetchBcLandingOffers()
    .then(() => fillBcSalesLandingSelect(s.salesLandingUrl || $("bcSalesLanding")?.value || ""))
    .catch(() => fillBcSalesLandingSelect(s.salesLandingUrl || ""));
  if ($("bcSalesInstantCta")) $("bcSalesInstantCta").value = s.salesInstantPageCta || "Continue";
  if ($("bcSalesCreativeCta")) $("bcSalesCreativeCta").value = s.salesCreativeCta || "Learn more";
  if ($("bcSalesDisplayCard")) $("bcSalesDisplayCard").checked = s.salesDisplayCard !== false;
  if ($("bcSalesAdName")) $("bcSalesAdName").value = s.salesAdNameTemplate || "{offer}-{i}";
  if ($("bcSalesCreativeText")) $("bcSalesCreativeText").value = s.salesCreativeText || "";

  const card = s.card || {};
  if ($("bcCardHint")) {
    $("bcCardHint").textContent = card.configured
      ? `Selected card ending ${card.last4}, exp ${card.exp}${card.name ? `, cardholder ${card.name}` : ""}.`
      : "No card saved yet. Use + Create… to add one. Cardholder must be the name on the card."
  }
  const preview = s.runPreview || {};
  const banner = $("bcSpendBanner");
  if (banner) {
    const msg = (s.spend && s.spend.message) || preview.message || "";
    banner.textContent = msg;
    banner.classList.toggle("hidden", !msg);
  }
  applyBcAddressState(s.billingAddresses || null);
}

function applyBcAddressState(addr) {
  const hint = $("bcAddressHint");
  const stats = $("bcAddressStats");
  const list = $("bcAddressList");
  if (!stats || !list) return;
  if (!addr) {
    stats.textContent = "";
    list.innerHTML = `<div class="bc-settings-list-empty">Loading addresses…</div>`;
    return;
  }
  const next = addr.nextAddress || {};
  const pin = addr.pin;
  const mode = pin
    ? `Pinned · ${pin.street || ""} ${pin.postalCode || ""}`
    : `Rotating · next #${addr.cursorIndex ?? 0}: ${next.street || ""} ${next.postalCode || ""}`;
  stats.textContent = `${mode} · pool ${addr.poolSize ?? 0}`;
  if (hint) {
    hint.textContent = pin
      ? "A pin is active — every new BC uses that street/postal until you clear it."
      : "Warm-up cycles Stockholm streets/postals. Pin one address to reuse it, or add a custom row to the pool.";
  }
  const rows = Array.isArray(addr.addresses) ? addr.addresses : [];
  if (!rows.length) {
    list.innerHTML = `<div class="bc-settings-list-empty">No addresses in pool.</div>`;
    return;
  }
  const pinKey = pin
    ? `${String(pin.street || "").trim()}|${String(pin.postalCode || "").replace(/\D/g, "")}`
    : "";
  list.innerHTML = rows
    .map((r) => {
      const key = `${String(r.street || "").trim()}|${String(r.postalCode || "").replace(/\D/g, "")}`;
      const isPinned = pinKey && key === pinKey;
      const tag = [
        r.custom ? "custom" : "pool",
        isPinned ? "pinned" : "",
      ]
        .filter(Boolean)
        .join(" · ");
      return `<div class="bc-settings-list-row" data-street="${escapeHtml(r.street || "")}" data-postal="${escapeHtml(r.postalCode || "")}">
        <div>
          <div>${escapeHtml(r.street || "")} · ${escapeHtml(r.postalCode || "")}</div>
          <div class="bc-settings-list-meta">${escapeHtml(tag)}${r.source ? ` · ${escapeHtml(r.source)}` : ""}</div>
        </div>
        <button type="button" class="btn" data-pin-address="${isPinned ? "1" : "0"}">${isPinned ? "Pinned" : "Pin"}</button>
      </div>`;
    })
    .join("");
}

function applyBcProxyState(data) {
  const stats = $("bcProxyStats");
  const list = $("bcProxyList");
  const hint = $("bcProxyHint");
  if (!stats || !list) return;
  if (!data) {
    stats.textContent = "";
    list.innerHTML = `<div class="bc-settings-list-empty">Loading proxies…</div>`;
    return;
  }
  const res = data.residential || {};
  const dc = data.datacenter || {};
  const plan = data.residentialPlan || {};
  const counts = data.counts || {};
  const bw =
    plan.bandwidthUnlimited
      ? "bandwidth unlimited"
      : plan.bandwidthLimitGb != null
        ? `bandwidth limit ${plan.bandwidthLimitGb} GB`
        : "bandwidth n/a";
  const refreshLeft =
    plan.onDemandRefreshesAvailable != null
      ? `${plan.onDemandRefreshesAvailable} IP refresh(es) left (${plan.onDemandRefreshesUsed || 0}/${plan.onDemandRefreshesTotal || 0} used)`
      : "IP refresh n/a";
  const subtype = plan.isResidential
    ? "residential"
    : plan.proxySubtype || "geo";
  stats.textContent = [
    `Residential ${subtype}: ${res.inUse ?? 0} in use · ${res.free ?? 0} free · ${res.total ?? 0} total`,
    `Data center: ${dc.inUse ?? 0} in use · ${dc.free ?? 0} free · ${dc.total ?? 0} total`,
    `All cached: ${counts.inUse ?? 0} in use · ${counts.free ?? 0} free · ${counts.total ?? 0} total`,
    `Plan ${plan.id || data.geoPlanId || "—"} · ${bw} · ${refreshLeft}`,
    data.lastSyncAt ? `last sync ${data.lastSyncAt}` : "",
  ]
    .filter(Boolean)
    .join(" · ");
  if (hint) {
    if (data.webshareError) {
      hint.textContent = data.webshareError;
    } else {
      hint.textContent =
        "Residential (geo) and data center proxies. Refresh each pool from Webshare separately; “Request new IPs” spends an on-demand refresh on the residential plan when available.";
    }
  }
  const rows = Array.isArray(data.proxies) ? data.proxies : [];
  if (!rows.length) {
    list.innerHTML = `<div class="bc-settings-list-empty">No proxies synced yet — refresh residential or data center proxies.</div>`;
    return;
  }
  list.innerHTML = rows
    .map((r) => {
      const use = r.inUse
        ? `in use${r.assignedProfileNo ? ` · BC ${r.assignedProfileNo}` : ""}`
        : r.valid === false
          ? "invalid / burned"
          : "free";
      return `<div class="bc-settings-list-row">
        <div>
          <div>${escapeHtml(r.host || "")}:${escapeHtml(String(r.port || ""))} · ${escapeHtml(r.countryCode || "")}${r.cityName ? ` · ${escapeHtml(r.cityName)}` : ""}</div>
          <div class="bc-settings-list-meta">${escapeHtml(r.tier || "")} · ${escapeHtml(use)} · id ${escapeHtml(r.id || "")}</div>
        </div>
      </div>`;
    })
    .join("");
}

async function loadBcProxies({ force = false } = {}) {
  if (!force && window.__bcProxiesCache) {
    applyBcProxyState(window.__bcProxiesCache);
    return window.__bcProxiesCache;
  }
  try {
    const data = await api("/api/provision/proxies");
    window.__bcProxiesCache = data;
    applyBcProxyState(data);
    return data;
  } catch (err) {
    const stats = $("bcProxyStats");
    if (stats) stats.textContent = err.message || String(err);
    throw err;
  }
}

async function refreshBcAddressesFromApi() {
  const data = await api("/api/provision/finance/addresses?geo=SE");
  applyBcAddressState(data);
  return data;
}

async function loadBcFinance({ force = false } = {}) {
  const vatSel = $("bcVatId");
  if (!vatSel) return null;

  const cached = readBcFinanceCache();
  const age = cached ? Date.now() - (cached.savedAt || 0) : Infinity;
  const fresh = age < BC_FINANCE_TTL_MS;

  // Paint from cache immediately so dropdowns aren't blank.
  if (cached && cached.data) {
    applyBcFinanceState(cached.data);
    setBcFinanceLoading(false);
  }

  if (!force && fresh && cached && cached.data) {
    loadBcProxies().catch(() => {});
    return cached.data;
  }

  if (_bcFinanceInflight) return _bcFinanceInflight;

  const hadCache = !!(cached && cached.data);
  if (!hadCache) {
    setBcFinanceLoading(true, "Loading Business Center settings…");
  } else {
    // Soft refresh — keep form usable while we refresh in the background.
    setBcFinanceLoading(true, "Updating…", { soft: true });
  }

  _bcFinanceInflight = (async () => {
    try {
      const s = await api("/api/provision/finance");
      writeBcFinanceCache(s);
      applyBcFinanceState(s);
      loadBcProxies({ force: true }).catch(() => {});
      return s;
    } catch (err) {
      if (!hadCache) {
        const hint = $("bcHostHint");
        if (hint) hint.textContent = "Could not load settings: " + (err.message || err);
      }
      throw err;
    } finally {
      setBcFinanceLoading(false);
      _bcFinanceInflight = null;
    }
  })();
  return _bcFinanceInflight;
}

async function saveBcFinance() {
  const hostId =
    $("bcHostId") && $("bcHostId").value !== BC_CREATE_OPTION ? $("bcHostId").value : "";
  if (hostId) {
    await api("/api/provision/hosts", {
      method: "PUT",
      body: JSON.stringify({ activeHostId: hostId }),
    });
  }
  const body = {
    vatId: $("bcVatId") && $("bcVatId").value !== BC_CREATE_OPTION ? $("bcVatId").value : undefined,
    sparkCodeId:
      $("bcSparkCodeId") && $("bcSparkCodeId").value !== BC_CREATE_OPTION
        ? $("bcSparkCodeId").value
        : undefined,
    cardId:
      $("bcCardId") && $("bcCardId").value !== BC_CREATE_OPTION ? $("bcCardId").value : undefined,
    addBalanceAmount: $("bcAddAmount") ? Number($("bcAddAmount").value) : undefined,
    mercuryDailyLimit: $("bcDailyLimit") ? Number($("bcDailyLimit").value) : undefined,
    extraAdAccounts: $("bcExtraAdAccounts")
      ? Math.max(0, Math.min(20, (Number($("bcExtraAdAccounts").value) || 1) - 1))
      : undefined,
    businessCentersPerProfile: 1,
    adAccountBudgetUsd: $("bcAdBudget") ? Number($("bcAdBudget").value) : undefined,
    salesConversionBidUsd: $("bcSalesBid") ? Number($("bcSalesBid").value) : undefined,
    salesDailyBudgetUsd: $("bcSalesBudget") ? Number($("bcSalesBudget").value) : undefined,
    salesLandingUrl: $("bcSalesLanding") ? String($("bcSalesLanding").value || "").trim() : undefined,
    salesInstantPageCta: $("bcSalesInstantCta")
      ? String($("bcSalesInstantCta").value || "").trim()
      : undefined,
    salesCreativeCta: $("bcSalesCreativeCta")
      ? String($("bcSalesCreativeCta").value || "").trim()
      : undefined,
    salesDisplayCard: $("bcSalesDisplayCard")
      ? Boolean($("bcSalesDisplayCard").checked)
      : undefined,
    salesAdNameTemplate: $("bcSalesAdName")
      ? String($("bcSalesAdName").value || "").trim()
      : undefined,
    salesCreativeText: $("bcSalesCreativeText")
      ? String($("bcSalesCreativeText").value || "").trim()
      : undefined,
  };
  setBcFinanceLoading(true, "Saving…");
  try {
    const s = await api("/api/provision/finance", { method: "PUT", body: JSON.stringify(body) });
    writeBcFinanceCache(s);
    applyBcFinanceState(s);
    return s;
  } finally {
    setBcFinanceLoading(false);
  }
}


function wireCreateSelect(selId, stateKey, openModal) {
  const sel = $(selId);
  if (!sel || sel.dataset.createWired) return;
  sel.dataset.createWired = "1";
  sel.addEventListener("change", () => {
    if (sel.value === BC_CREATE_OPTION) {
      sel.value = bcFinanceState[stateKey] || "";
      openModal();
      return;
    }
    bcFinanceState[stateKey] = sel.value;
    if (selId === "bcHostId") syncBcHostActionButtons();
  });
}

function openBcHostCreateModal({ editId = "" } = {}) {
  if ($("bcHostEditId")) $("bcHostEditId").value = editId || "";
  if ($("bcHostCreateTitle")) {
    $("bcHostCreateTitle").textContent = editId ? "Edit AdsPower host" : "Create AdsPower host";
  }
  if ($("bcHostCreateMsg")) $("bcHostCreateMsg").textContent = "";
  if ($("bcHostCreateKey")) $("bcHostCreateKey").value = "";
  if (editId) {
    const host = (bcFinanceState.hosts || []).find((h) => h.id === editId) || {};
    if ($("bcHostCreateName")) $("bcHostCreateName").value = host.name || "";
    if ($("bcHostCreateUrl")) {
      $("bcHostCreateUrl").value = host.adspowerUrl || "YOUR_VALUE";
    }
  } else {
    if ($("bcHostCreateName")) $("bcHostCreateName").value = "";
    if ($("bcHostCreateUrl")) $("bcHostCreateUrl").value = "YOUR_VALUE";
  }
  setFinanceModalOpen("bcHostCreateModal", true);
  $("bcHostCreateName")?.focus();
}

function initBcFinanceCreateModals() {
  const closeVat = () => setFinanceModalOpen("bcVatCreateModal", false);
  const closeSpark = () => setFinanceModalOpen("bcSparkCreateModal", false);
  const closeCard = () => setFinanceModalOpen("bcCardCreateModal", false);
  const closeHost = () => {
    setFinanceModalOpen("bcHostCreateModal", false);
    if ($("bcHostEditId")) $("bcHostEditId").value = "";
  };

  wireCreateSelect("bcHostId", "hostId", () => openBcHostCreateModal());
  wireCreateSelect("bcVatId", "vatId", () => {
    if ($("bcVatCreateNumber")) $("bcVatCreateNumber").value = "";
    if ($("bcVatCreateLabel")) $("bcVatCreateLabel").value = "";
    if ($("bcVatCreateMsg")) $("bcVatCreateMsg").textContent = "";
    setFinanceModalOpen("bcVatCreateModal", true);
    $("bcVatCreateNumber")?.focus();
  });
  wireCreateSelect("bcSparkCodeId", "sparkCodeId", () => {
    if ($("bcSparkCreateCode")) $("bcSparkCreateCode").value = "";
    if ($("bcSparkCreateMsg")) $("bcSparkCreateMsg").textContent = "";
    setFinanceModalOpen("bcSparkCreateModal", true);
    $("bcSparkCreateCode")?.focus();
  });
  wireCreateSelect("bcCardId", "cardId", () => {
    if ($("bcCardCreateNumber")) $("bcCardCreateNumber").value = "";
    if ($("bcCardCreateExp")) $("bcCardCreateExp").value = "";
    if ($("bcCardCreateCvv")) $("bcCardCreateCvv").value = "";
    if ($("bcCardCreateName")) $("bcCardCreateName").value = "";
    if ($("bcCardCreateMsg")) $("bcCardCreateMsg").textContent = "";
    setFinanceModalOpen("bcCardCreateModal", true);
    $("bcCardCreateNumber")?.focus();
  });

  ["bcVatCreateClose", "bcVatCreateCancel", "bcVatCreateBackdrop"].forEach((id) => {
    $(id)?.addEventListener("click", closeVat);
  });
  ["bcSparkCreateClose", "bcSparkCreateCancel", "bcSparkCreateBackdrop"].forEach((id) => {
    $(id)?.addEventListener("click", closeSpark);
  });
  ["bcCardCreateClose", "bcCardCreateCancel", "bcCardCreateBackdrop"].forEach((id) => {
    $(id)?.addEventListener("click", closeCard);
  });
  ["bcHostCreateClose", "bcHostCreateCancel", "bcHostCreateBackdrop"].forEach((id) => {
    $(id)?.addEventListener("click", closeHost);
  });

  $("bcHostEditBtn")?.addEventListener("click", () => {
    const hostId =
      $("bcHostId") && $("bcHostId").value !== BC_CREATE_OPTION ? $("bcHostId").value : "";
    if (!hostId) return;
    openBcHostCreateModal({ editId: hostId });
  });

  $("bcHostDeleteBtn")?.addEventListener("click", async () => {
    const hostId =
      $("bcHostId") && $("bcHostId").value !== BC_CREATE_OPTION ? $("bcHostId").value : "";
    if (!hostId) return;
    const host = (bcFinanceState.hosts || []).find((h) => h.id === hostId);
    const label = (host && (host.name || host.id)) || hostId;
    if (!confirm(`Delete AdsPower host “${label}”?`)) return;
    try {
      await api(`/api/provision/hosts/${encodeURIComponent(hostId)}`, {
        method: "DELETE",
      });
      const s = await api("/api/provision/finance");
      writeBcFinanceCache(s);
      applyBcFinanceState(s);
    } catch (err) {
      alert(err.message || String(err));
    }
  });

  $("bcHostCreateSave")?.addEventListener("click", async () => {
    const msg = $("bcHostCreateMsg");
    try {
      const editId = ($("bcHostEditId")?.value || "").trim();
      const name = ($("bcHostCreateName")?.value || "").trim();
      const adspowerUrl = ($("bcHostCreateUrl")?.value || "").trim();
      const apiKey = ($("bcHostCreateKey")?.value || "").trim();
      if (!name) throw new Error("Enter a host name");
      if (editId) {
        const body = { name, adspowerUrl };
        if (apiKey) body.apiKey = apiKey;
        await api(`/api/provision/hosts/${encodeURIComponent(editId)}`, {
          method: "PATCH",
          body: JSON.stringify(body),
        });
      } else {
        if (!apiKey) throw new Error("Enter the AdsPower API key");
        await api("/api/provision/hosts", {
          method: "POST",
          body: JSON.stringify({ name, adspowerUrl, apiKey, select: true }),
        });
      }
      const s = await api("/api/provision/finance");
      writeBcFinanceCache(s);
      applyBcFinanceState(s);
      closeHost();
    } catch (err) {
      if (msg) msg.textContent = err.message || String(err);
      else alert(err.message || String(err));
    }
  });

  $("bcVatCreateSave")?.addEventListener("click", async () => {
    const msg = $("bcVatCreateMsg");
    try {
      const number = ($("bcVatCreateNumber")?.value || "").trim();
      if (!number) throw new Error("Enter a VAT number");
      const s = await api("/api/provision/finance/vats", {
        method: "POST",
        body: JSON.stringify({
          number,
          label: ($("bcVatCreateLabel")?.value || "").trim(),
          geo: "SE",
        }),
      });
      writeBcFinanceCache(s);
      applyBcFinanceState(s);
      closeVat();
    } catch (err) {
      if (msg) msg.textContent = err.message || String(err);
      else alert(err.message || String(err));
    }
  });

  $("bcSparkCreateSave")?.addEventListener("click", async () => {
    const msg = $("bcSparkCreateMsg");
    try {
      const code = ($("bcSparkCreateCode")?.value || "").trim();
      if (!code) throw new Error("Enter a Spark code");
      const s = await api("/api/provision/finance/spark-codes", {
        method: "POST",
        body: JSON.stringify({ code }),
      });
      writeBcFinanceCache(s);
      applyBcFinanceState(s);
      closeSpark();
    } catch (err) {
      if (msg) msg.textContent = err.message || String(err);
      else alert(err.message || String(err));
    }
  });

  $("bcCardCreateSave")?.addEventListener("click", async () => {
    const msg = $("bcCardCreateMsg");
    try {
      const cardNumber = ($("bcCardCreateNumber")?.value || "").trim();
      const cardExp = ($("bcCardCreateExp")?.value || "").trim();
      const cardCvv = ($("bcCardCreateCvv")?.value || "").trim();
      const cardName = ($("bcCardCreateName")?.value || "").trim();
      if (!cardNumber || !cardExp || !cardCvv || !cardName) {
        throw new Error("Enter card number, expiration, CVV, and cardholder name");
      }
      const s = await api("/api/provision/finance/cards", {
        method: "POST",
        body: JSON.stringify({ cardNumber, cardExp, cardCvv, cardName }),
      });
      writeBcFinanceCache(s);
      applyBcFinanceState(s);
      closeCard();
    } catch (err) {
      if (msg) msg.textContent = err.message || String(err);
      else alert(err.message || String(err));
    }
  });

  const closeAddress = () => setFinanceModalOpen("bcAddressCreateModal", false);
  ["bcAddressCreateClose", "bcAddressCreateCancel", "bcAddressCreateBackdrop"].forEach((id) => {
    $(id)?.addEventListener("click", closeAddress);
  });
  $("bcAddressAddBtn")?.addEventListener("click", () => {
    if ($("bcAddressCreateStreet")) $("bcAddressCreateStreet").value = "";
    if ($("bcAddressCreatePostal")) $("bcAddressCreatePostal").value = "";
    if ($("bcAddressCreateLabel")) $("bcAddressCreateLabel").value = "";
    if ($("bcAddressCreatePin")) $("bcAddressCreatePin").checked = false;
    if ($("bcAddressCreateMsg")) $("bcAddressCreateMsg").textContent = "";
    setFinanceModalOpen("bcAddressCreateModal", true);
    $("bcAddressCreateStreet")?.focus();
  });
  $("bcAddressCreateSave")?.addEventListener("click", async () => {
    const msg = $("bcAddressCreateMsg");
    try {
      const street = ($("bcAddressCreateStreet")?.value || "").trim();
      const postalCode = ($("bcAddressCreatePostal")?.value || "").trim();
      if (!street || !postalCode) throw new Error("Enter street and postal code");
      const data = await api("/api/provision/finance/addresses", {
        method: "POST",
        body: JSON.stringify({
          street,
          postalCode,
          geo: "SE",
          label: ($("bcAddressCreateLabel")?.value || "").trim(),
          pin: !!$("bcAddressCreatePin")?.checked,
        }),
      });
      applyBcAddressState(data);
      // Keep finance cache in sync for next paint.
      const cached = readBcFinanceCache();
      if (cached && cached.data) {
        cached.data.billingAddresses = data;
        writeBcFinanceCache(cached.data);
      }
      closeAddress();
    } catch (err) {
      if (msg) msg.textContent = err.message || String(err);
      else alert(err.message || String(err));
    }
  });
  $("bcAddressUnpinBtn")?.addEventListener("click", async () => {
    try {
      const data = await api("/api/provision/finance/addresses/pin?geo=SE", {
        method: "DELETE",
      });
      applyBcAddressState(data);
    } catch (err) {
      alert(err.message || String(err));
    }
  });
  $("bcAddressResetBtn")?.addEventListener("click", async () => {
    try {
      const data = await api("/api/provision/finance/addresses/reset-cursor?geo=SE", {
        method: "POST",
      });
      applyBcAddressState(data);
    } catch (err) {
      alert(err.message || String(err));
    }
  });
  $("bcAddressList")?.addEventListener("click", async (ev) => {
    const btn = ev.target && ev.target.closest ? ev.target.closest("[data-pin-address]") : null;
    if (!btn) return;
    const row = btn.closest(".bc-settings-list-row");
    if (!row) return;
    const street = row.getAttribute("data-street") || "";
    const postal = row.getAttribute("data-postal") || "";
    const already = btn.getAttribute("data-pin-address") === "1";
    try {
      if (already) {
        const data = await api("/api/provision/finance/addresses/pin?geo=SE", {
          method: "DELETE",
        });
        applyBcAddressState(data);
      } else {
        const data = await api("/api/provision/finance/addresses/pin", {
          method: "POST",
          body: JSON.stringify({ street, postalCode: postal, geo: "SE" }),
        });
        applyBcAddressState(data);
      }
    } catch (err) {
      alert(err.message || String(err));
    }
  });

  async function refreshBcProxies(which, btn) {
    if (btn) btn.disabled = true;
    try {
      const data = await api("/api/provision/proxies/refresh", {
        method: "POST",
        body: JSON.stringify({ geo: "SE", which, requestIpRefresh: false }),
      });
      window.__bcProxiesCache = data;
      applyBcProxyState(data);
    } catch (err) {
      alert(err.message || String(err));
    } finally {
      if (btn) btn.disabled = false;
    }
  }
  $("bcProxyRefreshResidentialBtn")?.addEventListener("click", async () => {
    await refreshBcProxies("residential", $("bcProxyRefreshResidentialBtn"));
  });
  $("bcProxyRefreshDatacenterBtn")?.addEventListener("click", async () => {
    await refreshBcProxies("datacenter", $("bcProxyRefreshDatacenterBtn"));
  });
  $("bcProxyIpRefreshBtn")?.addEventListener("click", async () => {
    if (
      !confirm(
        "Spend one Webshare on-demand refresh to rotate residential IPs, then re-sync the residential list?"
      )
    ) {
      return;
    }
    const btn = $("bcProxyIpRefreshBtn");
    if (btn) btn.disabled = true;
    try {
      const data = await api("/api/provision/proxies/refresh", {
        method: "POST",
        body: JSON.stringify({
          geo: "SE",
          which: "residential",
          requestIpRefresh: true,
        }),
      });
      window.__bcProxiesCache = data;
      applyBcProxyState(data);
    } catch (err) {
      alert(err.message || String(err));
    } finally {
      if (btn) btn.disabled = false;
    }
  });
}

function initBcFinance() {
  initBcFinanceCreateModals();
  if ($("bcSaveFinance")) {
    $("bcSaveFinance").addEventListener("click", async () => {
      try {
        await saveBcFinance();
      } catch (err) {
        alert(err.message || String(err));
      }
    });
  }
  // Paint from localStorage immediately if Settings → BC is already visible.
  const cached = readBcFinanceCache();
  if (cached && cached.data) applyBcFinanceState(cached.data);
}

function initSettingsPage() {
  initSettingsNav();
  initBcFinance();
  $("ytCookiesUploadBtn").addEventListener("click", () => $("ytCookiesFile").click());
  $("ytCookiesFile").addEventListener("change", async () => {
    const file = $("ytCookiesFile").files[0];
    $("ytCookiesFile").value = "";
    if (!file) return;
    try {
      const fd = new FormData();
      fd.append("file", file);
      await api("/api/settings/ytdlp-cookies", { method: "POST", body: fd });
      if (config) config.hasYtdlpCookies = true;
      setYtCookiesStatus();
    } catch (err) {
      alert("Upload failed: " + err.message);
    }
  });
  $("ytCookiesRemoveBtn").addEventListener("click", async () => {
    try {
      await api("/api/settings/ytdlp-cookies", { method: "DELETE" });
      if (config) config.hasYtdlpCookies = false;
      setYtCookiesStatus();
    } catch (err) {
      alert(err.message || String(err));
    }
  });
}

// ---------------------------------------------------------------------------
// Boot
// ---------------------------------------------------------------------------

async function boot() {
  const runInit = (name, fn) => {
    try {
      fn();
    } catch (err) {
      console.error(name + " failed", err);
    }
  };
  runInit("initTheme", initTheme);
  runInit("initNav", initNav);
  runInit("initVisualViewport", initVisualViewport);
  runInit("initCreateRacks", initCreateRacks);
  runInit("initDropZone", initDropZone);
  runInit("initUrls", initUrls);
  runInit("initPinterestSearch", initPinterestSearch);
  runInit("initSourceListActions", initSourceListActions);
  runInit("initVideoLibrary", initVideoLibrary);
  runInit("initCaptions", initCaptions);
  runInit("initEmojiPicker", initEmojiPicker);
  runInit("initVideoDetail", initVideoDetail);
  runInit("initStyleControls", initStyleControls);
  runInit("initPreview", initPreview);
  runInit("initBatch", initBatch);
  runInit("initTikTok", initTikTok);
  runInit("initFacebook", initFacebook);
  runInit("initOutputs", initOutputs);
  runInit("initSettingsPage", initSettingsPage);
  runInit("initEndCapTemplate", initEndCapTemplate);
  if (typeof window.initHooks === "function") runInit("initHooks", () => window.initHooks());
  // Wire chat/BC UI only — network waits until those pages are shown.
  if (typeof window.initChat === "function") {
    runInit("initChat", () => window.initChat({ prefetchModels: false }));
  }
  if (typeof window.initBcPage === "function") {
    runInit("initBcPage", () => window.initBcPage({ prefetch: false, watch: false }));
  }
  bindPersist(["urlInput"]);

  // Wire workspace save on caption/batch/style changes
  ["captionsInput", "preserveBreaks", "forceLower", "mode", "nameTemplate", "skipExisting", "dedupe"].forEach((id) => {
    const el = $(id);
    if (!el) return;
    el.addEventListener("input", scheduleWorkspaceSave);
    el.addEventListener("change", scheduleWorkspaceSave);
  });
  window.addEventListener("captionToolStyleChanged", scheduleWorkspaceSave);

  syncSlidersFromStyle();

  const page = currentPage();
  // initNav may have shown #/bcs before loadBcPage existed — load now.
  if (page === "bcs" && typeof window.loadBcPage === "function") {
    window.loadBcPage().catch(() => {});
  }

  // Parallel first paint: config + workspace + sources (+ create media when on Create).
  const configP = api("/api/config").then((c) => {
    config = c;
    window.config = c;
    return c;
  }).catch((err) => {
    if ($("ffmpegWarning")) {
      $("ffmpegWarning").textContent = "Cannot reach the server: " + (err.message || err);
      $("ffmpegWarning").classList.remove("hidden");
    }
    return null;
  });
  const workspaceP = loadWorkspaceRemote();
  const sourcesP = api("/api/sources").then(async (loaded) => {
    sources = loaded;
    // Non-blocking restore — don't serialize boot on crop/trim PUTs.
    syncCropsWithLocal(sources).catch(() => {});
    syncTrimsWithLocal(sources).catch(() => {});
    renderSources();
    updatePreviewSelectors();
    return loaded;
  }).catch(() => []);

  const createMediaP = page === "create"
    ? Promise.all([
        loadLibraryCache().catch(() => []),
        (_imagesInflight || (_imagesInflight = api("/api/images")
          .then((res) => {
            imageLibraryCache = (res && res.items) || [];
            return imageLibraryCache;
          })
          .finally(() => { _imagesInflight = null; }))).catch(() => []),
      ]).then(() => {
        paintRecentMediaStrip();
      })
    : Promise.resolve();

  await Promise.all([configP, workspaceP, sourcesP, createMediaP]);

  if (config) {
    if (!config.ffmpeg || !config.ffprobe) $("ffmpegWarning")?.classList.remove("hidden");
    if ($("encoderBadge")) {
      $("encoderBadge").textContent = config.nvenc ? "NVENC (GPU)" : "libx264 (CPU)";
    }
    if ($("retentionLabel")) $("retentionLabel").textContent = String(config.retentionDays || 14);
    if ($("settingsInfo")) {
      $("settingsInfo").textContent =
        `Max upload ${config.maxUploadMb} MB per file · max ${config.maxBatchJobs} jobs per batch · ` +
        `up to 2 renders + 1 upload at a time (extras queue) · files auto-delete after ${config.retentionDays} days.`;
    }
    setYtCookiesStatus();
    renderAiProvidersStatus();
  }

  if (page === "create") {
    loadActiveWorkspacePlanName().catch(() => {});
    // Image grid only when carousel mode may need it — cheap if already cached.
    refreshImageLibrary().catch(() => {});
  }

  // Deferred: settings / TikTok / Facebook / BC finance — only when needed.
  if (page === "settings") {
    loadTtSettings().catch(() => {});
    loadTtAccounts().catch(() => {});
    loadFacebookStatus().catch(() => {});
  }
  // Never block boot on BC finance.

  try {
    wireActiveJobsDock();
    resumeActiveJobs().catch(() => {});
  } catch {}

  updateCount();
  saveUiState();
  syncCreateSparksUploadOption().catch(() => {});
  window.dispatchEvent(new CustomEvent("captionToolReady"));
}

function startBoot() {
  // Warm BC inventory in parallel with the rest of boot (auth already kicked
  // /healthz). Safe no-op if bc.js has not registered the helper yet.
  try {
    if (typeof window.prefetchBcInventory === "function") {
      window.prefetchBcInventory();
    }
  } catch {
    /* ignore */
  }
  boot().catch((err) => console.error("boot failed", err));
}

// AuthReady can resolve during app.js (cached Google session). A `.then`
// microtask would run boot *before* remaining defer scripts register
// window.init* — New chat, Recents, and BC clicks stay dead. Wait until
// every defer script has run (DOMContentLoaded), then one macrotask.
window.AuthReady.then(() => {
  const go = () => setTimeout(startBoot, 0);
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", go, { once: true });
  } else {
    go();
  }
});

// Expose helpers for chat.js
window.api = api;
window.authHeaders = authHeaders;
window.withAuth = withAuth;
window.setAuthImage = setAuthImage;
window.$ = $;
window.loadWorkspaceRemote = loadWorkspaceRemote;
window.scheduleWorkspaceSave = scheduleWorkspaceSave;
window.cancelWorkspaceSave = cancelWorkspaceSave;
window.saveWorkspaceRemote = saveWorkspaceRemote;
window.getSources = () => sources;
window.refreshImageLibrary = refreshImageLibrary;
window.getImageLibraryCache = () => imageLibraryCache;
window.setImageLibraryCache = (items) => {
  imageLibraryCache = Array.isArray(items) ? items : [];
};
window.paintRecentMediaStrip = paintRecentMediaStrip;
window.refreshRecentMediaStrip = refreshRecentMediaStrip;
window.renderPinterestResults = renderPinterestResults;
window.bindPinterestSearchPanel = bindPinterestSearchPanel;
window.bindPinterestHover = bindPinterestHover;
window.playPinterestPreview = playPinterestPreview;
window.stopPinterestPreview = stopPinterestPreview;
window.downloadPinUrls = downloadPinUrls;
window.getWorkspaceRenderPairs = () => workspaceRenderPairs;
window.setWorkspaceRenderPairs = (pairs, opts = {}) => {
  workspaceRenderPairs = Array.isArray(pairs) ? pairs : [];
  if (opts.clearOnSave) clearRenderPairsOnSave = true;
  else if (workspaceRenderPairs.length) clearRenderPairsOnSave = false;
  syncCustomModeOption();
  if (typeof updateCount === "function") updateCount();
};
window.setSources = (s) => {
  sources = s;
  if (clearStaleWorkspaceRenderPairs()) scheduleWorkspaceSave();
};
window.renderSources = renderSources;
window.showChatLibraryTab = showChatLibraryTab;
window.fmtDuration = fmtDuration;
window.fmtEtaLeft = fmtEtaLeft;
window.estimateUploadSeconds = estimateUploadSeconds;
window.refreshLibraryGrids = refreshLibraryGrids;
window.upsertSource = upsertSource;
window.captionList = captionList;
window.settingsPayload = settingsPayload;
window.refreshOverlay = refreshOverlay;
window.updatePreviewSelectors = updatePreviewSelectors;
window.loadOutputs = loadOutputs;
window.reloadJobSources = reloadJobSources;
window.navigateAppPage = (page) => {
  window.location.hash = "#/" + (page || "create");
};
window.openTtUploadSheet = openTtUploadSheet;
window.startWatchingUpload = startWatchingUpload;
window.updateTtSheetSparkResults = updateTtSheetSparkResults;
window.showTtUploadResultsSheet = showTtUploadResultsSheet;
window.escapeHtml = escapeHtml;

async function cancelCurrentTikTokUpload() {
  const id = focusedUploadId || currentUploadId;
  if (!id) return;
  try {
    await api(`/api/tiktok/upload/${id}/cancel`, { method: "POST" });
  } catch (err) {
    console.error(`Failed to cancel TikTok upload ${id}:`, err);
    showToast("Failed to cancel upload", { error: true });
  }
}

function uploadItemStatusLabel(i) {
  if (i.skipReason === "already_uploaded") return "uploaded";
  if (i.status === "skipped" && /already on tiktok/i.test(String(i.error || ""))) {
    return "uploaded";
  }
  return i.status || "pending";
}

async function continueTikTokUpload(batchId) {
  const bid = batchId || lastUploadBatchId || currentBatchId || ttSheetContext?.batchId;
  if (!bid) {
    alert("No batch to continue — re-open upload from the batch that was interrupted.");
    return;
  }
  try {
    const res = await api("/api/tiktok/upload/continue", {
      method: "POST",
      body: JSON.stringify({ batchId: bid }),
    });
    const id = res.uploadId || res.id;
    rememberUploadJob(id, res.sourceBatchId || bid);
    currentUploadId = id;
    setTtSheetMode("progress");
    startWatchingUpload();
  } catch (err) {
    alert(err.message || String(err));
  }
}

function updateTtUploadActionButtons(st) {
  const running = st && (st.status === "running" || st.status === "queued");
  const canContinue = !!(st && st.canContinue && st.sourceBatchId && !running);
  if (st && st.sourceBatchId) lastUploadBatchId = st.sourceBatchId;
  const continueIds = [
    "continueTikTokUpload",
    "continueTikTokUploadStandalone",
    "ttSheetContinue",
  ];
  // Dock Cancel is owned by syncDockCancelButtons (selected job only).
  const cancelIds = ["cancelTikTokUploadStandalone", "ttSheetCancelUpload"];
  for (const id of continueIds) {
    if ($(id)) $(id).classList.toggle("hidden", !canContinue);
  }
  for (const id of cancelIds) {
    if ($(id)) $(id).classList.toggle("hidden", !running);
  }
  syncDockCancelButtons();
}

function startWatchingUpload(uploadId) {
  const id = uploadId || focusedUploadId || currentUploadId;
  if (id) {
    focusedUploadId = id;
    currentUploadId = id;
    rememberUploadJob(id);
  }
  showProgressDock();
  if ($("tiktokUploadPanel")) $("tiktokUploadPanel").classList.remove("hidden");
  if ($("renderProgressBlock") && dockFocusKind === "upload") $("renderProgressBlock").classList.add("hidden");
  if ($("facebookAdsPanel") && dockFocusKind === "upload") $("facebookAdsPanel").classList.add("hidden");
  if ($("tiktokUploadPanelStandalone")) $("tiktokUploadPanelStandalone").classList.remove("hidden");
  if ($("cancelTikTokUploadStandalone")) $("cancelTikTokUploadStandalone").classList.remove("hidden");
  if ($("uploadToTikTok")) $("uploadToTikTok").disabled = false;
  if ($("dockDismiss")) $("dockDismiss").classList.add("hidden");
  syncDockCancelButtons();
  pollUpload(id);
}

function renderUploadState(st) {
  if (!st || !st.id) return;
  jobSnapshots[st.id] = st;
  refreshActiveJobsDock();

  const uploaded = st.uploaded ?? (st.items || []).filter((i) => i.status === "done").length;
  const skippedAlready = st.skippedAlready ?? (st.items || []).filter(
    (i) => i.skipReason === "already_uploaded"
  ).length;
  const failed = st.failed ?? (st.items || []).filter((i) => i.status === "failed").length;
  const total = st.total || (st.items || []).length;

  let summary = `${uploaded} uploaded`;
  if (st.status === "queued") {
    summary = `Queued #${st.queuePosition || "?"}`;
  }
  if (skippedAlready) summary += ` · ${skippedAlready} already on TikTok`;
  if (failed) summary += ` · ${failed} failed`;
  summary += ` · ${total} total`;
  if (st.uploadEngine) summary += ` · ${st.uploadEngine}`;
  if (st.message) summary += ` — ${st.message}`;
  if (st.current) {
    summary += ` — ${st.current.status} ${st.current.filename || ""}`;
    if (st.current.accountName) summary += ` → ${st.current.accountName}`;
  }
  if (st.status === "running") {
    const eta = fmtEtaLeft(st.etaSeconds);
    if (eta) summary += ` · ${eta}`;
  }
  if (st.status === "cancelled") summary += " — cancelled";
  if (st.status === "done") summary += " — done";

  const doneCount = uploaded + skippedAlready + failed;
  const frac = doneCount / Math.max(total, 1);
  const listHtml = (st.items || []).map((i) => {
    const label = uploadItemStatusLabel(i);
    return `
    <li>
      <span class="st ${label}">${label}</span>
      <span class="cap" title="${escapeHtml(i.description || i.caption || "")}">
        ${escapeHtml(i.filename || "")}${i.accountName ? " → " + escapeHtml(i.accountName) : ""}
      </span>
      ${i.error && label !== "uploaded" ? `<span class="err-msg" title="${escapeHtml(String(i.error))}">${escapeHtml(String(i.error).slice(0, 900))}</span>` : ""}
    </li>
  `;
  }).join("");

  const failedWithErr = (st.items || []).find((i) => i.status === "failed" && i.error);
  let summaryFull = summary;
  if (failedWithErr && st.status !== "running") {
    summaryFull += ` — error: ${failedWithErr.error}`;
  }

  if ($("tiktokUploadPanel")) {
    showProgressDock();
    if ($("tiktokUploadSummary")) $("tiktokUploadSummary").textContent = summaryFull;
    if ($("tiktokUploadBar")) $("tiktokUploadBar").style.width = `${Math.min(frac * 100, 100)}%`;
    if ($("tiktokUploadList")) $("tiktokUploadList").innerHTML = listHtml;
  }
  if ($("tiktokUploadPanelStandalone")) {
    $("tiktokUploadPanelStandalone").classList.remove("hidden");
    if ($("tiktokUploadSummaryStandalone")) $("tiktokUploadSummaryStandalone").textContent = summaryFull;
    if ($("tiktokUploadBarStandalone")) $("tiktokUploadBarStandalone").style.width = `${Math.min(frac * 100, 100)}%`;
    if ($("tiktokUploadListStandalone")) $("tiktokUploadListStandalone").innerHTML = listHtml;
  }

  updateTtUploadActionButtons(st);
  if ($("ttSheetHide")) {
    $("ttSheetHide").classList.toggle(
      "hidden",
      st.status === "running" || st.status === "queued" || st.status === "done" || st.status === "cancelled"
    );
  }
  if ($("uploadToTikTok")) $("uploadToTikTok").disabled = false;
  if (st.status === "done" || st.status === "cancelled") {
    if ($("dockDismiss")) $("dockDismiss").classList.remove("hidden");
    maybeAutoExpandDock(st.id, st.status);
  }
  renderDockStrip(st);
  const sheet = $("ttUploadSheet");
  const sheetOpen = sheet && !sheet.classList.contains("hidden");
  const boundHere = ttSheetBoundUploadId && st.id === ttSheetBoundUploadId;
  const wantCompleteUi =
    boundHere ||
    !!(ttSheetContext && (ttSheetContext.fetchSparks || ttSheetContext.fromCreatePlan));

  if (st.status === "running" || st.status === "queued" || !!st.canContinue) {
    // Only drive sheet progress when it is bound to this job — never overwrite
    // a setup sheet the user opened to start/queue another upload.
    if (sheetOpen && boundHere) {
      setTtSheetMode("progress");
      if ($("ttSheetTitle")) {
        $("ttSheetTitle").textContent =
          st.status === "running"
            ? "Uploading to TikTok"
            : st.status === "queued"
              ? "Upload queued"
              : st.canContinue
                ? "Upload paused"
                : "Uploading to TikTok";
      }
      renderTtSheetProgress(st);
    }
  } else if (st.status === "done" || st.status === "cancelled") {
    if (wantCompleteUi && !ttSheetResultsDismissed.has(st.id)) {
      const already = ttSheetCompleteNotified.has(st.id);
      showTtUploadResultsSheet(st);
      if (!already) {
        ttSheetCompleteNotified.add(st.id);
        const uploaded =
          st.uploaded ??
          (st.items || []).filter((i) => uploadItemStatusLabel(i) === "uploaded" || i.status === "done")
            .length;
        const failed = st.failed ?? (st.items || []).filter((i) => i.status === "failed").length;
        if (typeof showToast === "function") {
          if (st.status === "cancelled") {
            showToast(`Upload cancelled (${uploaded} posted)`, { error: !uploaded });
          } else if (failed && !uploaded) {
            showToast("Upload failed", { error: true });
          } else if (failed) {
            showToast(`Upload finished with ${failed} failure${failed === 1 ? "" : "s"}`);
          } else {
            showToast(`Upload complete — ${uploaded} posted`);
          }
        }
        const ctx = ttSheetContext || {};
        if (typeof window.onTtUploadComplete === "function") {
          try {
            window.onTtUploadComplete(st, ctx);
          } catch (err) {
            if (typeof showToast === "function") {
              showToast(err.message || String(err), { error: true });
            }
          }
        }
      }
    }
  }

  if (st.incidentToShow) {
    maybeShowTtIncident(st.incidentToShow);
  }
}

let _ttIncidentAcked = new Set();
try {
  const raw = sessionStorage.getItem("ttIncidentAcked");
  if (raw) _ttIncidentAcked = new Set(JSON.parse(raw));
} catch {}

function _persistTtIncidentAcked() {
  try {
    sessionStorage.setItem(
      "ttIncidentAcked",
      JSON.stringify([..._ttIncidentAcked].slice(-40))
    );
  } catch {}
}

function maybeShowTtIncident(inc) {
  if (!inc || !inc.id) return;
  if (_ttIncidentAcked.has(inc.id)) return;
  showTtIncidentModal(inc);
}

function showTtIncidentModal(inc) {
  const modal = $("ttUploadIncidentModal");
  if (!modal || !inc) return;
  modal.dataset.incidentId = inc.id || "";
  if ($("ttIncidentTitle")) $("ttIncidentTitle").textContent = inc.title || "Upload issue";
  if ($("ttIncidentMessage")) $("ttIncidentMessage").textContent = inc.message || "";
  if ($("ttIncidentFix")) $("ttIncidentFix").textContent = inc.how_to_fix || "";
  const tech = [
    `code: ${inc.code || ""}`,
    `engine: ${inc.engine || ""}`,
    `phase: ${inc.phase || ""}`,
    inc.http_status != null ? `http: ${inc.http_status}` : "",
    inc.account_name ? `account: ${inc.account_name}` : "",
    inc.video_filename ? `file: ${inc.video_filename}` : "",
    "",
    inc.response_snippet || "",
    "",
    inc.technical || "",
  ]
    .filter((x) => x !== undefined)
    .join("\n")
    .trim();
  if ($("ttIncidentTech")) $("ttIncidentTech").textContent = tech;
  const code = String(inc.code || "");
  const needAccounts = /AUTH_EXPIRED|COOKIE_/.test(code);
  const needStudio = /SIGNER_/.test(code);
  if ($("ttIncidentOpenAccounts")) {
    $("ttIncidentOpenAccounts").classList.toggle("hidden", !needAccounts);
  }
  if ($("ttIncidentSwitchStudio")) {
    $("ttIncidentSwitchStudio").classList.toggle("hidden", !needStudio);
  }
  modal.classList.remove("hidden");
}

async function ackTtIncident(incidentId) {
  if (!incidentId) return;
  _ttIncidentAcked.add(incidentId);
  _persistTtIncidentAcked();
  try {
    await api("/api/tiktok/incidents/ack", {
      method: "POST",
      body: JSON.stringify({ incidentId }),
    });
  } catch {}
  const modal = $("ttUploadIncidentModal");
  if (modal) modal.classList.add("hidden");
}

async function refreshTtIncidentBanner() {
  const banner = $("ttIncidentBanner");
  if (!banner) return;
  try {
    const res = await api("/api/tiktok/incidents?limit=1");
    const item = (res.items || [])[0];
    if (!item || item.severity === "info" || item.code === "OK") {
      if (($("ttUploadEngine") || {}).value !== "http") banner.classList.add("hidden");
      return;
    }
    const ageH = (Date.now() / 1000 - (item.at || 0)) / 3600;
    if (ageH > 24) {
      if (($("ttUploadEngine") || {}).value !== "http") banner.classList.add("hidden");
      return;
    }
    if (item.severity !== "error" && item.severity !== "critical" && item.severity !== "warning") {
      return;
    }
    banner.classList.remove("hidden");
    banner.innerHTML = `Last upload issue: <strong>${escapeHtml(item.title || item.code || "error")}</strong>
      <button type="button" class="btn ghost" id="ttBannerViewIncident">View</button>`;
    const btn = $("ttBannerViewIncident");
    if (btn) btn.onclick = () => showTtIncidentModal(item);
  } catch {}
}

function wireTtIncidentModal() {
  const hide = () => {
    const modal = $("ttUploadIncidentModal");
    const id = modal && modal.dataset.incidentId;
    if (id) ackTtIncident(id);
    else if (modal) modal.classList.add("hidden");
  };
  if ($("ttIncidentClose")) $("ttIncidentClose").onclick = hide;
  if ($("ttIncidentAck")) $("ttIncidentAck").onclick = hide;
  if ($("ttIncidentBackdrop")) $("ttIncidentBackdrop").onclick = hide;
  if ($("ttIncidentCopy")) {
    $("ttIncidentCopy").onclick = async () => {
      const t = ($("ttIncidentTech") && $("ttIncidentTech").textContent) || "";
      try {
        await navigator.clipboard.writeText(t);
      } catch {}
    };
  }
  if ($("ttIncidentOpenAccounts")) {
    $("ttIncidentOpenAccounts").onclick = () => {
      hide();
      const nav = document.querySelector('[data-settings="tiktok"]');
      if (nav) nav.click();
      const settingsNav = document.querySelector('[data-nav="settings"]');
      if (settingsNav) settingsNav.click();
    };
  }
  if ($("ttIncidentSwitchStudio")) {
    $("ttIncidentSwitchStudio").onclick = async () => {
      try {
        if ($("ttUploadEngine")) $("ttUploadEngine").value = "studio";
        await saveTtSettings({ uploadEngine: "studio" });
        applyTtEngineUi();
      } catch {}
      hide();
    };
  }
}

const _TT_SETTINGS_LOCK_IDS = [
  "ttUploadEngine",
  "ttCaptionTemplate",
  "ttHashtags",
  "ttDelay",
  "ttMinGap",
  "ttAutoUpload",
  "ttContinueOnError",
  "ttCopyrightChecks",
  "ttContentChecks",
  "ttSaveSettings",
  "ttTestUploadBtn",
];

let _ttTestUploadId = null;
let _ttTestPollTimer = null;

function setTtSettingsLocked(locked) {
  _TT_SETTINGS_LOCK_IDS.forEach((id) => {
    const el = $(id);
    if (!el) return;
    if (id === "ttTestUploadBtn") {
      el.disabled = !!locked;
      el.textContent = locked ? "Testing…" : "Test upload";
      return;
    }
    el.disabled = !!locked;
  });
  const pane = document.querySelector('[data-settings-pane="upload"]');
  if (pane) pane.classList.toggle("is-locked", !!locked);
}

function setTtTestStatus(msg, isError) {
  const el = $("ttTestUploadStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError && !!msg);
  el.classList.toggle("is-ok", !isError && !!msg);
}

function showTtTestResultSheet(tr, st) {
  const modal = $("ttTestResultModal");
  if (!modal || !tr) return;
  const ok = !!tr.ok;
  const running = tr.ok === null || tr.status === "running";
  if ($("ttTestResultTitle")) {
    $("ttTestResultTitle").textContent = running
      ? "Test upload running…"
      : ok
        ? "Test upload succeeded"
        : "Test upload failed";
  }
  const badge = $("ttTestResultBadge");
  if (badge) {
    badge.className = "tt-test-badge " + (running ? "is-run" : ok ? "is-ok" : "is-fail");
    badge.textContent = running ? "Running" : ok ? "Passed" : "Failed";
  }
  const meta = $("ttTestResultMeta");
  if (meta) {
    const rows = [
      ["Engine", tr.engine || "—"],
      ["Account", tr.accountName || "—"],
      ["Video", tr.filename || "—"],
      ["Title", tr.title || tr.caption || "—"],
      ["Description", tr.description || "—"],
      ["Elapsed", tr.elapsedMs != null ? `${Math.round(tr.elapsedMs / 100) / 10}s` : "—"],
      ["Upload id", tr.uploadId || (st && st.id) || "—"],
    ];
    meta.innerHTML = rows
      .map(([k, v]) => `<dt>${escapeHtml(k)}</dt><dd>${escapeHtml(String(v))}</dd>`)
      .join("");
  }
  const why = $("ttTestResultWhy");
  if (why) {
    if (!ok && !running) {
      const inc = tr.incident || {};
      const parts = [
        tr.error || "",
        inc.title ? `\n${inc.title}` : "",
        inc.message ? `\n${inc.message}` : "",
        inc.how_to_fix ? `\n\nHow to fix:\n${inc.how_to_fix}` : "",
        inc.code ? `\n\nCode: ${inc.code}` : "",
      ]
        .join("")
        .trim();
      why.textContent = parts || "Upload failed for an unknown reason.";
      why.classList.remove("hidden");
    } else {
      why.classList.add("hidden");
      why.textContent = "";
    }
  }
  const post = $("ttTestResultPost");
  const openBtn = $("ttTestResultOpenPost");
  if (tr.postUrl) {
    if (post) {
      post.classList.remove("hidden");
      post.innerHTML = `Posted: <a href="${escapeHtml(tr.postUrl)}" target="_blank" rel="noopener">${escapeHtml(tr.postUrl)}</a>`;
    }
    if (openBtn) {
      openBtn.href = tr.postUrl;
      openBtn.classList.remove("hidden");
    }
  } else {
    if (post) {
      post.classList.add("hidden");
      post.textContent = "";
    }
    if (openBtn) openBtn.classList.add("hidden");
  }
  modal.classList.remove("hidden");
}

function hideTtTestResultSheet() {
  const modal = $("ttTestResultModal");
  if (modal) modal.classList.add("hidden");
}

function stopTtTestPoll() {
  if (_ttTestPollTimer) {
    clearTimeout(_ttTestPollTimer);
    _ttTestPollTimer = null;
  }
}

async function pollTtTestUpload() {
  if (!_ttTestUploadId) return;
  try {
    const st = await api(`/api/tiktok/upload/${_ttTestUploadId}`);
    const tr = st.testResult;
    if (tr) showTtTestResultSheet(tr, st);
    if (st.status === "running") {
      setTtTestStatus(
        `Testing with ${st.uploadEngine || "engine"}… ${st.completed || 0}/${st.total || 1}`
      );
      _ttTestPollTimer = setTimeout(pollTtTestUpload, 1200);
      return;
    }
    stopTtTestPoll();
    setTtSettingsLocked(false);
    _ttTestUploadId = null;
    if (tr && tr.ok) {
      setTtTestStatus("Test upload passed", false);
    } else {
      setTtTestStatus(
        (tr && (tr.error || (tr.incident && tr.incident.title))) || "Test upload failed",
        true
      );
    }
    if (tr) showTtTestResultSheet(tr, st);
    loadTtAccounts().catch(() => {});
  } catch (err) {
    stopTtTestPoll();
    setTtSettingsLocked(false);
    _ttTestUploadId = null;
    setTtTestStatus(err.message || "Test upload status error", true);
  }
}

async function startTtTestUpload() {
  setTtTestStatus("Starting test upload…");
  setTtSettingsLocked(true);
  hideTtTestResultSheet();
  try {
    const st = await api("/api/tiktok/test-upload", { method: "POST", body: "{}" });
    _ttTestUploadId = st.uploadId || st.id;
    if (!_ttTestUploadId) throw new Error("No upload id returned");
    if (st.testResult) showTtTestResultSheet(st.testResult, st);
    setTtTestStatus(`Running (${st.uploadEngine || "engine"})…`);
    pollTtTestUpload();
  } catch (err) {
    setTtSettingsLocked(false);
    setTtTestStatus(err.message || "Could not start test", true);
    showTtTestResultSheet(
      {
        ready: true,
        ok: false,
        status: "done",
        engine: ($("ttUploadEngine") && $("ttUploadEngine").value) || "studio",
        caption: "Caption Bay engine test - 7f3a9c - ignore this post",
        filename: "tiktok_test_upload.mp4",
        error: err.message || String(err),
        incident: null,
        postUrl: "",
        elapsedMs: null,
      },
      null
    );
  }
}

function wireTtTestUpload() {
  if ($("ttTestUploadBtn")) {
    $("ttTestUploadBtn").addEventListener("click", () => {
      startTtTestUpload().catch(() => {});
    });
  }
  const hide = () => hideTtTestResultSheet();
  if ($("ttTestResultClose")) $("ttTestResultClose").onclick = hide;
  if ($("ttTestResultOk")) $("ttTestResultOk").onclick = hide;
  if ($("ttTestResultBackdrop")) $("ttTestResultBackdrop").onclick = hide;
}

function pollUpload(uploadId) {
  const id = uploadId || focusedUploadId || currentUploadId;
  if (!id) return;
  rememberUploadJob(id);
  pollUntil(async () => {
    try {
      const st = await api(`/api/tiktok/upload/${id}`);
      jobSnapshots[id] = st;
      refreshActiveJobsDock();
      if (id === focusedUploadId || id === currentUploadId || !focusedUploadId) {
        focusedUploadId = focusedUploadId || id;
        currentUploadId = focusedUploadId;
        renderUploadState(st);
      }
      if (st.status === "running" || st.status === "queued") return true;
      if (st.canContinue && st.sourceBatchId) {
        lastFinishedUploadSt = st;
      } else {
        lastFinishedUploadSt = null;
        if (st.status === "done") lastFinishedBatchSt = null;
      }
      forgetUploadJob(id);
      maybeAutoExpandDock(id, st.status);
      loadTtAccounts().catch(() => {});
      loadOutputs().catch(() => {});
      return false;
    } catch (err) {
      const msg = String(err && err.message ? err.message : err);
      if (/not found|404/i.test(msg)) {
        forgetUploadJob(id);
        setTtSheetUploadMode(false);
        const note =
          "Upload job lost (server restarted or job expired). " +
          "The UI was frozen mid-batch — re-select remaining videos to finish.";
        if ($("tiktokUploadSummary")) $("tiktokUploadSummary").textContent = note;
        if ($("tiktokUploadSummaryStandalone")) {
          $("tiktokUploadSummaryStandalone").textContent = note;
        }
        if ($("ttSheetProgressSummary")) $("ttSheetProgressSummary").textContent = note;
        if ($("ttOutputMeta")) $("ttOutputMeta").textContent = note;
        if ($("cancelTikTokUpload")) $("cancelTikTokUpload").classList.add("hidden");
        if ($("cancelTikTokUploadStandalone")) {
          $("cancelTikTokUploadStandalone").classList.add("hidden");
        }
        if (lastUploadBatchId) {
          lastFinishedUploadSt = {
            id,
            status: "done",
            canContinue: true,
            sourceBatchId: lastUploadBatchId,
            uploaded: 0,
            total: 0,
            items: [],
          };
          if ($("continueTikTokUploadStandalone")) {
            $("continueTikTokUploadStandalone").classList.remove("hidden");
          }
        } else {
          lastFinishedUploadSt = null;
        }
        if ($("uploadToTikTok")) $("uploadToTikTok").disabled = false;
        refreshActiveJobsDock();
        syncDockCancelButtons();
        return false;
      }
      return { continue: true, interval: 2500 };
    }
  }, { interval: 1500, key: "tiktok-upload:" + id });
}

async function watchAutoUpload(batchId) {
  const wantBatch = batchId || focusedBatchId || currentBatchId;
  try {
    const active = await api("/api/jobs/active");
    const uploads = active.uploads || [];
    const match = uploads.find(
      (u) => u && u.sourceBatchId === wantBatch && (u.status === "running" || u.status === "queued")
    );
    if (match && match.id) {
      rememberUploadJob(match.id, match.sourceBatchId);
      showTtUploadProgressSheet(match);
      startWatchingUpload(match.id);
      return;
    }
    const st = await api("/api/tiktok/upload-latest");
    if (st && st.id && st.sourceBatchId === wantBatch) {
      rememberUploadJob(st.id, st.sourceBatchId);
      if (st.status === "running" || st.status === "queued") {
        showTtUploadProgressSheet(st);
        startWatchingUpload(st.id);
      } else {
        renderUploadState(st);
      }
    }
  } catch {}
}

async function resumeActiveJobs() {
  const saved = readActiveJobsLs();
  let active = { batches: [], uploads: [], facebook: [], sparks: [], provisions: [], logins: [] };
  try {
    active = await api("/api/jobs/active");
  } catch {}

  const batches = Array.isArray(active.batches) ? active.batches : [];
  const uploads = Array.isArray(active.uploads) ? active.uploads : [];
  const facebook = Array.isArray(active.facebook) ? active.facebook : [];
  const sparks = Array.isArray(active.sparks) ? active.sparks : [];
  const logins = Array.isArray(active.logins) ? active.logins : [];

  // Server is source of truth — reattach every running/queued job.
  for (const upload of uploads) {
    if (!upload || !upload.id) continue;
    if (upload.status !== "running" && upload.status !== "queued") continue;
    rememberUploadJob(upload.id, upload.sourceBatchId);
    jobSnapshots[upload.id] = upload;
    if (!focusedUploadId || focusedUploadId === upload.id) {
      showTtUploadProgressSheet(upload);
    }
    startWatchingUpload(upload.id);
  }
  if (!uploads.length && saved.uploadIds && saved.uploadIds.length) {
    for (const uid of saved.uploadIds) {
      try {
        const st = await api(`/api/tiktok/upload/${uid}`);
        if (st && (st.status === "running" || st.status === "queued")) {
          rememberUploadJob(st.id, st.sourceBatchId || saved.uploadBatchId);
          jobSnapshots[st.id] = st;
          showTtUploadProgressSheet(st);
          startWatchingUpload(st.id);
        }
      } catch {}
    }
  } else if (saved.uploadBatchId) {
    lastUploadBatchId = saved.uploadBatchId;
  }

  for (const batch of batches) {
    if (!batch || !batch.id) continue;
    if (batch.status !== "running" && batch.status !== "queued") continue;
    rememberBatchJob(batch.id);
    jobSnapshots[batch.id] = batch;
    showProgressDock();
    if (batch.id === focusedBatchId) renderBatchState(batch);
    watchBatch(batch.id);
  }
  if (!batches.length && saved.batchIds && saved.batchIds.length) {
    for (const bid of saved.batchIds) {
      try {
        const st = await api(`/api/batch/${bid}`);
        if (st && (st.status === "running" || st.status === "queued")) {
          rememberBatchJob(st.id || bid);
          jobSnapshots[st.id || bid] = st;
          showProgressDock();
          watchBatch(st.id || bid);
        }
      } catch {}
    }
  }

  for (const fb of facebook) {
    if (!fb || !fb.id) continue;
    if (fb.status !== "running" && fb.status !== "queued") continue;
    jobSnapshots[fb.id] = fb;
    startWatchingFbAds(fb.id);
  }
  if (!facebook.length && saved.fbJobId) {
    try {
      const st = await api(`/api/facebook/ads/${saved.fbJobId}`);
      if (st && (st.status === "running" || st.status === "queued")) {
        jobSnapshots[st.id] = st;
        startWatchingFbAds(st.id);
      } else {
        writeActiveJobsLs();
      }
    } catch {
      // Stale id from a prior server process — drop it.
      if (currentFbJobId === saved.fbJobId) forgetFbJob(saved.fbJobId);
      else writeActiveJobsLs();
    }
  }

  // Resume spark jobs from server
  for (const spark of sparks) {
    if (!spark || !spark.id) continue;
    if (spark.status !== "running" && spark.status !== "queued") continue;
    rememberSparkJob(spark.id);
    jobSnapshots[spark.id] = spark;
    showProgressDock();
    updateSparkFetchDock(spark);
    watchSparkJob(spark.id);
  }
  // Fallback to localStorage if server didn't return any
  if (!sparks.length && saved.sparkJobIds && saved.sparkJobIds.length) {
    for (const sid of saved.sparkJobIds) {
      try {
        const st = await api(`/api/spark/jobs/${sid}`);
        if (st && (st.status === "running" || st.status === "queued")) {
          rememberSparkJob(st.id || sid);
          jobSnapshots[st.id || sid] = st;
          showProgressDock();
          updateSparkFetchDock(st);
          watchSparkJob(st.id || sid);
        }
      } catch {}
    }
  }

  // Resume TikTok remote login if the browser session is still open server-side.
  const login = logins[0] || null;
  if (login && login.loginId) {
    resumeRemoteLogin(login);
  } else if (saved.loginId) {
    try {
      const st = await api(`/api/tiktok/accounts/login/${saved.loginId}`);
      if (st && (st.status === "starting" || st.status === "waiting")) {
        resumeRemoteLogin(st);
      } else {
        writeActiveJobsLs();
      }
    } catch {
      writeActiveJobsLs();
    }
  }

  // Provision work is not part of /api/jobs/active. Reattach it here so an
  // ad launch started before a reload immediately has a visible dock and
  // Cancel control, without requiring the user to open the BC page first.
  try {
    const provision = await api("/api/provision/jobs?limit=20");
    if (typeof updateProvisionDock === "function" && (provision.active || []).length) {
      updateProvisionDock({ active: provision.active, source: "background" });
    }
  } catch {}

  refreshActiveJobsDock();
  startActiveJobsSync();
}

/**
 * Re-pull /api/jobs/active periodically so jobs started from chat (or another
 * tab) show up in the dock even if this page didn't start them.
 */
function startActiveJobsSync() {
  pollUntil(async () => {
    if (document.visibilityState === "hidden") {
      return { continue: true, interval: 15000 };
    }
    try {
      await syncActiveJobsFromServer();
    } catch {}
    const busy =
      activeBatchIds.size > 0 ||
      activeUploadIds.size > 0 ||
      activeSparkIds.size > 0 ||
      !!currentFbJobId ||
      !!remoteLoginId;
    return { continue: true, interval: busy ? 4000 : 12000 };
  }, { interval: 5000, key: "active-jobs-sync" });
}

async function syncActiveJobsFromServer() {
  let active;
  try {
    active = await api("/api/jobs/active");
  } catch {
    return;
  }
  const batches = Array.isArray(active.batches) ? active.batches : [];
  const uploads = Array.isArray(active.uploads) ? active.uploads : [];
  const facebook = Array.isArray(active.facebook) ? active.facebook : [];
  const sparks = Array.isArray(active.sparks) ? active.sparks : [];
  const logins = Array.isArray(active.logins) ? active.logins : [];

  const liveBatch = new Set(batches.map((b) => b && b.id).filter(Boolean));
  const liveUpload = new Set(uploads.map((u) => u && u.id).filter(Boolean));
  const liveFb = new Set(facebook.map((f) => f && f.id).filter(Boolean));
  const liveSpark = new Set(sparks.map((s) => s && s.id).filter(Boolean));

  for (const upload of uploads) {
    if (!upload || !upload.id) continue;
    if (upload.status !== "running" && upload.status !== "queued") continue;
    if (activeUploadIds.has(upload.id)) {
      jobSnapshots[upload.id] = upload;
      continue;
    }
    rememberUploadJob(upload.id, upload.sourceBatchId);
    jobSnapshots[upload.id] = upload;
    startWatchingUpload(upload.id);
  }
  for (const batch of batches) {
    if (!batch || !batch.id) continue;
    if (batch.status !== "running" && batch.status !== "queued") continue;
    if (activeBatchIds.has(batch.id)) {
      jobSnapshots[batch.id] = batch;
      continue;
    }
    rememberBatchJob(batch.id);
    jobSnapshots[batch.id] = batch;
    showProgressDock();
    watchBatch(batch.id);
  }
  for (const fb of facebook) {
    if (!fb || !fb.id) continue;
    if (fb.status !== "running" && fb.status !== "queued") continue;
    jobSnapshots[fb.id] = fb;
    if (currentFbJobId === fb.id) continue;
    startWatchingFbAds(fb.id);
  }
  for (const spark of sparks) {
    if (!spark || !spark.id) continue;
    if (spark.status !== "running" && spark.status !== "queued") continue;
    const wasTracked = activeSparkIds.has(spark.id);
    updateSparkFetchDock(spark);
    if (!wasTracked) watchSparkJob(spark.id);
  }
  if (logins.length && !remoteLoginId) {
    resumeRemoteLogin(logins[0]);
  }

  // Drop local tracking for jobs the server no longer has (restarted process).
  for (const id of [...activeBatchIds]) {
    if (!liveBatch.has(id) && !jobSnapshots[id]) {
      // Still polling — leave alone; poller handles 404.
    }
  }
  for (const id of [...activeUploadIds]) {
    if (!liveUpload.has(id) && !jobSnapshots[id]) {
      // Still polling — leave alone.
    }
  }
  if (currentFbJobId && !liveFb.has(currentFbJobId) && !jobSnapshots[currentFbJobId]) {
    // Leave for poller.
  }
  for (const id of [...activeSparkIds]) {
    if (!liveSpark.has(id) && !jobSnapshots[id]) {
      // Keep the id until its dedicated poller confirms a terminal state.
    }
  }
  try {
    const provision = await api("/api/provision/jobs?limit=20");
    if (typeof updateProvisionDock === "function" && (provision.active || []).length) {
      updateProvisionDock({ active: provision.active, source: "background" });
    }
  } catch {}
  refreshActiveJobsDock();
}

function resumeRemoteLogin(st) {
  if (!st || !st.loginId) return;
  if (remoteLoginId === st.loginId) return;
  remoteLoginId = st.loginId;
  writeActiveJobsLs();
  const modal = $("remoteLoginModal");
  if (modal) modal.classList.remove("hidden");
  if ($("remoteScreenHint")) $("remoteScreenHint").style.display = "";
  setRemoteMsg(st.message || "Reconnected — waiting for sign-in…");
  if ($("remoteScreenWrap")) $("remoteScreenWrap").focus();
  pollRemoteFrame();
  pollLogin(st.loginId);
}

// Flush remembered job ids before the tab dies (does NOT cancel server work).
window.addEventListener("pagehide", () => {
  try {
    writeActiveJobsLs();
  } catch {}
});
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") {
    syncActiveJobsFromServer().catch(() => {});
  }
});
