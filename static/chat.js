/* Chat — multi-thread AI agent UI (depends on app.js globals) */
"use strict";

(function () {
  const $ = (id) => (window.$ ? window.$(id) : document.getElementById(id));
  const api = (...args) => window.api(...args);
  const authHeaders = (...args) => window.authHeaders(...args);
  const withAuth = (url) => window.withAuth(url);
  const esc = (s) =>
    (window.escapeHtml || ((x) => String(x)))(s == null ? "" : String(s));

  // The workspace request can race Create's debounced save when someone adjusts
  // Look and immediately opens Chat.  The controls are shared in this document,
  // so layer their current value over the persisted workspace copy for every
  // Chat render/lookup request.
  function currentCaptionSettings(workspace) {
    const saved = workspace && workspace.style && typeof workspace.style === "object"
      ? workspace.style
      : {};
    const live = typeof window.settingsPayload === "function"
      ? window.settingsPayload()
      : {};
    return { ...saved, ...live };
  }

  let modelsCatalog = [];
  let modelAliases = {};
  let modelPrefs = { provider: "", models: {} };
  let prefsSaveTimer = 0;
  const DEFAULT_SLASH_COMMANDS = [
    { id: "all", cmd: "/all", hint: "Every AI answers; uses the last model you picked for each provider", enabled: true },
    { id: "multiple", cmd: "/multiple", hint: "Pick specific models from any providers, then run one prompt", enabled: true },
    { id: "research", cmd: "/research", hint: "Deep research with Perplexity", enabled: true },
    { id: "pinterest", cmd: "/pinterest", hint: "bot picks search, reopen, or download from your message", enabled: true },
    { id: "preview", cmd: "/preview", hint: "Spreadsheet of what will render — then Render or Upload", enabled: true },
    { id: "render", cmd: "/render", hint: "Open the render plan and burn-in selected captions", enabled: true },
    { id: "help", cmd: "/help", hint: "List chat commands", enabled: true },
  ];
  let slashCommands = DEFAULT_SLASH_COMMANDS.slice();
  let slashIndex = 0;
  let threads = [];
  let activeThreadId = null;
  let sending = false;
  let previewPairs = [];
  let previewSelectionPlanKey = "";
  let resetPreviewSelection = false;
  let previewPlanMeta = {
    mode: "",
    origin: "",
    modelLabel: "",
    intent: "preview",
    videoCount: 0,
    captionCount: 0,
    batchId: "",
    tiktokPosted: 0,
    tiktokFailed: 0,
    tiktokAccountId: "",
    tiktokAccountName: "",
    sparkJobId: "",
  };
  let duoPlusConfigured = false;
  let sparkPollTimer = 0;
  let sparkPollDeadline = 0;
  let sparkFetchStarting = false;
  let sparkPipelineStarting = false;
  let carouselStatusTimer = 0;
  let threadPlans = [];
  let workspacePlans = [];
  let activePlanId = "";
  let pendingPlanId = "";
  let planSelectionGeneration = 0;
  const workspacePlanDetails = new Map();
  const threadDetailCache = new Map();
  let threadOpenGeneration = 0;
  /** "workspace" | "chat" — which sidebar list owns activePlanId. */
  let activePlanSource = "workspace";
  let planHoverTimer = 0;
  let planHoverId = "";
  let planHoverGen = 0;
  let previewEtaTimer = 0;
  let createPlanIntent = ""; // "" | "render" | "upload" | "sparks"
  let resultsItems = [];
  let resultsBatchId = "";
  let usageRange = "session";
  let usageTimer = 0;
  let liveReview = null;
  let persistReviewTimer = 0;
  let captionPickItems = [];
  let captionPickPrompt = "";
  let captionPickOrigin = "/all";
  let multipleModelIds = loadMultipleModelIds();
  let pendingAttachments = [];
  let chatLibrarySources = [];
  const LS_USAGE = "captionToolChatUsage";
  const LS_MULTIPLE = "captionToolMultipleModels";

  function loadMultipleModelIds() {
    try {
      const raw = localStorage.getItem("captionToolMultipleModels");
      const arr = raw ? JSON.parse(raw) : [];
      return Array.isArray(arr) ? arr.map(String).filter(Boolean) : [];
    } catch {
      return [];
    }
  }

  function saveMultipleModelIds(ids) {
    multipleModelIds = (ids || []).map(String).filter(Boolean);
    try {
      localStorage.setItem(LS_MULTIPLE, JSON.stringify(multipleModelIds));
    } catch {}
  }

  function chatStick(box) {
    if (!box) return false;
    return box.scrollHeight - box.scrollTop - box.clientHeight < 96;
  }
  function scrollChat(box, force) {
    if (!box) return;
    if (force || chatStick(box)) box.scrollTop = box.scrollHeight;
  }
  function scrollChatToEnd() {
    const box = $("chatMessages");
    if (!box) return;
    const go = () => {
      box.scrollTop = box.scrollHeight;
    };
    go();
    requestAnimationFrame(() => {
      go();
      // Pinterest grids / review cards grow after images layout.
      setTimeout(go, 80);
      setTimeout(go, 280);
    });
  }

  function emptyStateHtml() {
    return `<div class="chat-empty-state">
      <p>New chat — ask for hooks, preview, render, or type / for commands.</p>
      <div class="chat-empty-cmds">
        <button type="button" class="btn" data-slash="/all "> /all</button>
        <button type="button" class="btn" data-slash="/multiple"> /multiple</button>
        <button type="button" class="btn" data-slash="/preview"> /preview</button>
        <button type="button" class="btn" data-slash="/render"> /render</button>
        <button type="button" class="btn" data-slash="/research "> /research</button>
        <button type="button" class="btn" data-slash="/pinterest "> /pinterest</button>
      </div>
    </div>`;
  }

  function growComposer() {
    const el = $("chatInput");
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }

  function toolChips(names) {
    const wrap = document.createElement("div");
    wrap.className = "chat-tools-chips";
    for (const name of names) {
      const chip = document.createElement("span");
      chip.className = "chat-tool-chip";
      chip.textContent = name;
      wrap.appendChild(chip);
    }
    return wrap;
  }

  function parseToolJson(content) {
    try {
      return JSON.parse(content || "{}") || {};
    } catch {
      return {};
    }
  }

  function ensureActivityPanel(bubble) {
    if (!bubble) return null;
    let el = bubble.querySelector(":scope > .chat-activity");
    if (el) return el;
    el = document.createElement("details");
    el.className = "chat-activity";
    el.innerHTML =
      '<summary class="chat-activity-summary">Working…</summary>' +
      '<div class="chat-activity-log" role="log"></div>';
    const role = bubble.querySelector(":scope > .chat-role");
    const body = bubble.querySelector(":scope > .chat-body");
    if (role && role.nextSibling) bubble.insertBefore(el, role.nextSibling);
    else if (body) bubble.insertBefore(el, body);
    else bubble.appendChild(el);
    return el;
  }

  function pushActivity(bubble, line, summary) {
    const el = ensureActivityPanel(bubble);
    if (!el) return;
    const log = el.querySelector(".chat-activity-log");
    if (log && line) {
      const row = document.createElement("div");
      row.className = "chat-activity-line";
      row.textContent = line;
      log.appendChild(row);
    }
    const sum = el.querySelector(".chat-activity-summary");
    if (sum && summary) sum.textContent = summary;
    el.open = true;
  }

  function finishActivity(bubble, summary) {
    const el = bubble && bubble.querySelector(":scope > .chat-activity");
    if (!el) return;
    const sum = el.querySelector(".chat-activity-summary");
    if (sum && summary) sum.textContent = summary;
    el.open = false;
  }

  function updatePinterestCountLabel(bubble, count) {
    if (!bubble) return;
    const body = bubble.querySelector(":scope > .chat-body");
    if (!body) return;
    const n = Number(count) || 0;
    const text = n === 1 ? "Found 1 video pin." : `Found ${n} video pin(s).`;
    if (body.classList.contains("is-thinking") || !(body.textContent || "").trim()
      || /^Found \d+ video pin/i.test((body.textContent || "").trim())) {
      setChatBody(body, text, true);
    }
  }

  function mergePinLists(a, b) {
    const byUrl = new Map();
    for (const p of [...(a || []), ...(b || [])]) {
      if (!p) continue;
      const url = (p.url || p.pinUrl || "").trim();
      if (!url) continue;
      byUrl.set(url, p);
    }
    return [...byUrl.values()];
  }

  function pinAttachmentFromPin(pin) {
    const pinUrl = ((pin && (pin.pinUrl || pin.url)) || "").trim();
    return {
      kind: "pin",
      pinUrl,
      url: pinUrl,
      name: (pin && (pin.title || pin.name)) || "Pinterest pin",
      title: (pin && (pin.title || pin.name)) || "",
      thumbUrl: (pin && (pin.thumbnail || pin.thumbUrl)) || "",
      previewUrl: (pin && pin.previewUrl) || "",
      previewKind: (pin && pin.previewKind) || "",
      width: (pin && pin.width) || 0,
      height: (pin && pin.height) || 0,
      duration: (pin && pin.duration) || 0,
      platform: "pinterest",
    };
  }

  function attachmentKey(att) {
    if (!att) return "";
    if (att.kind === "pin" || att.pinUrl) return "pin:" + (att.pinUrl || att.url || "");
    if (att.libraryId) return "library:" + att.libraryId;
    if (att.kind === "image" || att.imageId) return "image:" + (att.imageId || "");
    return "source:" + (att.sourceId || "");
  }

  const attachmentRequests = new Map();
  let attachmentRequestSerial = 0;

  async function queueChatAttachment(spec) {
    let optimisticKey = "";
    const requestKey = spec && (spec.optimisticAttachment || spec.sourceId || spec.libraryId || spec.imageId)
      ? attachmentKey(spec.optimisticAttachment || spec)
      : "";
    const requestSerial = requestKey ? ++attachmentRequestSerial : 0;
    if (requestKey) attachmentRequests.set(requestKey, requestSerial);
    try {
      let slim = null;
      if (spec && spec.optimisticAttachment) {
        slim = { ...spec.optimisticAttachment };
        optimisticKey = attachmentKey(slim);
        pendingAttachments = pendingAttachments.filter((a) => attachmentKey(a) !== optimisticKey);
        pendingAttachments.push(slim);
        renderAttachChips();
        const input = $("chatInput");
        if (input) input.focus();
      }
      if (spec && spec.file) {
        const fd = new FormData();
        fd.append("file", spec.file);
        slim = await api("/api/chat/attach", { method: "POST", body: fd });
      } else if (spec && (spec.kind === "pin" || spec.pinUrl)) {
        slim = pinAttachmentFromPin(spec);
        if (!slim.pinUrl) return;
      } else if (spec && (spec.libraryId || (spec.kind === "image" && spec.imageId))) {
        const lid = spec.libraryId || spec.imageId;
        slim = await api(`/api/chat/attach/image/${encodeURIComponent(lid)}`);
      } else if (spec && spec.sourceId) {
        slim = await api(`/api/chat/attach/source/${spec.sourceId}`);
      }
      if (requestKey && attachmentRequests.get(requestKey) !== requestSerial) return;
      if (!slim) return;
      if (spec && spec.optimisticAttachment && optimisticKey) {
        // Keep the content-addressed, generate-once poster URL from the
        // instant local row; the server payload intentionally stays generic.
        slim = {
          ...spec.optimisticAttachment,
          ...slim,
          thumbUrl: spec.optimisticAttachment.thumbUrl || slim.thumbUrl || "",
        };
      }
      const key = attachmentKey(slim);
      pendingAttachments = pendingAttachments.filter((a) => attachmentKey(a) !== key);
      pendingAttachments.push(slim);
      renderAttachChips();
      const input = $("chatInput");
      if (input) input.focus();
    } catch (err) {
      if (requestKey && attachmentRequests.get(requestKey) !== requestSerial) return;
      if (optimisticKey) {
        pendingAttachments = pendingAttachments.filter((a) => attachmentKey(a) !== optimisticKey);
        renderAttachChips();
      }
      setStatus(err.message || String(err), true);
    }
  }
  window.queueChatAttachment = queueChatAttachment;

  window.isChatAttachmentQueued = (sourceId) => pendingAttachments.some(
    (att) => att && att.sourceId === sourceId
  );

  function unqueueChatAttachment(spec) {
    const sourceId = (spec && spec.sourceId) || "";
    const imageId = (spec && spec.imageId) || "";
    const libraryId = (spec && spec.libraryId) || "";
    const pinUrl = (spec && (spec.pinUrl || (spec.kind === "pin" && spec.url))) || "";
    if (!sourceId && !imageId && !libraryId && !pinUrl) return;
    for (const key of [
      sourceId && `source:${sourceId}`,
      libraryId && `library:${libraryId}`,
      imageId && `image:${imageId}`,
      pinUrl && `pin:${pinUrl}`,
    ].filter(Boolean)) {
      attachmentRequests.delete(key);
    }
    const before = pendingAttachments.length;
    pendingAttachments = pendingAttachments.filter((a) => {
      if (pinUrl && (a.kind === "pin" || a.pinUrl) && (a.pinUrl || a.url) === pinUrl) {
        return false;
      }
      if (libraryId && a.libraryId === libraryId) return false;
      if (sourceId && a.sourceId === sourceId) return false;
      if (imageId && a.imageId === imageId) return false;
      return true;
    });
    if (pendingAttachments.length !== before) {
      renderAttachChips();
    }
  }
  window.unqueueChatAttachment = unqueueChatAttachment;

  function attachmentThumbUrl(att) {
    if (!att) return "";
    if (att.thumbUrl) {
      if (/^https?:\/\//i.test(att.thumbUrl)) return att.thumbUrl;
      return withAuth(att.thumbUrl);
    }
    if (att.thumbnail && /^https?:\/\//i.test(att.thumbnail)) return att.thumbnail;
    if (att.libraryId) return withAuth(`/api/images/${att.libraryId}/file`);
    if (att.imageId) return withAuth(`/api/chat/images/${att.imageId}`);
    if (att.sourceId) return withAuth(`/api/sources/${att.sourceId}/frame`);
    return "";
  }

  function syncPinterestGridSelection() {
    const urls = new Set(
      pendingAttachments
        .filter((a) => a && (a.kind === "pin" || a.pinUrl))
        .map((a) => a.pinUrl || a.url)
        .filter(Boolean)
    );
    document.querySelectorAll(".chat-pinterest-grid").forEach((grid) => {
      const set = grid._pinSelected;
      if (set && typeof set.clear === "function") {
        set.clear();
        urls.forEach((u) => set.add(u));
      }
      grid.querySelectorAll(".pinterest-card").forEach((card) => {
        const url = card.dataset.url || "";
        card.classList.toggle("selected", urls.has(url));
      });
    });
  }

  function closeAttachLightbox() {
    const lb = $("chatAttachLightbox");
    if (!lb) return;
    lb.classList.add("hidden");
    const img = lb.querySelector("img");
    if (img) img.removeAttribute("src");
  }

  function openAttachLightbox(src, alt) {
    let lb = $("chatAttachLightbox");
    if (!lb) {
      lb = document.createElement("div");
      lb.id = "chatAttachLightbox";
      lb.className = "chat-attach-lightbox hidden";
      lb.setAttribute("role", "dialog");
      lb.setAttribute("aria-modal", "true");
      lb.setAttribute("aria-label", "Attachment preview");
      lb.innerHTML =
        '<button type="button" class="chat-attach-lightbox-close" aria-label="Close">✕</button>' +
        '<img alt="" />';
      document.body.appendChild(lb);
      lb.addEventListener("click", (e) => {
        if (e.target === lb || e.target.closest(".chat-attach-lightbox-close")) {
          closeAttachLightbox();
        }
      });
    }
    const img = lb.querySelector("img");
    if (img) {
      img.src = src;
      img.alt = alt || "";
    }
    lb.classList.remove("hidden");
  }

  function buildAttachmentTiles(atts, { canRemove = false } = {}) {
    const row = document.createElement("div");
    row.className = "chat-attach-tiles";
    (atts || []).forEach((att, i) => {
      const tile = document.createElement("div");
      tile.className = "chat-attach-tile";
      tile.title = att.name || att.title || "Attachment";
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chat-attach-tile-preview";
      btn.setAttribute(
        "aria-label",
        "View " + (att.name || att.title || "attachment")
      );
      const img = document.createElement("img");
      img.alt = "";
      img.loading = "lazy";
      const rawThumb = att && (att.thumbUrl || att.thumbnail) || "";
      if (/^\/api\/sources\//.test(rawThumb) && window.cachedLibraryMediaUrl) {
        window.cachedLibraryMediaUrl(rawThumb).then((src) => {
          if (img.isConnected) img.src = src;
        }).catch(() => { img.src = attachmentThumbUrl(att); });
      } else {
        img.src = attachmentThumbUrl(att);
      }
      btn.appendChild(img);
      btn.addEventListener("click", () => {
        const src = attachmentThumbUrl(att);
        if (src) openAttachLightbox(src, att.name || att.title || "");
      });
      tile.appendChild(btn);
      if (canRemove) {
        const rm = document.createElement("button");
        rm.type = "button";
        rm.className = "chat-attach-tile-x";
        rm.setAttribute("aria-label", "Remove attachment");
        rm.textContent = "✕";
        rm.addEventListener("click", (e) => {
          e.stopPropagation();
          unqueueChatAttachment(att);
        });
        tile.appendChild(rm);
      }
      row.appendChild(tile);
    });
    return row;
  }

  function mountAttachmentTiles(parent, atts, opts) {
    if (!parent || !atts || !atts.length) return null;
    parent.querySelectorAll(":scope > .chat-attach-tiles").forEach((el) => el.remove());
    const row = buildAttachmentTiles(atts, opts);
    const body = parent.querySelector(".chat-body");
    if (body) parent.insertBefore(row, body);
    else parent.appendChild(row);
    return row;
  }

  function renderAttachChips() {
    const host = $("chatAttachChips");
    if (!host) return;
    host.innerHTML = "";
    host.hidden = !pendingAttachments.length;
    window.syncChatVideoLibrarySelection?.();
    window.syncChatImageLibrarySelection?.();
    if (!pendingAttachments.length) {
      syncPinterestGridSelection();
      return;
    }
    host.appendChild(buildAttachmentTiles(pendingAttachments, { canRemove: true }));
    syncPinterestGridSelection();
  }

  function setStatus(msg, isError) {
    const el = $("chatStatus");
    if (!el) return;
    const text = msg || "";
    el.classList.toggle("is-error", !!isError);
    el.classList.remove("is-thinking");
    el.style.color = isError ? "var(--danger)" : "";
    el.textContent = text;
  }

  function showThinkingBody(el, text) {
    if (!el) return;
    el.classList.add("is-thinking");
    el.classList.remove("chat-md");
    const wrap = document.createElement("span");
    wrap.className = "chat-run-status";
    const label = document.createElement("span");
    label.className = "chat-status-label";
    label.textContent = text || "Thinking…";
    const dots = document.createElement("span");
    dots.className = "chat-thinking-dots";
    dots.setAttribute("aria-hidden", "true");
    dots.innerHTML = "<span></span><span></span><span></span>";
    wrap.appendChild(label);
    wrap.appendChild(dots);
    el.replaceChildren(wrap);
  }

  function setRunStatus(msg, ...els) {
    const text = msg || "Thinking…";
    for (const el of els) {
      if (!el) continue;
      if (el.classList.contains("is-thinking") || !(el.textContent || "").trim()) {
        showThinkingBody(el, text);
      }
    }
  }

  function threadIdFromHash() {
    const raw = (window.location.hash || "").replace(/^#\/?/, "");
    if (raw.startsWith("chat/")) return raw.slice(5).split(/[/?#]/)[0] || "";
    return "";
  }

  function setHashThread(id) {
    if (id) window.location.hash = "#/chat/" + id;
    else window.location.hash = "#/chat";
  }

  function relativeTime(ts) {
    if (!ts) return "";
    const sec = Math.max(0, (Date.now() / 1000) - ts);
    if (sec < 60) return "just now";
    if (sec < 3600) return Math.floor(sec / 60) + "m";
    if (sec < 86400) return Math.floor(sec / 3600) + "h";
    return Math.floor(sec / 86400) + "d";
  }

  function formatTokens(n) {
    const v = Math.max(0, Math.round(Number(n) || 0));
    if (v >= 1_000_000) return (v / 1_000_000).toFixed(v >= 10_000_000 ? 1 : 2) + "M";
    if (v >= 10_000) return (v / 1000).toFixed(v >= 100_000 ? 0 : 1) + "k";
    return v.toLocaleString();
  }

  function formatUsd(n) {
    const v = Number(n) || 0;
    if (v === 0) return "$0.00";
    if (v < 0.01) return "$" + v.toFixed(4);
    if (v < 1) return "$" + v.toFixed(3);
    return "$" + v.toFixed(2);
  }

  function usageQuery() {
    const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "";
    const utcOffset = -new Date().getTimezoneOffset();
    const params = new URLSearchParams({
      range: usageRange,
      tz,
      utcOffset: String(utcOffset),
    });
    if (usageRange === "session" && activeThreadId) params.set("threadId", activeThreadId);
    return params.toString();
  }

  function renderUsage(data) {
    const totals = $("chatUsageTotals");
    const list = $("chatUsageModels");
    if (!totals || !list) return;
    const models = (data && data.models) || [];
    totals.innerHTML = `
      <div class="chat-usage-stat">
        <span class="chat-usage-stat-label">Input</span>
        <span class="chat-usage-stat-value">${formatTokens(data && data.inputTokens)}</span>
      </div>
      <div class="chat-usage-stat">
        <span class="chat-usage-stat-label">Output</span>
        <span class="chat-usage-stat-value">${formatTokens(data && data.outputTokens)}</span>
      </div>
      <div class="chat-usage-stat cost">
        <span class="chat-usage-stat-label">Estimated cost</span>
        <span class="chat-usage-stat-value">${formatUsd(data && data.costUsd)}</span>
      </div>`;
    if (usageRange === "session" && !activeThreadId) {
      list.innerHTML = '<li class="chat-usage-empty">Open a chat to see session usage.</li>';
      return;
    }
    if (!models.length) {
      list.innerHTML = '<li class="chat-usage-empty">No usage in this range yet.</li>';
      return;
    }
    list.innerHTML = models.map((m) => {
      const isPin = m.provider === "pinterest";
      const isResearch = m.provider === "perplexity";
      let detail = `${formatTokens(m.inputTokens)} in · ${formatTokens(m.outputTokens)} out`;
      if (isPin) {
        const calls = m.calls || 0;
        const credits = m.credits || 0;
        detail = `${calls} search${calls === 1 ? "" : "es"} · ${credits} credit${credits === 1 ? "" : "s"}`;
      } else if (isResearch && !(m.inputTokens || m.outputTokens)) {
        const calls = m.calls || 0;
        detail = `${calls} research run${calls === 1 ? "" : "s"}`;
      }
      const unpriced = m.priced === false ? " · unpriced" : "";
      return `<li class="chat-usage-model">
        <div class="chat-usage-model-name">${esc(m.label || m.id)}</div>
        <div class="chat-usage-model-meta">
          <span>${esc(detail)}${unpriced}</span>
          <span class="chat-usage-model-cost">${formatUsd(m.costUsd)}</span>
        </div>
      </li>`;
    }).join("");
  }

  async function refreshUsage() {
    if (!$("chatUsagePanel")) return;
    try {
      const data = await api("/api/chat/usage?" + usageQuery());
      renderUsage(data);
    } catch {
      /* keep last rendered totals */
    }
  }

  function scheduleUsageRefresh() {
    clearTimeout(usageTimer);
    usageTimer = setTimeout(() => {
      refreshUsage().catch(() => {});
    }, 120);
  }

  // ----- Models -----
  function mergeServerModelPrefs(prefs) {
    if (!prefs || typeof prefs !== "object") return;
    if (!modelPrefs.provider && prefs.provider) modelPrefs.provider = prefs.provider;
    const models = prefs.models && typeof prefs.models === "object" ? prefs.models : {};
    for (const [p, id] of Object.entries(models)) {
      if (p && id && !modelPrefs.models[p]) modelPrefs.models[p] = id;
    }
  }

  function persistModelPrefs() {
    clearTimeout(prefsSaveTimer);
    prefsSaveTimer = setTimeout(() => {
      api("/api/workspace", {
        method: "PUT",
        body: JSON.stringify({
          chatProvider: modelPrefs.provider,
          chatModels: modelPrefs.models,
        }),
      }).catch(() => {});
    }, 400);
  }

  function preferredModelFor(provider) {
    const id = (provider && modelPrefs.models[provider]) || "";
    return modelAliases[id] || id;
  }

  function canonicalModelId(id) {
    const mid = (id || "").trim();
    return mid ? modelAliases[mid] || mid : "";
  }

  function modelCatalogEntry(modelId) {
    const mid = canonicalModelId(modelId);
    return mid ? modelsCatalog.find((m) => m.id === mid) : null;
  }

  function applyThreadModel(thread) {
    if (thread.providerModels && typeof thread.providerModels === "object") {
      for (const [p, mid] of Object.entries(thread.providerModels)) {
        if (p && mid) modelPrefs.models[p] = canonicalModelId(mid);
      }
    }
    const threadModel = canonicalModelId(thread.model || "");
    const entry = threadModel ? modelCatalogEntry(threadModel) : null;
    const threadProvider = thread.provider || (entry && entry.provider) || "";
    if (threadProvider) modelPrefs.provider = threadProvider;
    if (threadProvider && threadModel) modelPrefs.models[threadProvider] = threadModel;
    fillProviderSelect(threadProvider, threadModel);
  }

  function rememberCurrentSelection() {
    const p = $("chatProvider") && $("chatProvider").value;
    const m = selectedModel();
    if (p) modelPrefs.provider = p;
    if (p && m) modelPrefs.models[p] = m;
    persistModelPrefs();
  }

  function rememberSelection(provider, modelId) {
    if (!provider || !modelId) return;
    modelPrefs.models[provider] = modelId;
    persistModelPrefs();
  }

  function selectedProviderModels() {
    const out = { ...modelPrefs.models };
    const p = $("chatProvider") && $("chatProvider").value;
    const m = selectedModel();
    if (p && m) out[p] = m;
    return out;
  }

  async function persistThreadModel() {
    rememberCurrentSelection();
    if (!activeThreadId) return;
    try {
      await api(`/api/chat/threads/${activeThreadId}`, {
        method: "PATCH",
        body: JSON.stringify({
          model: selectedModel(),
          providerModels: selectedProviderModels(),
        }),
      });
    } catch {}
  }

  // ----- Init -----
  let _modelsInflight = null;
  let _threadsInflight = null;
  let _chatInited = false;

  async function loadModels() {
    if (_modelsInflight) return _modelsInflight;
    _modelsInflight = (async () => {
      const res = await api("/api/chat/models");
      modelsCatalog = res.models || [];
      modelAliases = res.aliases || {};
      mergeServerModelPrefs(res.prefs);
      if (Array.isArray(res.commands) && res.commands.length) {
        const byId = new Map(DEFAULT_SLASH_COMMANDS.map((c) => [c.id, { ...c }]));
        for (const c of res.commands) {
          if (!c || !c.id) continue;
          byId.set(c.id, { ...(byId.get(c.id) || {}), ...c });
        }
        slashCommands = [...byId.values()];
      }
      fillProviderSelect();
    })();
    try {
      await _modelsInflight;
    } finally {
      _modelsInflight = null;
    }
  }

  function fillProviderSelect(preferProvider, preferModel) {
    const prov = $("chatProvider");
    const mod = $("chatModel");
    if (!prov || !mod) return;
    const prevP = prov.value;
    const prevM = mod.value;
    const providers = [...new Set(modelsCatalog.map((m) => m.provider))];
    const labels = { anthropic: "Claude", openai: "OpenAI", xai: "Grok", gemini: "Gemini" };
    prov.innerHTML = "";
    for (const p of providers) {
      const enabled = modelsCatalog.some((m) => m.provider === p && m.enabled);
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = (labels[p] || p) + (enabled ? "" : " (not configured)");
      opt.disabled = !enabled;
      prov.appendChild(opt);
    }
    const wantP = preferProvider || prevP || modelPrefs.provider;
    if (wantP && [...prov.options].some((o) => o.value === wantP && !o.disabled)) {
      prov.value = wantP;
    } else {
      const first = [...prov.options].find((o) => !o.disabled);
      if (first) prov.value = first.value;
    }
    fillModelSelect(preferModel || prevM || preferredModelFor(prov.value));
  }

  function fillModelSelect(prefer) {
    const prov = $("chatProvider");
    const mod = $("chatModel");
    if (!prov || !mod) return;
    const p = prov.value;
    mod.innerHTML = "";
    const list = modelsCatalog.filter((m) => m.provider === p);
    for (const m of list) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.label + (m.enabled ? "" : " (off)");
      opt.disabled = !m.enabled;
      mod.appendChild(opt);
    }
    const want = prefer || preferredModelFor(p);
    if (want && [...mod.options].some((o) => o.value === want && !o.disabled)) {
      mod.value = want;
    } else {
      const first = [...mod.options].find((o) => !o.disabled);
      if (first) mod.value = first.value;
    }
  }

  function selectedModel() {
    return ($("chatModel") && $("chatModel").value) || "";
  }

  let chatRowMenuEl = null;
  let chatRowMenuCloser = null;

  function closeRowMenu() {
    if (chatRowMenuCloser) {
      document.removeEventListener("mousedown", chatRowMenuCloser, true);
      document.removeEventListener("keydown", chatRowMenuCloser, true);
      chatRowMenuCloser = null;
    }
    if (chatRowMenuEl) {
      chatRowMenuEl.remove();
      chatRowMenuEl = null;
    }
  }

  function openRowMenu(anchor, items) {
    closeRowMenu();
    if (!anchor || !items || !items.length) return;
    const menu = document.createElement("div");
    menu.className = "chat-row-menu";
    menu.setAttribute("role", "menu");
    for (const item of items) {
      if (item.divider) {
        const hr = document.createElement("div");
        hr.className = "chat-row-menu-divider";
        menu.appendChild(hr);
        continue;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chat-row-menu-item" + (item.danger ? " is-danger" : "");
      btn.setAttribute("role", "menuitem");
      if (item.icon) {
        const ic = document.createElement("span");
        ic.className = "chat-row-menu-icon";
        ic.setAttribute("aria-hidden", "true");
        ic.textContent = item.icon;
        btn.appendChild(ic);
      }
      const lab = document.createElement("span");
      lab.textContent = item.label;
      btn.appendChild(lab);
      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        closeRowMenu();
        if (typeof item.action === "function") item.action();
      });
      menu.appendChild(btn);
    }
    document.body.appendChild(menu);
    chatRowMenuEl = menu;

    const rect = anchor.getBoundingClientRect();
    const mw = menu.offsetWidth || 180;
    const mh = menu.offsetHeight || 80;
    let left = rect.right - mw;
    let top = rect.bottom + 6;
    if (left < 8) left = 8;
    if (left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
    if (top + mh > window.innerHeight - 8) top = Math.max(8, rect.top - mh - 6);
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(top) + "px";

    chatRowMenuCloser = (ev) => {
      if (ev.type === "keydown" && ev.key === "Escape") {
        closeRowMenu();
        return;
      }
      if (ev.type === "mousedown") {
        if (menu.contains(ev.target) || anchor.contains(ev.target)) return;
        closeRowMenu();
      }
    };
    document.addEventListener("mousedown", chatRowMenuCloser, true);
    document.addEventListener("keydown", chatRowMenuCloser, true);
  }

  function makeMoreBtn(label) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chat-row-more";
    btn.title = label || "Options";
    btn.setAttribute("aria-label", label || "Options");
    btn.textContent = "⋯";
    return btn;
  }

  // ----- Threads -----
  const THREADS_CACHE_KEY = "captionToolChatThreads_v1";
  const THREADS_TTL_MS = 5 * 60 * 1000;

  function readThreadsCache() {
    try {
      const raw = sessionStorage.getItem(THREADS_CACHE_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || !Array.isArray(parsed.threads)) return null;
      return parsed;
    } catch {
      return null;
    }
  }

  function writeThreadsCache(rows) {
    try {
      sessionStorage.setItem(
        THREADS_CACHE_KEY,
        JSON.stringify({ savedAt: Date.now(), threads: rows || [] })
      );
    } catch {
      /* ignore */
    }
  }

  function paintThreadsLoading() {
    const list = $("chatThreadList");
    if (!list) return;
    if (threads.length) return;
    list.innerHTML =
      '<li class="chat-empty chat-recents-loading"><span class="spin" aria-hidden="true"></span>Loading chats…</li>';
  }

  async function refreshThreads({ force = false } = {}) {
    if (_threadsInflight) return _threadsInflight;

    const cached = readThreadsCache();
    const age = cached ? Date.now() - (cached.savedAt || 0) : Infinity;
    const cachedRows = cached && Array.isArray(cached.threads) ? cached.threads : null;
    // Paint any non-empty cache immediately so Recents isn't blank while we refresh.
    if (cachedRows && cachedRows.length && !threads.length) {
      threads = cachedRows;
      renderThreadList();
    }

    // Never treat an empty cache as fresh — that was hiding real threads after a race.
    if (!force && age < THREADS_TTL_MS && cachedRows && cachedRows.length) {
      threads = cachedRows;
      renderThreadList();
      return threads;
    }

    if (!threads.length) paintThreadsLoading();

    _threadsInflight = (async () => {
      threads = await api("/api/chat/threads");
      if (!Array.isArray(threads)) threads = [];
      writeThreadsCache(threads);
      renderThreadList();
      return threads;
    })();
    try {
      return await _threadsInflight;
    } finally {
      _threadsInflight = null;
    }
  }

  function renderThreadList() {
    const list = $("chatThreadList");
    if (!list) return;
    closeRowMenu();
    list.innerHTML = "";
    if (!threads.length) {
      list.innerHTML = '<li class="chat-empty">No chats yet — start a new one.</li>';
      return;
    }
    for (const t of threads) {
      const li = document.createElement("li");
      li.className = "chat-thread-item" + (t.id === activeThreadId ? " active" : "");
      li.dataset.id = t.id;
      const main = document.createElement("div");
      main.className = "chat-row-main";
      const title = document.createElement("div");
      title.className = "chat-thread-title";
      title.textContent = t.title || "New chat";
      main.appendChild(title);
      const more = makeMoreBtn("Chat options");
      more.addEventListener("click", (e) => {
        e.stopPropagation();
        openRowMenu(more, [
          {
            label: "Rename",
            icon: "✎",
            action: () => renameThread(t.id).catch((err) => setStatus(err.message, true)),
          },
          { divider: true },
          {
            label: "Delete",
            icon: "🗑",
            danger: true,
            action: () => deleteThread(t.id).catch((err) => setStatus(err.message, true)),
          },
        ]);
      });
      li.appendChild(main);
      li.appendChild(more);
      li.addEventListener("click", (e) => {
        if (e.target.closest(".chat-row-more") || e.target.closest(".chat-row-menu")) return;
        openThread(t.id);
        document.body.classList.remove("sidebar-open");
        if ($("sidebarBackdrop")) $("sidebarBackdrop").classList.add("hidden");
      });
      list.appendChild(li);
    }
  }

  async function renameThread(id) {
    const tid = id || activeThreadId;
    if (!tid) return;
    const t = threads.find((x) => x.id === tid);
    const next = window.prompt("Rename chat", (t && t.title) || "");
    if (next == null) return;
    const title = next.trim();
    if (!title) return;
    await api(`/api/chat/threads/${tid}`, {
      method: "PATCH",
      body: JSON.stringify({ title }),
    });
    await refreshThreads({ force: true });
  }

  async function deleteThread(id) {
    const tid = id || activeThreadId;
    if (!tid) return;
    if (!confirm("Delete this chat?")) return;
    await api(`/api/chat/threads/${tid}`, { method: "DELETE" });
    if (activeThreadId === tid) activeThreadId = null;
    await refreshThreads({ force: true });
    if (!activeThreadId) {
      $("chatMessages").innerHTML = emptyStateHtml();
      setHashThread("");
    }
    scheduleUsageRefresh();
  }

  async function newChat() {
    const thread = await api("/api/chat/threads", {
      method: "POST",
      body: JSON.stringify({
        model: selectedModel(),
        providerModels: selectedProviderModels(),
      }),
    });
    await refreshThreads({ force: true });
    // The immediately following D1 read can briefly fail while the Worker and
    // container hand off. Give a newly written document a short, bounded retry
    // instead of leaving a valid row in Recents with a misleading error.
    await openThread(thread.id, { retryNewThread: true });
  }

  async function newChatFromBc(draftText) {
    await newChat();
    location.hash = `#/chat/${activeThreadId}`;
    const input = $("chatInput");
    if (input && draftText) {
      input.value = draftText;
      input.dispatchEvent(new Event("input"));
      input.focus();
    }
  }
  window.newChatFromBc = newChatFromBc;

  function paintOpenedThread(thread) {
    if (!thread) return;
    applyThreadModel(thread);
    rememberCurrentSelection();
    renderMessages(thread.messages || []);
    threadPlans = [];
    // Plans are stored once in the workspace, but each chat remembers which
    // one it owns. Loading it here keeps an older chat's plan available without
    // making a brand-new chat inherit the workspace's last active plan.
    activePlanId = "";
    activePlanSource = "workspace";
    applyThreadPlan(null);
    loadThreadPlan()
      .then(() => refreshWorkspacePlansSidebar())
      .catch(() => renderPlanSidebar());
    if (thread.pendingConfirm) {
      showConfirmCard(thread.pendingConfirm);
    } else if (thread.pendingReview && (thread.pendingReview.roundIdeas || []).length) {
      showReviewCard(thread.pendingReview);
    } else {
      clearConfirmSlot();
      clearReviewSlot();
    }
    scheduleUsageRefresh();
  }

  async function openThread(id, { retryNewThread = false } = {}) {
    const generation = ++threadOpenGeneration;
    activeThreadId = id;
    setHashThread(id);
    renderThreadList();
    const cached = threadDetailCache.get(id);
    if (cached) paintOpenedThread(cached);
    let thread;
    try {
      const path = `/api/chat/threads/${encodeURIComponent(id)}`;
      // Retrying only a freshly created thread covers a transient Worker/D1
      // handoff without masking a deleted or invalid older chat.
      for (let attempt = 0; ; attempt += 1) {
        try {
          thread = await api(path);
          break;
        } catch (err) {
          const retryable = /thread not found|cloud storage temporarily unavailable/i
            .test(String(err && err.message));
          if (!retryNewThread || !retryable || attempt >= 2) throw err;
          await new Promise((resolve) => setTimeout(resolve, 150 * (attempt + 1)));
        }
      }
    } catch (err) {
      if (!cached && generation === threadOpenGeneration) throw err;
      return;
    }
    if (generation !== threadOpenGeneration) return;
    threadDetailCache.set(id, thread);
    paintOpenedThread(thread);
    const planModal = $("chatPreviewModal");
    if (planModal && !planModal.classList.contains("hidden")) {
      loadThreadPlan()
        .then(() => {
          paintPreviewModal();
          return refreshPlanTiktokStatus();
        })
        .then(() => paintPreviewStatus())
        .catch(() => paintPreviewModal());
    }
  }

  window.getChatActiveThreadId = () => activeThreadId;

  function repairMojibake(s) {
    let out = s == null ? "" : String(s);
    for (let i = 0; i < 3; i++) {
      if (!/[\u00C2\u00C3\u00E2][\u0080-\u00BF]/.test(out)) break;
      try {
        const bytes = Uint8Array.from(out, (ch) => ch.charCodeAt(0) & 0xff);
        out = new TextDecoder("utf-8", { fatal: true }).decode(bytes);
      } catch {
        break;
      }
    }
    return out;
  }

  function inlineMd(s) {
    return s
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(
        /\[([^\]]+)\]\((https?:[^)\s]+)\)/g,
        '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
      );
  }

  function renderChatMarkdown(text) {
    const lines = esc(repairMojibake(text || "")).split("\n");
    const out = [];
    let listType = null;
    const closeList = () => {
      if (listType) {
        out.push(listType === "ul" ? "</ul>" : "</ol>");
        listType = null;
      }
    };
    for (const line of lines) {
      const h = line.match(/^(#{1,3})\s+(.+)$/);
      if (h) {
        closeList();
        const tag = h[1].length >= 3 ? "h3" : "h2";
        out.push(`<${tag}>${inlineMd(h[2])}</${tag}>`);
        continue;
      }
      const ul = line.match(/^[-*]\s+(.+)$/);
      if (ul) {
        if (listType !== "ul") {
          closeList();
          out.push("<ul>");
          listType = "ul";
        }
        out.push(`<li>${inlineMd(ul[1])}</li>`);
        continue;
      }
      const ol = line.match(/^(\d+)\.\s+(.+)$/);
      if (ol) {
        if (listType !== "ol") {
          closeList();
          out.push(`<ol start="${ol[1]}">`);
          listType = "ol";
        }
        out.push(`<li>${inlineMd(ol[2])}</li>`);
        continue;
      }
      closeList();
      if (!line.trim()) continue;
      out.push(`<p>${inlineMd(line)}</p>`);
    }
    closeList();
    return out.join("");
  }

  function setChatBody(el, text, markdown) {
    if (!el) return;
    el.classList.remove("is-thinking");
    const cleaned = repairMojibake(text || "");
    if (markdown) {
      el.classList.add("chat-md");
      el.innerHTML = renderChatMarkdown(cleaned);
    } else {
      el.classList.remove("chat-md");
      el.textContent = cleaned;
    }
  }

  function renderMessages(messages) {
    const box = $("chatMessages");
    if (!box) return;
    box.innerHTML = "";
    if (!messages.length) {
      box.innerHTML = emptyStateHtml();
      return;
    }
    let i = 0;
    while (i < messages.length) {
      const m = messages[i];
      if (
        m.role === "tool" &&
        (m.name === "search_pinterest" || m.name === "pinterest_set")
      ) {
        const batch = [];
        while (
          i < messages.length &&
          messages[i].role === "tool" &&
          (messages[i].name === "search_pinterest" ||
            messages[i].name === "pinterest_set")
        ) {
          batch.push(messages[i]);
          i += 1;
        }
        let shownPins = null;
        let searchPins = [];
        const activityLines = [];
        for (const t of batch) {
          const data = parseToolJson(t.content);
          const pins = data.pins || [];
          const q = data.query || "";
          const n = data.count != null ? data.count : pins.length;
          if (t.name === "pinterest_set") {
            const action = String(data.action || "show").toLowerCase();
            if (action === "download") {
              const got = data.downloaded != null ? data.downloaded : n;
              activityLines.push(
                `pinterest_set download → ${got} video(s)`
              );
              continue;
            }
            const mode = String(data.mode || "replace").toLowerCase();
            if (mode === "merge" && Array.isArray(shownPins)) {
              shownPins = mergePinLists(shownPins, pins);
            } else {
              shownPins = pins.slice();
            }
            activityLines.push(
              q
                ? `pinterest_set show “${q}” → ${n} pin(s)`
                : `pinterest_set show → ${n} pin(s)`
            );
            continue;
          }
          if (q || n) {
            activityLines.push(
              q
                ? `search_pinterest “${q}” → ${n} pin(s)`
                : `search_pinterest → ${n} pin(s)`
            );
          }
          searchPins = mergePinLists(searchPins, pins);
        }
        const allPins =
          shownPins && shownPins.length ? shownPins : searchPins;
        if (!allPins.length && !activityLines.length) continue;
        const pinDiv = document.createElement("div");
        pinDiv.className = "chat-bubble chat-assistant chat-pinterest";
        pinDiv.innerHTML = `<div class="chat-role">Pinterest</div>
          <div class="chat-body"></div>`;
        if (activityLines.length) {
          const panel = ensureActivityPanel(pinDiv);
          const sum = panel.querySelector(".chat-activity-summary");
          if (sum) {
            sum.textContent =
              allPins.length === 1
                ? "Pinterest · 1 pin"
                : `Pinterest · ${allPins.length} pins`;
          }
          const log = panel.querySelector(".chat-activity-log");
          activityLines.forEach((line) => {
            const row = document.createElement("div");
            row.className = "chat-activity-line";
            row.textContent = line;
            log.appendChild(row);
          });
          panel.open = false;
        }
        setChatBody(
          pinDiv.querySelector(".chat-body"),
          allPins.length === 1
            ? "Showing 1 video pin."
            : `Showing ${allPins.length} video pin(s).`,
          true
        );
        if (allPins.length) attachPinterestGrid(pinDiv, allPins, { merge: false });
        box.appendChild(pinDiv);
        continue;
      }
      if (m.role === "tool") {
        i += 1;
        continue;
      }
      if (
        m.role === "assistant" &&
        m.tool_calls &&
        !(m.content || "").trim() &&
        m.source !== "compare" &&
        m.source !== "multiple"
      ) {
        i += 1;
        continue;
      }
      const div = document.createElement("div");
      if (
        (m.source === "compare" || m.source === "multiple") &&
        Array.isArray(m.bots) &&
        m.bots.length
      ) {
        renderCompareBubble(div, m.bots, false, m.source === "multiple" ? "multiple" : "all");
        box.appendChild(div);
        i += 1;
        continue;
      }
      div.className = "chat-bubble chat-" + (m.role || "assistant");
      const role = document.createElement("div");
      role.className = "chat-role";
      const isResearch = m.source === "perplexity";
      const isPinterest = m.source === "pinterest";
      role.textContent = m.role === "user"
        ? "You"
        : isResearch
          ? "Perplexity Research"
          : isPinterest
            ? "Pinterest"
            : "Assistant";
      if (isResearch) div.classList.add("chat-research");
      if (isPinterest) div.classList.add("chat-pinterest");
      const body = document.createElement("div");
      body.className = "chat-body";
      const fallback = m.tool_calls ? "(calling tools…)" : "";
      setChatBody(body, m.content || fallback, m.role !== "user");
      div.appendChild(role);
      div.appendChild(body);
      if (m.role === "user" && Array.isArray(m.attachments) && m.attachments.length) {
        mountAttachmentTiles(div, m.attachments, { canRemove: false });
      }
      if (isPinterest && Array.isArray(m.pins) && m.pins.length) {
        attachPinterestGrid(div, m.pins, { merge: false });
      }
      if (m.tool_calls && m.tool_calls.length && !isPinterest) {
        const names = m.tool_calls.map((t) => t.name);
        div.appendChild(toolChips(names));
        if (names.some((n) => n === "search_pinterest" || n === "pinterest_set")) {
          pushActivity(
            div,
            "Ran " + names.join(", "),
            "Used tools · " + names.length
          );
          finishActivity(div, "Used tools · " + names.length);
        }
      }
      box.appendChild(div);
      i += 1;
    }
    scrollChat(box, true);
  }

  function appendBubble(role, text, label, thinkingText) {
    const box = $("chatMessages");
    if (!box) return null;
    if (box.querySelector(".chat-empty-state")) box.innerHTML = "";
    const div = document.createElement("div");
    const isResearch = label === "Perplexity Research";
    const isPinterest = label === "Pinterest";
    div.className =
      "chat-bubble chat-" +
      role +
      (isResearch ? " chat-research" : "") +
      (isPinterest ? " chat-pinterest" : "");
    div.innerHTML = `<div class="chat-role"></div>
      <div class="chat-body"></div>`;
    div.querySelector(".chat-role").textContent =
      label || (role === "user" ? "You" : "Assistant");
    const body = div.querySelector(".chat-body");
    if (role === "assistant" && !(text || "").trim()) {
      showThinkingBody(body, thinkingText || "Thinking…");
    } else {
      setChatBody(body, text || "", role !== "user");
    }
    box.appendChild(div);
    scrollChat(box, true);
    return body;
  }

  function attachPinterestGrid(bubble, pins, { merge = true } = {}) {
    if (!bubble || !(pins || []).length) return;
    const prev = bubble.querySelector(".chat-pinterest-grid");
    let existing = [];
    if (merge && prev && Array.isArray(prev._pins)) existing = prev._pins;
    const all = mergePinLists(existing, pins);
    if (!all.length) return;
    if (prev) prev.remove();
    const grid = document.createElement("div");
    grid.className = "pinterest-results chat-pinterest-grid";
    grid._pins = all;
    bubble.appendChild(grid);
    const selected = new Set(
      pendingAttachments
        .filter((a) => a && (a.kind === "pin" || a.pinUrl))
        .map((a) => a.pinUrl || a.url)
        .filter(Boolean)
    );
    grid._pinSelected = selected;
    if (window.renderPinterestResults) {
      window.renderPinterestResults(grid, all, selected, {
        onChange: (pin, isOn) => {
          const url = (pin && (pin.url || pin.pinUrl)) || "";
          if (!url) return;
          if (isOn) queueChatAttachment(pinAttachmentFromPin(pin));
          else unqueueChatAttachment({ pinUrl: url });
        },
      });
    }
    updatePinterestCountLabel(bubble, all.length);
    return all;
  }

  function compareBotKey(bot) {
    return (bot && (bot.model || bot.provider)) || "";
  }

  function clearCompareThinking(root, fallbackText) {
    if (!root) return;
    root.querySelectorAll(".chat-compare-row").forEach((row) => {
      row.classList.remove("pending");
      const body = row.querySelector(".chat-body");
      if (!body || !body.classList.contains("is-thinking")) return;
      if (fallbackText) {
        row.classList.add("error");
        setChatBody(body, fallbackText, false);
      } else {
        setChatBody(body, "(No reply)", false);
      }
    });
  }

  function findCompareRow(root, payload) {
    if (!root) return null;
    const model = (payload && payload.model) || "";
    const provider = (payload && payload.provider) || "";
    const rows = [...root.querySelectorAll(".chat-compare-row")];
    if (model) {
      const byModel = rows.find((el) => el.dataset.model === model);
      if (byModel) return byModel;
    }
    if (provider) {
      const byProv = rows.filter((el) => el.dataset.provider === provider);
      if (byProv.length === 1) return byProv[0];
      // Prefer a still-thinking row for this provider.
      const thinking = byProv.find((el) =>
        el.querySelector(".chat-body.is-thinking")
      );
      if (thinking) return thinking;
    }
    return null;
  }

  function makeCompareRow(bot, pending) {
    const row = document.createElement("div");
    row.className =
      "chat-compare-row" +
      (bot.error ? " error" : "") +
      (pending ? " pending" : "");
    row.dataset.provider = bot.provider || "";
    row.dataset.model = bot.model || "";
    row.innerHTML = `<div class="chat-compare-label"></div><div class="chat-body"></div>`;
    const label = bot.label || bot.provider || "Model";
    row.querySelector(".chat-compare-label").textContent = bot.model
      ? label + " · " + bot.model
      : label;
    const bodyEl = row.querySelector(".chat-body");
    if (bot.error) {
      setChatBody(bodyEl, bot.error, false);
    } else if (bot.content) {
      setChatBody(bodyEl, bot.content, true);
    } else if (pending) {
      showThinkingBody(bodyEl, "Thinking…");
    } else {
      setChatBody(bodyEl, "", false);
    }
    return row;
  }

  function renderCompareBubble(div, bots, pending, command) {
    const title =
      command === "multiple" || (bots || []).length !== 4
        ? "Selected models"
        : "All models";
    // Prefer explicit command; fall back to "Selected" when not a full /all set.
    const role =
      command === "multiple"
        ? "Selected models"
        : command === "all"
          ? "All models"
          : title;
    div.className = "chat-bubble chat-compare";
    div.dataset.compareCommand = command || "all";
    div.innerHTML = `<div class="chat-role"></div>
      <div class="chat-compare-fallback chat-body hidden"></div>
      <div class="chat-compare-rows"></div>`;
    div.querySelector(".chat-role").textContent = role;
    const rows = div.querySelector(".chat-compare-rows");
    for (const bot of bots || []) {
      rows.appendChild(makeCompareRow(bot, !!pending && !bot.content && !bot.error));
    }
  }

  function appendCompareShell(command) {
    const box = $("chatMessages");
    if (!box) return null;
    if (box.querySelector(".chat-empty-state")) box.innerHTML = "";
    const div = document.createElement("div");
    renderCompareBubble(div, [], false, command || "all");
    box.appendChild(div);
    if (box) scrollChat(box, true);
    return div;
  }

  // ----- SSE -----
  async function readSSE(res, handlers) {
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop() || "";
      for (const chunk of parts) {
        let event = "message";
        let data = "";
        for (const line of chunk.split("\n")) {
          if (line.startsWith("event:")) event = line.slice(6).trim();
          else if (line.startsWith("data:")) data += line.slice(5).trim();
        }
        if (!data) continue;
        let parsed = {};
        try {
          parsed = JSON.parse(data);
        } catch {
          parsed = { text: data };
        }
        if (handlers[event]) handlers[event](parsed);
      }
    }
  }

  async function sendMessage() {
    if (sending) return;
    const input = $("chatInput");
    const text = (input && input.value || "").trim();
    const atts = pendingAttachments.map((a) => ({
      kind: a.kind || "source",
      sourceId: a.sourceId || "",
      imageId: a.imageId || "",
      libraryId: a.libraryId || "",
      name: a.name || a.title || "",
      pinUrl: a.pinUrl || a.url || "",
      thumbUrl: a.thumbUrl || a.thumbnail || "",
      title: a.title || a.name || "",
      previewUrl: a.previewUrl || "",
      previewKind: a.previewKind || "",
      width: a.width || 0,
      height: a.height || 0,
      duration: a.duration || 0,
    }));
    if (!text && !atts.length) return;
    if (!activeThreadId) {
      await newChat();
    }
    if (!activeThreadId) return;
    sending = true;
    if ($("chatSendBtn")) $("chatSendBtn").disabled = true;
    hideSlashMenu();
    if (input) input.value = "";
    growComposer();
    const sentAtts = pendingAttachments.slice();
    pendingAttachments = [];
    attachmentRequests.clear();
    renderAttachChips();
    const userBody = appendBubble("user", text || "Use the attached clips/images.");
    if (sentAtts.length && userBody) {
      const bubble = userBody.closest(".chat-bubble");
      if (bubble) mountAttachmentTiles(bubble, sentAtts, { canRemove: false });
    }
    const isResearch = /^\s*\/(research|deep|deepresearch|pplx|perplexity)\b/i.test(text);
    const isPinterest = /^\s*\/(pinterest|pintrest|pintrist|pin|pins)\b/i.test(text);
    const isMultiple = /^\s*\/(multiple|multi|models)\b/i.test(text);
    const isCompare = isMultiple || /^\s*\/(all|compare|bots|each)\b/i.test(text);
    const isPlan = /^\s*\/(preview|plan|render|burn)\b/i.test(text);
    let assistantBody = null;
    let followBody = null;
    let pinGridDone = false;
    let compareRoot = null;
    const thinkingText = isMultiple
      ? "Asking selected models…"
      : isCompare
        ? "Asking every model…"
        : isResearch
          ? "Deep researching…"
          : isPinterest
            ? "Searching Pinterest for angles…"
            : isPlan
              ? "Building render plan…"
              : "Thinking…";
    if (isCompare) {
      compareRoot = appendCompareShell(isMultiple ? "multiple" : "all");
      const fallback = compareRoot && compareRoot.querySelector(".chat-compare-fallback");
      if (fallback) {
        fallback.classList.remove("hidden");
        showThinkingBody(fallback, thinkingText);
      }
    } else {
      assistantBody = appendBubble(
        "assistant",
        "",
        isResearch ? "Perplexity Research" : "Assistant",
        thinkingText
      );
    }
    const reviewSnap = snapshotReviewChoices();
    clearConfirmSlot();

    try {
      const headers = {
        "Content-Type": "application/json",
        ...(await authHeaders()),
      };
      const body = {
        content: text,
        model: selectedModel(),
        providerModels: selectedProviderModels(),
        approvedIds: reviewSnap.approvedIds,
        deniedIds: reviewSnap.deniedIds,
        attachments: atts,
      };
      if (isMultiple && multipleModelIds.length) {
        body.models = multipleModelIds.slice();
      }
      const res = await fetch(`/api/chat/threads/${activeThreadId}/messages`, {
        method: "POST",
        headers,
        body: JSON.stringify(body),
      });
      if (!res.ok) {
        let d = res.statusText;
        try {
          d = (await res.json()).detail || d;
        } catch {}
        throw new Error(d);
      }
      let acc = "";
      let followAcc = "";
      const compareAcc = {};
      await readSSE(res, {
        token: (p) => {
          const chunk = p.text || "";
          const box = $("chatMessages");
          const stick = chatStick(box);
          if (pinGridDone) {
            followAcc += chunk;
            if (!followBody) followBody = appendBubble("assistant", "");
            setChatBody(followBody, followAcc, true);
          } else {
            acc += chunk;
            if (compareRoot && !compareRoot.querySelector(".chat-compare-row")) {
              const fallback = compareRoot.querySelector(".chat-compare-fallback");
              if (fallback) {
                fallback.classList.remove("hidden");
                setChatBody(fallback, acc, true);
              }
            } else if (assistantBody) {
              setChatBody(assistantBody, acc, true);
            }
          }
          if (stick) scrollChat(box, true);
        },
        compare_start: (p) => {
          const cmd = p.command || (isMultiple ? "multiple" : "all");
          if (!compareRoot) compareRoot = appendCompareShell(cmd);
          else {
            const role = compareRoot.querySelector(".chat-role");
            if (role) {
              role.textContent = cmd === "multiple" ? "Selected models" : "All models";
            }
            compareRoot.dataset.compareCommand = cmd;
          }
          const fallback = compareRoot.querySelector(".chat-compare-fallback");
          if (fallback) fallback.classList.add("hidden");
          const rows = compareRoot.querySelector(".chat-compare-rows");
          if (!rows) return;
          rows.innerHTML = "";
          for (const bot of p.bots || []) {
            const key = compareBotKey(bot);
            compareAcc[key] = "";
            rows.appendChild(makeCompareRow(bot, true));
          }
        },
        compare_token: (p) => {
          const key = compareBotKey(p);
          if (!key || !compareRoot) return;
          compareAcc[key] = (compareAcc[key] || "") + (p.text || "");
          const row = findCompareRow(compareRoot, p);
          if (!row) return;
          row.classList.remove("pending", "error");
          setChatBody(row.querySelector(".chat-body"), compareAcc[key], true);
          const box = $("chatMessages");
          if (chatStick(box)) scrollChat(box, true);
        },
        compare_error: (p) => {
          if (!compareRoot) return;
          const row = findCompareRow(compareRoot, p);
          if (!row) return;
          row.classList.remove("pending");
          row.classList.add("error");
          setChatBody(row.querySelector(".chat-body"), p.message || "Error", false);
        },
        compare_bot_done: (p) => {
          if (!compareRoot) return;
          const row = findCompareRow(compareRoot, p);
          if (!row) return;
          row.classList.remove("pending");
          const body = row.querySelector(".chat-body");
          if (body && body.classList.contains("is-thinking")) {
            setChatBody(body, "(No reply)", false);
          }
        },
        tool_confirm: (p) => showConfirmCard(p),
        idea_review: (p) => {
          showReviewCard(p);
          scrollChatToEnd();
        },
        pinterest_results: (p) => {
          if (p.hideGrid) return;
          const pins = p.pins || [];
          const body = followBody || assistantBody;
          const bubble = body && body.closest(".chat-bubble");
          if (!bubble) return;
          const q = p.query || "";
          const n = p.count != null ? p.count : pins.length;
          const mode = String(p.mode || "replace").toLowerCase();
          const fromSet = (p.source || "") === "pinterest_set";
          pushActivity(
            bubble,
            fromSet
              ? q
                ? `pinterest_set show “${q}” → ${n} pin(s)`
                : `pinterest_set show → ${n} pin(s)`
              : q
                ? `search_pinterest “${q}” → ${n} pin(s)`
                : `Found ${n} pin(s)`,
            n === 1 ? "Pinterest · 1 pin" : `Pinterest · ${n} pins`
          );
          if (pins.length) {
            pinGridDone = true;
            const all =
              attachPinterestGrid(bubble, pins, {
                merge: mode === "merge",
              }) || pins;
            updatePinterestCountLabel(bubble, all.length);
            const sumN = all.length;
            const panel = bubble.querySelector(":scope > .chat-activity");
            const sum = panel && panel.querySelector(".chat-activity-summary");
            if (sum) {
              sum.textContent =
                sumN === 1 ? "Pinterest · 1 pin" : `Pinterest · ${sumN} pins`;
            }
          }
          scrollChatToEnd();
        },
        tool_result: (p) => {
          handleToolResult(p);
          const r = (p && p.result) || {};
          const body = followBody || assistantBody;
          const bubble = body && body.closest(".chat-bubble");
          if (!bubble) return;
          if (p.name === "search_pinterest") {
            // Discover only — grid updates via pinterest_set / pinterest_results.
            const q = r.query || "";
            const n = r.count != null ? r.count : (r.pins || []).length;
            pushActivity(
              bubble,
              q
                ? `search_pinterest “${q}” → ${n} pin(s)`
                : `search_pinterest → ${n} pin(s)`,
              "Working…"
            );
            return;
          }
          if (p.name === "pinterest_set") {
            const action = String(r.action || "show").toLowerCase();
            if (action === "download") {
              const got =
                r.downloaded != null ? r.downloaded : r.count != null ? r.count : 0;
              pushActivity(
                bubble,
                `pinterest_set download → ${got} video(s)`,
                got ? `Downloaded · ${got}` : "Download"
              );
              return;
            }
            // show — prefer pinterest_results SSE for grid; backfill if needed
            const pins = r.pins || [];
            const mode = String(r.mode || "replace").toLowerCase();
            const q = r.query || "";
            const n = r.count != null ? r.count : pins.length;
            pushActivity(
              bubble,
              q
                ? `pinterest_set show “${q}” → ${n} pin(s)`
                : `pinterest_set show → ${n} pin(s)`,
              n === 1 ? "Pinterest · 1 pin" : `Pinterest · ${n} pins`
            );
            if (pins.length && !pinGridDone) {
              pinGridDone = true;
              const all =
                attachPinterestGrid(bubble, pins, {
                  merge: mode === "merge",
                }) || pins;
              updatePinterestCountLabel(bubble, all.length);
            }
            return;
          }
          if (p.name) {
            pushActivity(
              bubble,
              (p.name || "tool") + (r.ok === false ? " failed" : " done"),
              "Running tools…"
            );
          }
        },
        tool_start: (p) => {
          const name = p.name || "tool";
          const msg = "Running " + name + "…";
          setRunStatus(msg, followBody, assistantBody);
          const body = followBody || assistantBody;
          const bubble = body && body.closest(".chat-bubble");
          if (bubble) {
            let detail = msg;
            const args = p.arguments || p.args || {};
            if (name === "search_pinterest") {
              const q = args.query || "";
              detail = q ? `Searching Pinterest for “${q}”…` : "Searching Pinterest…";
            } else if (name === "pinterest_set") {
              const action = String(args.action || "show").toLowerCase();
              detail =
                action === "download"
                  ? "Downloading Pinterest pins…"
                  : "Updating Pinterest grid…";
            }
            pushActivity(bubble, detail, "Working…");
          }
          if (compareRoot) {
            compareRoot.querySelectorAll(".chat-body.is-thinking").forEach((el) => {
              showThinkingBody(el, msg);
            });
          }
        },
        saved_videos: (p) => downloadSavedSources(p),
        status: (p) => {
          const msg = p.message || "Researching…";
          setRunStatus(msg, followBody, assistantBody);
          const body = followBody || assistantBody;
          const bubble = body && body.closest(".chat-bubble");
          if (bubble) pushActivity(bubble, msg, "Working…");
          if (compareRoot) {
            compareRoot.querySelectorAll(".chat-body.is-thinking").forEach((el) => {
              showThinkingBody(el, msg);
            });
          }
        },
        error: (p) => {
          setStatus(p.message || "Error", true);
          if (assistantBody && (assistantBody.classList.contains("is-thinking") || !acc)) {
            setChatBody(assistantBody, p.message || "Error", false);
          }
          if (compareRoot) {
            if (!compareRoot.querySelector(".chat-compare-row")) {
              const fallback = compareRoot.querySelector(".chat-compare-fallback");
              if (fallback) {
                fallback.classList.remove("hidden");
                setChatBody(fallback, p.message || "Error", false);
              }
            } else {
              clearCompareThinking(compareRoot, p.message || "Error");
            }
          }
        },
        done: () => {
          setStatus("");
          if (assistantBody && assistantBody.classList.contains("is-thinking")) {
            setChatBody(assistantBody, "", false);
          }
          clearCompareThinking(compareRoot);
          const body = followBody || assistantBody;
          const bubble = body && body.closest(".chat-bubble");
          if (bubble) {
            const grid = bubble.querySelector(".chat-pinterest-grid");
            const n = grid && Array.isArray(grid._pins) ? grid._pins.length : 0;
            finishActivity(
              bubble,
              n
                ? n === 1
                  ? "Searched Pinterest · 1 pin"
                  : `Searched Pinterest · ${n} pins`
                : "Done"
            );
          }
          refreshThreads().catch(() => {});
          scheduleUsageRefresh();
          Promise.resolve(syncReviewSlot())
            .catch(() => {})
            .finally(() => scrollChatToEnd());
        },
        caption_pick: () => {
          // /all and /multiple save captions straight into Plan — skip this popup.
        },
        open_plan: (p) => {
          openPlanFromPayload(p || {});
        },
        meta: () => {},
      });
    } catch (err) {
      setStatus(err.message || String(err), true);
      if (assistantBody && (assistantBody.classList.contains("is-thinking") || !assistantBody.textContent)) {
        setChatBody(assistantBody, err.message || String(err), false);
      }
      if (compareRoot) {
        if (!compareRoot.querySelector(".chat-compare-row")) {
          const fallback = compareRoot.querySelector(".chat-compare-fallback");
          if (fallback) {
            fallback.classList.remove("hidden");
            setChatBody(fallback, err.message || String(err), false);
          }
        } else {
          clearCompareThinking(compareRoot, err.message || String(err));
        }
      }
    } finally {
      clearCompareThinking(compareRoot);
      sending = false;
      if ($("chatSendBtn")) $("chatSendBtn").disabled = false;
    }
  }

  function downloadSavedSources(p) {
    const ids = (p && p.sourceIds) || [];
    if (!ids.length) return;
    const q = ids.map(encodeURIComponent).join(",");
    const a = document.createElement("a");
    a.href = withAuth(`/api/sources/zip?ids=${q}`);
    a.download = "videos.zip";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
    if (window.refreshLibraryGrids) window.refreshLibraryGrids();
  }

  function handleToolResult(p) {
    const r = p.result || {};
    // Agent automation builds the plan silently; only open UI when explicitly requested.
    if (r.openPreviewPopup && Array.isArray(r.pairs) && r.pairs.length) {
      openPlanFromPayload(r);
    }
    if (r.ok && p.name === "start_render" && r.batchId) {
      if (r.status === "queued") {
        setStatus(`Render queued #${r.queuePosition || "?"}…`);
      }
      pollBatchThenResults(r.batchId);
    }
    if (r.ok && p.name === "start_tiktok_upload" && (r.uploadId || r.id)) {
      const uid = r.uploadId || r.id;
      if (r.status === "queued") {
        setStatus(`Upload queued #${r.queuePosition || "?"}…`);
      } else {
        setStatus(`Upload started (${uid}).`);
      }
      if (typeof window.startWatchingUpload === "function") {
        window.startWatchingUpload(uid);
      }
    }
    if (r.ok && p.name === "start_carousel_upload" && (r.uploadId || r.id)) {
      const uid = r.uploadId || r.id;
      if (r.status === "queued") {
        setStatus(`Carousel upload queued #${r.queuePosition || "?"}…`);
      } else {
        setStatus(`Carousel upload started (${uid}).`);
      }
      if (typeof window.startWatchingUpload === "function") {
        window.startWatchingUpload(uid);
      }
      refreshPlanTiktokStatus().catch(() => {});
    }
    if (r.ok && p.name === "fetch_spark") {
      const n = (r.sparkCodes || []).length;
      if (r.waited && n) {
        setStatus(`Fetched ${n} Spark code${n === 1 ? "" : "s"}.`);
      } else if (r.job && r.job.id) {
        setStatus(`Spark job started (${r.job.id}).`);
      }
      startSparkPoll();
      refreshPlanSparkStatus().catch(() => {});
    }
    if (r.ok && p.name === "start_facebook_ads" && (r.jobId || r.id)) {
      const fid = r.jobId || r.id;
      setStatus(`Facebook ads job started (${fid}).`);
      if (typeof window.startWatchingFbAds === "function") {
        window.startWatchingFbAds(fid);
      }
    }
    if (r.ok && (p.name === "set_render_plan" || p.name === "set_carousel_plan" || p.name === "preview_plan" || p.name === "list_plans")) {
      loadThreadPlan().catch(() => {});
    }
    if (r.ok && (p.name === "add_source_url" || p.name === "add_source_urls" || p.name === "propose_ideas")) {
      refreshSourcesFromServer().catch(() => {});
    }
  }

  function openPlanFromPayload(payload) {
    previewPairs = Array.isArray(payload.pairs) ? payload.pairs.slice() : [];
    resetPreviewSelection = true;
    // An `open_plan` SSE event is the just-built plan for this chat.  Keep its
    // identity while the modal refreshes its sidebar/status in the background;
    // otherwise that refresh can replace these rows with the previously active
    // plan before the user ever sees the new one.
    if (payload.planId) {
      activePlanId = String(payload.planId);
      activePlanSource = "chat";
    }
    previewPlanMeta = {
      mode: payload.mode || "",
      origin: payload.origin || "",
      modelLabel: payload.modelLabel || "",
      intent: payload.intent || "preview",
      videoCount: payload.videoCount || 0,
      captionCount: payload.captionCount || 0,
      title: payload.planTitle || payload.title || "",
    };
    openPreviewModal().catch((err) => setStatus(err.message || String(err), true));
  }

  function captionPickBoxes() {
    return [...document.querySelectorAll("#chatCaptionPickList input[type=checkbox]")];
  }

  function selectedCaptionTexts() {
    return captionPickBoxes()
      .filter((c) => c.checked)
      .map((c) => c.dataset.text || "")
      .filter(Boolean);
  }

  function updateCaptionPickCount() {
    const boxes = captionPickBoxes();
    const n = boxes.filter((c) => c.checked).length;
    const el = $("chatCaptionPickCount");
    if (el) el.textContent = n + " of " + boxes.length + " selected";
    boxes.forEach((c) => {
      const row = c.closest(".chat-caption-pick-item");
      if (row) row.classList.toggle("is-on", c.checked);
    });
    if ($("chatCaptionPickGo")) $("chatCaptionPickGo").disabled = n < 1;
  }

  function setCaptionPickChecked(on) {
    captionPickBoxes().forEach((c) => {
      c.checked = !!on;
    });
    updateCaptionPickCount();
  }

  function closeCaptionPick() {
    showModal("chatCaptionPickModal", false);
    const status = $("chatCaptionPickAiStatus");
    if (status) status.textContent = "";
  }

  function fillCaptionPickModelSelect(prefer) {
    const prov = $("chatCaptionPickProvider");
    const mod = $("chatCaptionPickModel");
    if (!prov || !mod) return;
    const p = prov.value;
    mod.innerHTML = "";
    const list = modelsCatalog.filter((m) => m.provider === p);
    for (const m of list) {
      const opt = document.createElement("option");
      opt.value = m.id;
      opt.textContent = m.label + (m.enabled ? "" : " (off)");
      opt.disabled = !m.enabled;
      mod.appendChild(opt);
    }
    if (prefer && [...mod.options].some((o) => o.value === prefer && !o.disabled)) {
      mod.value = prefer;
    } else {
      const remembered = preferredModelFor(p);
      if (remembered && [...mod.options].some((o) => o.value === remembered && !o.disabled)) {
        mod.value = remembered;
      } else {
        const first = [...mod.options].find((o) => !o.disabled);
        if (first) mod.value = first.value;
      }
    }
  }

  function fillCaptionPickSelects() {
    const prov = $("chatCaptionPickProvider");
    const mod = $("chatCaptionPickModel");
    if (!prov || !mod) return;
    const prevP = prov.value;
    const prevM = mod.value;
    const chatP = $("chatProvider") && $("chatProvider").value;
    const chatM = selectedModel();
    const providers = [...new Set(modelsCatalog.map((m) => m.provider))];
    const labels = { anthropic: "Claude", openai: "OpenAI", xai: "Grok", gemini: "Gemini" };
    prov.innerHTML = "";
    for (const p of providers) {
      const enabled = modelsCatalog.some((m) => m.provider === p && m.enabled);
      const opt = document.createElement("option");
      opt.value = p;
      opt.textContent = (labels[p] || p) + (enabled ? "" : " (not configured)");
      opt.disabled = !enabled;
      prov.appendChild(opt);
    }
    const preferP = prevP || chatP || modelPrefs.provider;
    if (preferP && [...prov.options].some((o) => o.value === preferP && !o.disabled)) {
      prov.value = preferP;
    } else {
      const first = [...prov.options].find((o) => !o.disabled);
      if (first) prov.value = first.value;
    }
    fillCaptionPickModelSelect(prevM || preferredModelFor(prov.value) || chatM);
  }

  function openCaptionPick(payload) {
    const items = (payload && payload.items) || [];
    if (!items.length || !$("chatCaptionPickModal")) return;
    captionPickItems = items;
    captionPickPrompt = (payload && payload.prompt) || "";
    captionPickOrigin =
      (payload && payload.origin) === "/multiple" ? "/multiple" : "/all";
    fillCaptionPickSelects();
    const aiBtn = $("chatCaptionPickAiBtn");
    if (aiBtn) aiBtn.disabled = !modelsCatalog.some((m) => m.enabled);
    const nEl = $("chatCaptionPickN");
    if (nEl) {
      const def = Math.min(8, items.length);
      nEl.max = String(items.length);
      nEl.value = String(Math.max(1, def));
    }
    const list = $("chatCaptionPickList");
    list.innerHTML = "";
    items.forEach((item, idx) => {
      const row = document.createElement("label");
      row.className = "check chat-caption-pick-item is-on";
      row.innerHTML = `
        <input type="checkbox" checked />
        <span class="chat-caption-pick-text"></span>`;
      const box = row.querySelector("input");
      box.dataset.text = item.text || "";
      box.dataset.index = String(idx);
      const textEl = row.querySelector(".chat-caption-pick-text");
      textEl.appendChild(document.createTextNode(item.text || ""));
      const srcs = (item.sources || []).filter(Boolean);
      if (srcs.length) {
        const src = document.createElement("span");
        src.className = "chat-caption-pick-src";
        src.textContent = srcs.join(" · ");
        textEl.appendChild(src);
      }
      box.addEventListener("change", updateCaptionPickCount);
      list.appendChild(row);
    });
    const status = $("chatCaptionPickAiStatus");
    if (status) status.textContent = "";
    updateCaptionPickCount();
    showModal("chatCaptionPickModal", true);
  }

  async function applyCaptionPickAi() {
    const status = $("chatCaptionPickAiStatus");
    const btn = $("chatCaptionPickAiBtn");
    const captions = captionPickItems.map((i) => i.text).filter(Boolean);
    const want = Math.max(1, Math.min(Number($("chatCaptionPickN").value) || 8, captions.length));
    const model = ($("chatCaptionPickModel") && $("chatCaptionPickModel").value) || "";
    const pickProvider = $("chatCaptionPickProvider") && $("chatCaptionPickProvider").value;
    rememberSelection(pickProvider, model);
    if (captions.length < 2) return;
    if (status) {
      status.textContent = "Picking favorites…";
      status.style.color = "";
    }
    if (btn) btn.disabled = true;
    try {
      const res = await api("/api/chat/pick-captions", {
        method: "POST",
        body: JSON.stringify({
          captions,
          count: want,
          model,
          prompt: captionPickPrompt,
          threadId: activeThreadId || "",
        }),
      });
      const indexes = new Set((res.indexes || []).map((n) => Number(n)));
      captionPickBoxes().forEach((c) => {
        c.checked = indexes.has(Number(c.dataset.index));
      });
      updateCaptionPickCount();
      scheduleUsageRefresh();
      const n = selectedCaptionTexts().length;
      if (status) {
        status.textContent = n
          ? (res.label || "Model") + " picked " + n + " favorite" + (n === 1 ? "" : "s") + "."
          : "No favorites returned.";
      }
    } catch (err) {
      if (status) {
        status.textContent = err.message || String(err);
        status.style.color = "var(--danger)";
      }
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function proceedCaptionPick() {
    const boxes = captionPickBoxes().filter((c) => c.checked);
    const selected = boxes
      .map((c) => c.dataset.text || "")
      .filter(Boolean);
    if (!selected.length) return;
    const meta = boxes.map((c) => {
      const item = captionPickItems[Number(c.dataset.index)] || {};
      const sources = (item.sources || []).filter(Boolean);
      const multiOrigin =
        captionPickOrigin === "/multiple" ? "/multiple" : "/all";
      return {
        text: item.text || c.dataset.text || "",
        sources,
        model: sources.length === 1 ? sources[0] : sources.join(" · "),
        origin: sources.length > 1 ? multiOrigin : sources.length === 1 ? multiOrigin : "chat",
      };
    }).filter((m) => m.text);
    const go = $("chatCaptionPickGo");
    if (go) go.disabled = true;
    try {
      if (!activeThreadId) {
        setStatus("Open a chat first.", true);
        return;
      }
      await api(`/api/chat/threads/${activeThreadId}/plan`, {
        method: "PUT",
        body: JSON.stringify({
          captions: selected.join("\n"),
          captionMeta: meta,
        }),
      });
      await loadThreadPlan();
      closeCaptionPick();
      setStatus(
        "Saved " +
          selected.length +
          " caption" +
          (selected.length === 1 ? "" : "s") +
          " to this chat’s plan. Use Plan → Send to Create when you want them on Create."
      );
    } catch (err) {
      setStatus(err.message || String(err), true);
    } finally {
      if (go) go.disabled = false;
    }
  }

  function clearConfirmSlot() {
    const slot = $("chatConfirmSlot");
    if (slot) slot.innerHTML = "";
  }

  function showConfirmCard(pending) {
    const slot = $("chatConfirmSlot");
    if (!slot) return;
    clearReviewSlot();
    slot.innerHTML = `
      <div class="chat-confirm-card">
        <h3>Confirm action</h3>
        <pre class="chat-confirm-summary"></pre>
        <div class="row gap wrap">
          <button type="button" class="btn primary" data-act="yes">Confirm</button>
          <button type="button" class="btn danger" data-act="no">Reject</button>
        </div>
      </div>`;
    slot.querySelector(".chat-confirm-summary").textContent =
      pending.summary || pending.name || "";
    slot.querySelector('[data-act="yes"]').addEventListener("click", () =>
      respondConfirm(true)
    );
    slot.querySelector('[data-act="no"]').addEventListener("click", () =>
      respondConfirm(false)
    );
  }

  async function respondConfirm(accept) {
    if (!activeThreadId) return;
    clearConfirmSlot();
    if (!accept) {
      setStatus("Declined");
      return;
    }
    const assistantBody = appendBubble("assistant", "", "Assistant", "Running…");
    try {
      const res = await fetch(`/api/chat/threads/${activeThreadId}/confirm`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await authHeaders()),
        },
        body: JSON.stringify({ accept }),
      });
      if (!res.ok) throw new Error(res.statusText);
      let acc = "";
      await readSSE(res, {
        token: (p) => {
          acc += p.text || "";
          setChatBody(assistantBody, acc, true);
        },
        tool_result: (p) => handleToolResult(p),
        tool_start: (p) => setRunStatus("Running " + (p.name || "tool") + "…", assistantBody),
        tool_confirm: (p) => showConfirmCard(p),
        idea_review: (p) => {
          showReviewCard(p);
          scrollChatToEnd();
        },
        error: (p) => {
          setStatus(p.message || "Error", true);
          if (assistantBody && (assistantBody.classList.contains("is-thinking") || !acc)) {
            setChatBody(assistantBody, p.message || "Error", false);
          }
        },
        done: () => {
          setStatus("");
          if (assistantBody && assistantBody.classList.contains("is-thinking")) {
            setChatBody(assistantBody, "", false);
          }
          refreshThreads().catch(() => {});
          scheduleUsageRefresh();
          Promise.resolve(syncReviewSlot())
            .catch(() => {})
            .finally(() => scrollChatToEnd());
        },
      });
    } catch (err) {
      setStatus(err.message || String(err), true);
      if (assistantBody && assistantBody.classList.contains("is-thinking")) {
        setChatBody(assistantBody, err.message || String(err), false);
      }
    }
  }

  function clearReviewSlot() {
    liveReview = null;
    if (persistReviewTimer) {
      clearTimeout(persistReviewTimer);
      persistReviewTimer = 0;
    }
    const slot = $("chatReviewSlot");
    if (slot) {
      if (window.stopPinterestPreview) {
        slot.querySelectorAll(".chat-review-idea").forEach((el) => {
          window.stopPinterestPreview(el);
        });
      }
      slot.innerHTML = "";
    }
  }

  function reviewRoundKey(pending) {
    return (pending.roundIdeas || []).map((i) => i.id || "").join(",");
  }

  async function syncReviewSlot() {
    if (!activeThreadId) return;
    try {
      const thread = await api(`/api/chat/threads/${activeThreadId}`);
      const pending = thread.pendingReview;
      if (pending && (pending.roundIdeas || []).length) {
        const key = reviewRoundKey(pending);
        const liveKey = liveReview && liveReview.round
          ? liveReview.round.map((i) => i.id || "").join(",")
          : "";
        if (!liveReview || liveKey !== key) showReviewCard(pending);
      } else {
        clearReviewSlot();
      }
    } catch {}
  }

  function snapshotReviewChoices() {
    if (!liveReview || !(liveReview.round || []).length) {
      return { approvedIds: [], deniedIds: [] };
    }
    const round = liveReview.round;
    const choices = liveReview.choices || {};
    return {
      approvedIds: round.filter((i) => choices[i.id] !== "no").map((i) => i.id),
      deniedIds: round.filter((i) => choices[i.id] === "no").map((i) => i.id),
    };
  }

  function persistReviewChoices() {
    if (!activeThreadId || !liveReview || sending) return;
    const snap = snapshotReviewChoices();
    api(`/api/chat/threads/${activeThreadId}/review-choices`, {
      method: "POST",
      body: JSON.stringify({
        approvedIds: snap.approvedIds,
        deniedIds: snap.deniedIds,
      }),
    }).catch(() => {});
  }

  function persistReviewSoon() {
    if (persistReviewTimer) clearTimeout(persistReviewTimer);
    persistReviewTimer = setTimeout(() => {
      persistReviewTimer = 0;
      persistReviewChoices();
    }, 120);
  }

  function showReviewCard(pending) {
    const slot = $("chatReviewSlot");
    if (!slot) return;
    if (!(pending && (pending.roundIdeas || []).length)) {
      liveReview = null;
      slot.innerHTML = "";
      return;
    }
    clearConfirmSlot();
    const target = Number(pending.targetCount || 0) || 0;
    const kept = pending.approved || [];
    const round = pending.roundIdeas || [];
    const saved = pending.roundChoices || {};
    const choices = {};
    round.forEach((idea) => {
      choices[idea.id] = saved[idea.id] === "no" ? "no" : "yes";
    });
    liveReview = { round, choices };

    if (window.stopPinterestPreview) {
      slot.querySelectorAll(".chat-review-idea").forEach((el) => {
        window.stopPinterestPreview(el);
      });
    }

    const render = () => {
      const roundApproved = round.filter((i) => choices[i.id] !== "no").length;
      const totalApproved = kept.length + roundApproved;
      const need = Math.max(0, target - totalApproved);
      const btnLabel = need > 0
        ? `Continue — find ${need} more`
        : `Use these ${totalApproved}`;
      slot.querySelector(".chat-review-count").textContent =
        `${totalApproved} of ${target} selected`;
      const go = slot.querySelector("[data-act=go]");
      if (go) go.textContent = btnLabel;
      const proceed = slot.querySelector("[data-act=proceed]");
      if (proceed) {
        proceed.textContent = `Render and upload ${totalApproved} you selected`;
        proceed.disabled = totalApproved < 1;
      }
      const save = slot.querySelector("[data-act=save]");
      if (save) {
        save.textContent = `Download and save ${totalApproved} you selected`;
        save.disabled = totalApproved < 1;
      }
    };

    slot.innerHTML = `
      <div class="chat-review-card">
        <h3>Review videos</h3>
        <p class="chat-review-count"></p>
        <p class="hint">Click a clip to select or deselect. Hover to preview. Use Approve all / Deny all for the whole batch. Download and save keeps the original clips without captions or TikTok upload.</p>
        <div class="chat-review-kept"></div>
        <div class="chat-review-round"></div>
        <textarea class="chat-review-feedback" rows="2" placeholder="Optional feedback for rewrite / new video…"></textarea>
        <div class="row gap wrap chat-review-toolbar">
          <button type="button" class="btn" data-act="yes-all">Approve all</button>
          <button type="button" class="btn danger" data-act="no-all">Deny all</button>
          <button type="button" class="btn" data-act="rewrite-all">Rewrite all</button>
          <button type="button" class="btn primary" data-act="go">Continue</button>
          <button type="button" class="btn primary" data-act="proceed">Render and upload 0 you selected</button>
          <button type="button" class="btn" data-act="save">Download and save 0 you selected</button>
        </div>
      </div>`;
    const feedbackEl = slot.querySelector(".chat-review-feedback");
    const keptEl = slot.querySelector(".chat-review-kept");
    if (kept.length) {
      keptEl.innerHTML = `<p class="hint">Already selected</p>`;
      kept.forEach((idea) => {
        keptEl.appendChild(reviewIdeaEl(idea, "kept"));
      });
    }
    const roundEl = slot.querySelector(".chat-review-round");
    const ideaEls = {};
    const paintChoice = (idea, val) => {
      choices[idea.id] = val === "no" ? "no" : "yes";
      const el = ideaEls[idea.id];
      if (!el) return;
      el.classList.toggle("approved", choices[idea.id] !== "no");
      el.classList.toggle("denied", choices[idea.id] === "no");
      el.classList.toggle("selected", choices[idea.id] !== "no");
      el.setAttribute(
        "aria-pressed",
        choices[idea.id] !== "no" ? "true" : "false"
      );
    };
    const applyChoice = (idea, val) => {
      paintChoice(idea, val);
      persistReviewSoon();
      render();
    };
    const toggleChoice = (idea) => {
      applyChoice(idea, choices[idea.id] === "no" ? "yes" : "no");
    };
    round.forEach((idea) => {
      const el = reviewIdeaEl(idea, "round");
      ideaEls[idea.id] = el;
      el.addEventListener("click", (ev) => {
        // Ignore clicks that are only meant for nested controls (none now).
        if (ev.target.closest("button, a, textarea, input")) return;
        toggleChoice(idea);
      });
      el.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" || ev.key === " ") {
          ev.preventDefault();
          toggleChoice(idea);
        }
      });
      roundEl.appendChild(el);
      paintChoice(idea, choices[idea.id]);
    });
    slot.querySelector("[data-act=yes-all]").addEventListener("click", () => {
      round.forEach((idea) => paintChoice(idea, "yes"));
      persistReviewSoon();
      render();
    });
    slot.querySelector("[data-act=no-all]").addEventListener("click", () => {
      round.forEach((idea) => paintChoice(idea, "no"));
      persistReviewSoon();
      render();
    });
    slot.querySelector("[data-act=rewrite-all]").addEventListener("click", () => {
      respondReview({
        action: "rewrite",
        ideaIds: [],
        feedback: (feedbackEl && feedbackEl.value) || "",
      });
    });
    slot.querySelector("[data-act=go]").addEventListener("click", () => {
      const snap = snapshotReviewChoices();
      respondReview({ action: "submit", approvedIds: snap.approvedIds, deniedIds: snap.deniedIds });
    });
    slot.querySelector("[data-act=proceed]").addEventListener("click", () => {
      const snap = snapshotReviewChoices();
      const n = (liveReview && (liveReview.round || []).length)
        ? kept.length + snap.approvedIds.length
        : snap.approvedIds.length;
      if (n < 1) return;
      respondReview({
        action: "proceed",
        approvedIds: snap.approvedIds,
        deniedIds: snap.deniedIds,
      });
    });
    slot.querySelector("[data-act=save]").addEventListener("click", () => {
      const snap = snapshotReviewChoices();
      const n = (liveReview && (liveReview.round || []).length)
        ? kept.length + snap.approvedIds.length
        : snap.approvedIds.length;
      if (n < 1) return;
      respondReview({
        action: "save",
        approvedIds: snap.approvedIds,
        deniedIds: snap.deniedIds,
      });
    });
    render();
    persistReviewSoon();
    scrollChatToEnd();
  }

  function reviewIdeaEl(idea, kind) {
    const el = document.createElement("div");
    const pin = {
      url: idea.pinUrl || "",
      title: idea.title || "",
      thumbnail: idea.thumbnail || "",
      previewUrl: idea.previewUrl || "",
      previewKind: idea.previewKind || "",
      duration: idea.duration || "",
      width: idea.width,
      height: idea.height,
    };
    const landscape = Number(pin.width) > Number(pin.height);
    const selectable = kind === "round";
    el.className =
      "chat-review-idea" +
      (kind === "kept" ? " kept" : " round-pick") +
      (landscape ? " landscape" : "") +
      (selectable ? " is-selectable" : "");
    el.dataset.id = idea.id || "";
    if (selectable) {
      el.setAttribute("role", "button");
      el.tabIndex = 0;
    }
    const dur = pin.duration
      ? window.fmtDuration
        ? window.fmtDuration(pin.duration)
        : String(pin.duration)
      : "";
    el.innerHTML = `
      <div class="pinterest-card-media${landscape ? " is-landscape" : ""}">
        <img alt="" />
      </div>
      <div class="chat-review-idea-body">
        <div class="chat-review-title"></div>
        <div class="chat-review-caption"></div>
        ${dur ? `<div class="chat-review-meta"></div>` : ""}
        ${kind === "kept" ? `<span class="hint">Kept</span>` : ""}
      </div>`;
    const img = el.querySelector("img");
    if (idea.thumbnail) img.src = idea.thumbnail;
    el.querySelector(".chat-review-title").textContent = idea.title || idea.pinUrl || "";
    el.querySelector(".chat-review-caption").textContent = idea.caption || "";
    const meta = el.querySelector(".chat-review-meta");
    if (meta) meta.textContent = dur;
    if (pin.previewUrl && window.bindPinterestHover) {
      window.bindPinterestHover(el, pin);
    }
    return el;
  }

  async function respondReview(opts) {
    if (!activeThreadId) return;
    const action = opts.action || "submit";
    clearReviewSlot();
    const thinkingText =
      action === "proceed"
        ? "Preparing selected ideas…"
        : action === "save"
          ? "Downloading selected videos…"
          : action === "submit"
            ? "Updating ideas…"
            : "Revising ideas…";
    const assistantBody = appendBubble("assistant", "", "Assistant", thinkingText);
    try {
      const res = await fetch(`/api/chat/threads/${activeThreadId}/review`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(await authHeaders()),
        },
        body: JSON.stringify({
          action,
          approvedIds: opts.approvedIds || [],
          deniedIds: opts.deniedIds || [],
          ideaIds: opts.ideaIds || [],
          feedback: opts.feedback || "",
        }),
      });
      if (!res.ok) throw new Error(res.statusText);
      let acc = "";
      await readSSE(res, {
        token: (p) => {
          acc += p.text || "";
          setChatBody(assistantBody, acc, true);
        },
        tool_result: (p) => handleToolResult(p),
        tool_start: (p) => setRunStatus("Running " + (p.name || "tool") + "…", assistantBody),
        saved_videos: (p) => downloadSavedSources(p),
        tool_confirm: (p) => showConfirmCard(p),
        idea_review: (p) => {
          showReviewCard(p);
          scrollChatToEnd();
        },
        error: (p) => {
          setStatus(p.message || "Error", true);
          if (assistantBody && (assistantBody.classList.contains("is-thinking") || !acc)) {
            setChatBody(assistantBody, p.message || "Error", false);
          }
        },
        done: () => {
          setStatus("");
          if (assistantBody && assistantBody.classList.contains("is-thinking")) {
            setChatBody(assistantBody, "", false);
          }
          refreshThreads().catch(() => {});
          scheduleUsageRefresh();
          refreshSourcesFromServer().catch(() => {});
          Promise.resolve(syncReviewSlot())
            .catch(() => {})
            .finally(() => scrollChatToEnd());
        },
      });
    } catch (err) {
      setStatus(err.message || String(err), true);
      if (assistantBody && assistantBody.classList.contains("is-thinking")) {
        setChatBody(assistantBody, err.message || String(err), false);
      }
    }
  }

  // ----- Popups -----
  function canFineHover() {
    return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
  }

  function hidePlanHover() {
    planHoverGen += 1;
    planHoverId = "";
    if (planHoverTimer) {
      clearTimeout(planHoverTimer);
      planHoverTimer = 0;
    }
    const pop = $("chatPlanHover");
    const vid = $("chatPlanHoverVideo");
    if (pop) {
      pop.classList.add("hidden");
      pop.setAttribute("aria-hidden", "true");
    }
    if (vid) {
      try { vid.pause(); } catch {}
      vid.removeAttribute("src");
      vid.removeAttribute("poster");
      try { vid.load(); } catch {}
    }
  }

  function placePlanHover(anchor) {
    const pop = $("chatPlanHover");
    if (!pop || !anchor) return;
    const r = anchor.getBoundingClientRect();
    const w = 148;
    const h = Math.round((w * 16) / 9);
    let left = r.right + 8;
    let top = r.top + r.height / 2 - h / 2;
    if (left + w > window.innerWidth - 12) left = r.left - w - 8;
    if (left < 12) left = 12;
    if (top < 12) top = 12;
    if (top + h > window.innerHeight - 12) top = Math.max(12, window.innerHeight - h - 12);
    pop.style.left = Math.round(left) + "px";
    pop.style.top = Math.round(top) + "px";
  }

  async function showPlanHover(cell, sourceId) {
    if (!sourceId || !canFineHover()) return;
    const pop = $("chatPlanHover");
    const vid = $("chatPlanHoverVideo");
    if (!pop || !vid) return;
    const gen = ++planHoverGen;
    placePlanHover(cell);
    pop.classList.remove("hidden");
    pop.setAttribute("aria-hidden", "false");
    if (planHoverId === sourceId && vid.currentSrc) {
      try { vid.currentTime = 0; } catch {}
      vid.play().catch(() => {});
      return;
    }
    planHoverId = sourceId;
    await freshAuthUrl(`/api/sources/${encodeURIComponent(sourceId)}/media`);
    if (gen !== planHoverGen) return;
    vid.poster = withAuth(`/api/sources/${encodeURIComponent(sourceId)}/frame`);
    vid.src = withAuth(`/api/sources/${encodeURIComponent(sourceId)}/media`);
    vid.muted = true;
    vid.loop = true;
    vid.play().catch(() => {});
  }

  function bindPlanVideoHover(cell, sourceId) {
    if (!cell || !sourceId || !canFineHover()) return;
    cell.classList.add("has-preview");
    cell.addEventListener("pointerenter", () => {
      if (planHoverTimer) clearTimeout(planHoverTimer);
      planHoverTimer = setTimeout(() => {
        showPlanHover(cell, sourceId).catch(() => {});
      }, 80);
    });
    cell.addEventListener("pointerleave", () => {
      if (planHoverTimer) clearTimeout(planHoverTimer);
      planHoverTimer = setTimeout(hidePlanHover, 140);
    });
  }

  function initPlanHover() {
    const pop = $("chatPlanHover");
    if (!pop || pop.dataset.bound) return;
    pop.dataset.bound = "1";
    pop.addEventListener("pointerenter", () => {
      if (planHoverTimer) clearTimeout(planHoverTimer);
    });
    pop.addEventListener("pointerleave", () => {
      if (planHoverTimer) clearTimeout(planHoverTimer);
      planHoverTimer = setTimeout(hidePlanHover, 80);
    });
    const wrap = document.querySelector("#chatPreviewModal .chat-plan-table-wrap");
    if (wrap) wrap.addEventListener("scroll", hidePlanHover, { passive: true });
  }

  function pausePreviewVideos() {
    hidePlanHover();
  }
  window.pauseChatPreviewMedia = pausePreviewVideos;

  function showModal(id, on) {
    const el = $(id);
    if (!el) return;
    if (!on && id === "chatPreviewModal") {
      hidePlanHover();
      clearCreatePlanIntent();
    }
    el.classList.toggle("hidden", !on);
    if (typeof window.syncModalOpen === "function") window.syncModalOpen();
  }

  function createPlanConfirmLabel(intent) {
    if (intent === "sparks") return "Confirm · Render → Upload → Sparks";
    if (intent === "upload") return "Confirm · Render → Upload";
    return "Confirm · Render";
  }

  function syncCreatePlanConfirmUi() {
    const modal = $("chatPreviewModal");
    const btn = $("createPlanConfirmBtn");
    if (modal) {
      if (createPlanIntent) modal.setAttribute("data-create-intent", createPlanIntent);
      else modal.removeAttribute("data-create-intent");
    }
    if (btn) {
      btn.textContent = createPlanConfirmLabel(createPlanIntent || "render");
      btn.classList.toggle("hidden", !createPlanIntent);
    }
    setPreviewTitle();
  }

  function clearCreatePlanIntent() {
    createPlanIntent = "";
    syncCreatePlanConfirmUi();
  }

  function setCreatePlanIntent(intent) {
    createPlanIntent = intent === "sparks" || intent === "upload" || intent === "render" ? intent : "";
    syncCreatePlanConfirmUi();
    fillPlanAccountSelect().catch(() => {});
  }

  async function openCreatePlanReview(opts = {}) {
    const intent = opts.intent === "sparks" || opts.intent === "upload" ? opts.intent : "render";
    const rawPairs = Array.isArray(opts.pairs) ? opts.pairs : [];
    if (!rawPairs.length) {
      alert(
        intent === "render"
          ? "Add videos and captions before rendering."
          : "Add videos and captions before uploading."
      );
      return;
    }
    const mode = opts.mode || "custom";
    const pairs = rawPairs.map((p) => ({
      sourceId: p.sourceId,
      caption: p.caption,
      sourceName: p.sourceName || "",
      kind: p.kind || undefined,
      slides: p.slides || undefined,
      carouselId: p.carouselId || undefined,
      pinUrl: p.pinUrl || undefined,
    }));
    const isCarouselPlan = pairs.some((p) => p && p.kind === "carousel");
    // Matrix/pair Create reviews must not freeze pairs onto the workspace —
    // otherwise replacing a source leaves stale sourceIds in the next plan.
    // Only carousel / explicit custom locks Create into renderPairs.
    const lockPairsOnWorkspace = isCarouselPlan || mode === "custom";

    // Persist so Plan / Chat can reuse this selection.
    try {
      const nameEl = $("workspacePlanName");
      const planName =
        (nameEl && nameEl.value.trim()) ||
        opts.title ||
        `Create ${intent} ${new Date().toLocaleString()}`;
      const created = await api("/api/plans", {
        method: "POST",
        body: JSON.stringify({
          name: planName,
          renderPairs: pairs,
          selected: pairs,
          activate: true,
        }),
      });
      if (nameEl && !nameEl.value.trim()) nameEl.value = created.name || planName;
      await api("/api/workspace", {
        method: "PUT",
        body: JSON.stringify({
          renderPairs: lockPairsOnWorkspace ? pairs : [],
          mode: isCarouselPlan ? "custom" : mode,
          activePlanId: created.id,
          captions:
            opts.captions != null
              ? opts.captions
              : ($("captionsInput") && $("captionsInput").value) || "",
        }),
      });
      activePlanId = created.id || activePlanId;
      activePlanSource = "workspace";
      if (typeof window.setWorkspaceRenderPairs === "function") {
        window.setWorkspaceRenderPairs(lockPairsOnWorkspace ? pairs : [], {
          clearOnSave: !lockPairsOnWorkspace,
        });
      }
      refreshWorkspacePlansSidebar().catch(() => {});
    } catch {
      /* still open the review UI even if plan save fails */
    }

    previewPairs = pairs.map((p) => ({ ...p }));
    resetPreviewSelection = true;
    previewPlanMeta = {
      ...previewPlanMeta,
      mode: isCarouselPlan ? "carousel" : mode === "custom" ? "" : mode,
      origin: "create",
      intent,
      modelLabel: "",
      videoCount: isCarouselPlan
        ? pairs.length
        : new Set(pairs.map((p) => p.sourceId)).size,
      captionCount: new Set(pairs.map((p) => p.caption)).size,
      title: opts.title || "",
    };
    activePlanSource = "workspace";
    setCreatePlanIntent(intent);

    // Auto-execute: skip modal and directly start the action
    if (opts.autoExecute) {
      try {
        if (intent === "sparks") {
          // For sparks, we need to ensure an account is selected
          await fillPlanAccountSelect();
          if (!planUploadAccountId()) {
            alert("No DuoPlus-linked TikTok accounts available for Sparks upload.");
            return;
          }
        }
        if (intent === "render") {
          await makeSelectedFromPreview();
        } else {
          await uploadSelectedFromPreview(intent === "sparks" ? "sparks" : "upload", pairs);
        }
      } catch (err) {
        alert(err.message || String(err));
      }
      return;
    }

    showModal("chatPreviewModal", true);
    setPlanSidebarCollapsed(true);
    fillPlanAccountSelect().catch(() => {});
    paintPreviewModal();
    // Select every row by default so Confirm acts on the full plan.
    document
      .querySelectorAll("#chatPreviewTableBody input[type=checkbox]")
      .forEach((c) => {
        c.checked = isPreviewPairSelectable(previewPairs[Number(c.dataset.idx)]);
      });
    updatePreviewSelectAllState();
    schedulePreviewEstimate();
    loadDuoPlusFlag().then(() => {
      updatePreviewActionButtons(previewPlanStats(selectedPreviewPairObjects()));
    });
    refreshPlanTiktokStatus()
      .then(() => {
        paintPreviewStatus();
        document
          .querySelectorAll("#chatPreviewTableBody input[type=checkbox]")
          .forEach((c) => {
            c.checked = isPreviewPairSelectable(previewPairs[Number(c.dataset.idx)]);
          });
        updatePreviewSelectAllState();
        schedulePreviewEstimate();
      })
      .catch(() => {});
  }

  async function confirmCreatePlanIntent() {
    if (!createPlanIntent) return;
    const intent = createPlanIntent;
    const selected = selectedPreviewPairObjects();
    if (!selected.length) {
      alert("Select at least one row.");
      return;
    }
    if (intent === "sparks" && !planUploadAccountId()) {
      alert("Pick a DuoPlus-linked account under Post as (Sparks login).");
      return;
    }
    const btn = $("createPlanConfirmBtn");
    if (btn) btn.disabled = true;
    try {
      if (intent === "render") {
        await makeSelectedFromPreview();
      } else {
        await uploadSelectedFromPreview(intent === "sparks" ? "sparks" : "upload");
      }
    } catch (err) {
      alert(err.message || String(err));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function freshAuthUrl(path) {
    try {
      if (window.__auth && window.__auth.getToken) await window.__auth.getToken();
    } catch {}
    return withAuth(path);
  }

  async function loadAuthBlobUrl(path) {
    const res = await fetch(path, { headers: await authHeaders() });
    if (!res.ok) throw new Error(res.statusText || "Failed to load media");
    return URL.createObjectURL(await res.blob());
  }

  async function refreshSourcesFromServer() {
    // Chat uses the library; do not rewrite Create's in-job source list.
    try {
      const list = await api("/api/library");
      chatLibrarySources = Array.isArray(list) ? list : [];
    } catch {
      chatLibrarySources = [];
    }
    renderChatSourceList();
    if (window.refreshLibraryGrids) window.refreshLibraryGrids();
  }

  function chatSourcesForUi() {
    const byId = {};
    for (const s of chatLibrarySources || []) {
      if (s && s.id) byId[s.id] = s;
    }
    const job = (window.getSources && window.getSources()) || [];
    for (const s of job) {
      if (s && s.id && !byId[s.id]) byId[s.id] = s;
    }
    return Object.values(byId);
  }

  function renderChatSourceList() {
    const list = $("chatSourceList");
    if (!list) return;
    const sources = chatSourcesForUi();
    list.innerHTML = "";
    if (!sources.length) {
      list.innerHTML = '<li class="hooks-empty">No videos yet.</li>';
      return;
    }
    for (const src of sources) {
      const li = document.createElement("li");
      li.className = "source-item";
      li.innerHTML = `<span class="name"></span><span class="meta"></span>`;
      li.querySelector(".name").textContent = src.name;
      li.querySelector(".meta").textContent =
        `${src.width || "?"}×${src.height || "?"} · ${src.duration || "?"}s`;
      li.addEventListener("click", () => fillChatEndCap(src));
      list.appendChild(li);
    }
  }
  window.renderChatSourceList = renderChatSourceList;
  window.refreshChatSources = refreshSourcesFromServer;

  function setChatVideosTab(tab) {
    const add = $("chatVideosAddPane");
    const lib = $("chatVideosLibraryPane");
    document.querySelectorAll("#chatVideosModal .video-tab").forEach((btn) => {
      const on = btn.dataset.videosTab === tab;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (add) add.classList.toggle("hidden", tab !== "add");
    if (lib) lib.classList.toggle("hidden", tab !== "library");
    if (tab === "library" && window.showChatLibraryTab) {
      window.showChatLibraryTab();
    }
  }

  function fillChatEndCap(src) {
    const body = $("chatVideoDetailBody");
    const empty = $("chatVideoDetailEmpty");
    const detail = $("chatVideoDetail");
    if (!body) return;
    detail.classList.remove("empty");
    empty.classList.add("hidden");
    body.classList.remove("hidden");
    body.dataset.sourceId = src.id;
    const ec = src.endCaption || {};
    $("chatEndCapEnabled").checked = !!ec.enabled;
    $("chatEndCapText").value = ec.text || "";
    const dur = Math.min(5, Math.max(2, Number(ec.durationSeconds) || 3));
    $("chatEndCapDuration").value = dur;
    $("chatEndCapDurationOut").textContent = `${dur}s`;
  }

  async function persistChatEndCap() {
    const body = $("chatVideoDetailBody");
    const sid = body && body.dataset.sourceId;
    if (!sid) return;
    const payload = {
      enabled: !!$("chatEndCapEnabled").checked,
      text: ($("chatEndCapText").value || "").trim(),
      durationSeconds: Number($("chatEndCapDuration").value) || 3,
      xOffset: 0,
      yOffset: 0,
    };
    await api(`/api/sources/${sid}/end-caption`, {
      method: "PUT",
      body: JSON.stringify(payload),
    });
    await refreshSourcesFromServer();
  }

  function planFromToolPayload(data) {
    if (!data || !data.ok) return null;
    let pairs = Array.isArray(data.pairs) ? data.pairs : Array.isArray(data.renderPairs) ? data.renderPairs : [];
    if (!pairs.length && Array.isArray(data.videos)) {
      pairs = [];
      for (const v of data.videos) {
        if (!v || !v.sourceId) continue;
        const caps = Array.isArray(v.captions) ? v.captions : v.captions ? [v.captions] : [];
        for (const cap of caps) {
          const text = String(cap || "").trim();
          if (text) {
            pairs.push({
              sourceId: v.sourceId,
              sourceName: v.sourceName || v.sourceId,
              caption: text,
            });
          }
        }
      }
    }
    if (!pairs.length) return null;
    const captions = [...new Set(pairs.map((p) => p.caption).filter(Boolean))];
    return {
      renderPairs: pairs,
      captions: captions.join("\n"),
      captionMeta: Array.isArray(data.captionMeta) ? data.captionMeta : [],
      mode: data.mode || "custom",
    };
  }

  function recoverPlanFromThread(thread) {
    if (!thread || typeof thread !== "object") return null;
    const stored = thread.plan && Array.isArray(thread.plan.renderPairs) ? thread.plan.renderPairs : [];
    if (stored.length) return thread.plan;
    const planTools = new Set(["preview_plan", "set_render_plan", "set_carousel_plan"]);
    for (let i = (thread.messages || []).length - 1; i >= 0; i--) {
      const m = thread.messages[i];
      if (m.role !== "tool" || !planTools.has(m.name || "")) continue;
      try {
        const plan = planFromToolPayload(JSON.parse(m.content || "{}"));
        if (plan && plan.renderPairs.length) return plan;
      } catch {
        /* ignore */
      }
    }
    return null;
  }

  function planSidebarCount() {
    return (workspacePlans || []).length + (threadPlans || []).length;
  }

  function setPlanSidebarCollapsed(collapsed) {
    const layout = document.querySelector("#chatPreviewModal .chat-plan-layout");
    const aside = $("chatPlanSidebar");
    const btn = $("chatPlanSidebarToggle");
    const on = !!collapsed;
    if (layout) layout.classList.toggle("is-plans-collapsed", on);
    if (aside) {
      aside.hidden = on;
      aside.setAttribute("aria-hidden", on ? "true" : "false");
    }
    if (btn) {
      btn.setAttribute("aria-expanded", on ? "false" : "true");
      btn.title = on ? "Choose a plan" : "Close plan list";
      const n = planSidebarCount();
      const count = $("chatPlanCount");
      if (count) count.textContent = n ? String(n) : "";
    }
  }

  function applyTiktokCountsFromPairs() {
    previewPlanMeta.tiktokPosted = previewPairs.filter(
      (p) => p.tiktokStatus === "done"
    ).length;
    previewPlanMeta.tiktokFailed = previewPairs.filter(
      (p) => p.tiktokStatus === "failed"
    ).length;
  }

  function applyPlansPayload(data, opts = {}) {
    if (!data || typeof data !== "object") return;
    threadPlans = Array.isArray(data.plans) ? data.plans.slice() : [];
    // Chat endpoints are compatibility aliases for the workspace plan store.
    // Keep one list in the UI even while an older browser still calls them.
    workspacePlans = threadPlans.slice();
    threadPlans.sort(
      (a, b) =>
        (b.updatedAt || b.createdAt || 0) - (a.updatedAt || a.createdAt || 0)
    );
    const chatPlan = data.plan;
    const incomingPlanId = String(data.activePlanId || (chatPlan && chatPlan.id) || "");
    const chatPairs =
      chatPlan && Array.isArray(chatPlan.renderPairs) ? chatPlan.renderPairs : [];
    const keepWorkspace =
      !!opts.keepWorkspacePairs &&
      !chatPairs.length &&
      previewPairs.length &&
      activePlanSource === "workspace";
    // `open_plan` has already provided the exact rows created by this command.
    // A concurrent refresh is only for sidebar/status data, not permission to
    // swap the visible spreadsheet back to an older active plan.
    const keepPreviewPairs = !!opts.keepPairs && previewPairs.length;
    const staleWhileSelecting = pendingPlanId && incomingPlanId && incomingPlanId !== pendingPlanId;
    if (!keepWorkspace && !keepPreviewPairs && !staleWhileSelecting) {
      activePlanId = data.activePlanId || (chatPlan && chatPlan.id) || "";
      activePlanSource = "workspace";
      applyWorkspacePlan(chatPlan);
      if (data.tiktokSummary) {
        previewPlanMeta.tiktokPosted = Number(data.tiktokSummary.posted) || 0;
        previewPlanMeta.tiktokFailed = Number(data.tiktokSummary.failed) || 0;
      } else {
        applyTiktokCountsFromPairs();
      }
    }
    renderPlanSidebar();
    const layout = document.querySelector("#chatPreviewModal .chat-plan-layout");
    const collapsed = !layout || layout.classList.contains("is-plans-collapsed");
    setPlanSidebarCollapsed(collapsed);
  }

  function renderPlanSidebar() {
    const list = $("chatPlanList");
    if (!list) return;
    closeRowMenu();
    list.innerHTML = "";
    const titleEl = $("chatPlanSidebarTitle");
    if (titleEl) {
      titleEl.textContent =
        workspacePlans.length && !threadPlans.length
          ? "Plans"
          : threadPlans.length && !workspacePlans.length
            ? "Chat plans"
            : "Plans";
    }
    const total = planSidebarCount();
    if (!total) {
      const empty = document.createElement("li");
      empty.className = "chat-plan-list-empty";
      empty.textContent = "No plans yet.";
      list.appendChild(empty);
      return;
    }
    for (const p of workspacePlans) {
      const li = document.createElement("li");
      const isActive =
        activePlanSource === "workspace" && p.id === activePlanId;
      li.className = "chat-plan-list-item" + (isActive ? " active" : "");
      li.dataset.workspacePlanId = p.id;
      const main = document.createElement("div");
      main.className = "chat-row-main";
      const title = document.createElement("div");
      title.className = "chat-plan-list-title";
      title.textContent = p.name || p.title || "Plan";
      const meta = document.createElement("div");
      meta.className = "chat-plan-list-meta";
      const bits = ["Create"];
      if (p.pairCount) bits.push(p.pairCount + " pairs");
      else bits.push("Empty");
      meta.textContent = bits.join(" · ");
      main.appendChild(title);
      main.appendChild(meta);
      li.appendChild(main);
      if (isActive) {
        const more = makeMoreBtn("Plan options");
        more.addEventListener("click", (e) => {
          e.stopPropagation();
          openRowMenu(more, [
            {
              label: "Rename",
              icon: "✎",
              action: () => renameWorkspacePlan(p.id, p.name || p.title),
            },
            { divider: true },
            {
              label: "Delete",
              icon: "🗑",
              danger: true,
              action: () => deleteWorkspacePlan(p.id, p.name || p.title),
            },
          ]);
        });
        li.appendChild(more);
      }
      li.addEventListener("click", (e) => {
        if (e.target.closest(".chat-row-more") || e.target.closest(".chat-row-menu")) return;
        selectWorkspacePlan(p.id);
      });
      list.appendChild(li);
    }
    for (const p of threadPlans) {
      if (workspacePlans.some((workspacePlan) => workspacePlan.id === p.id)) continue;
      const li = document.createElement("li");
      const isActive = activePlanSource === "chat" && p.id === activePlanId;
      li.className = "chat-plan-list-item" + (isActive ? " active" : "");
      const main = document.createElement("div");
      main.className = "chat-row-main";
      const title = document.createElement("div");
      title.className = "chat-plan-list-title";
      title.textContent = p.title || "Plan";
      const meta = document.createElement("div");
      meta.className = "chat-plan-list-meta";
      const bits = ["chat"];
      if (p.pairCount) bits.push(p.pairCount + " pairs");
      else bits.push("Empty");
      if (p.videoCount) bits.push(p.videoCount + " videos");
      if (p.tiktokAccountName) bits.push("→ " + p.tiktokAccountName);
      meta.textContent = bits.join(" · ");
      main.appendChild(title);
      main.appendChild(meta);
      li.appendChild(main);
      if (isActive) {
        const more = makeMoreBtn("Plan options");
        more.addEventListener("click", (e) => {
          e.stopPropagation();
          openRowMenu(more, [
            {
              label: "Rename",
              icon: "✎",
              action: () => renameThreadPlan(p.id, p.title),
            },
            { divider: true },
            {
              label: "Delete",
              icon: "🗑",
              danger: true,
              action: () => deleteThreadPlan(p.id, p.title),
            },
          ]);
        });
        li.appendChild(more);
      }
      li.addEventListener("click", (e) => {
        if (e.target.closest(".chat-row-more") || e.target.closest(".chat-row-menu")) return;
        selectThreadPlan(p.id);
      });
      list.appendChild(li);
    }
  }

  async function persistThreadPlan(plan, opts = {}) {
    if (!activeThreadId || !plan || !(plan.renderPairs || []).length) return;
    try {
      await api(`/api/chat/threads/${activeThreadId}/plan`, {
        method: "PUT",
        body: JSON.stringify({
          renderPairs: plan.renderPairs,
          captions: plan.captions || "",
          captionMeta: plan.captionMeta || [],
          mode: plan.mode || "custom",
          newPlan: !!opts.newPlan,
          title: opts.title || plan.title || "",
          planId: opts.planId || activePlanId || "",
          tiktokAccountId: plan.tiktokAccountId ?? previewPlanMeta.tiktokAccountId ?? "",
          tiktokAccountName:
            plan.tiktokAccountName ?? previewPlanMeta.tiktokAccountName ?? "",
        }),
      });
      await loadThreadPlan();
    } catch {
      /* ignore */
    }
  }

  async function loadThreadPlan(opts = {}) {
    if (!activeThreadId) {
      threadPlans = [];
      if (!opts.keepPairs) {
        // Leave previewPairs alone — Create / workspace may own them.
      }
      renderPlanSidebar();
      return null;
    }
    let data = null;
    try {
      data = await api(`/api/chat/threads/${activeThreadId}/plans`);
    } catch {
      data = null;
    }
    if (data) {
      applyPlansPayload(data, {
        keepWorkspacePairs: !!opts.keepWorkspacePairs || !!opts.keepPairs,
      });
    }
    return data && data.plan;
  }

  async function selectThreadPlan(planId) {
    if (!activeThreadId || !planId) return;
    if (activePlanSource === "chat" && planId === activePlanId) return;
    try {
      const data = await api(`/api/chat/threads/${activeThreadId}/plans/${planId}`, {
        method: "PATCH",
        body: JSON.stringify({ active: true }),
      });
      applyPlansPayload(data);
      paintPreviewModal();
      refreshPlanTiktokStatus()
        .then(() => paintPreviewStatus())
        .catch(() => {});
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function renameThreadPlan(planId, current) {
    if (!activeThreadId || !planId) return;
    const next = window.prompt("Rename plan", current || "");
    if (next == null) return;
    const title = String(next).trim();
    if (!title) return;
    try {
      const data = await api(`/api/chat/threads/${activeThreadId}/plans/${planId}`, {
        method: "PATCH",
        body: JSON.stringify({ title }),
      });
      applyPlansPayload(data);
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function deleteThreadPlan(planId, title) {
    if (!activeThreadId || !planId) return;
    if (!window.confirm(`Delete “${title || "this plan"}”? This cannot be undone.`)) return;
    try {
      const data = await api(`/api/chat/threads/${activeThreadId}/plans/${planId}`, {
        method: "DELETE",
      });
      applyPlansPayload(data);
      updatePreviewMeta();
      renderPreviewTable();
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function createEmptyThreadPlan() {
    if (!activeThreadId) return;
    try {
      const data = await api(`/api/chat/threads/${activeThreadId}/plans`, {
        method: "POST",
        body: JSON.stringify({ title: "" }),
      });
      applyPlansPayload(data);
      updatePreviewMeta();
      renderPreviewTable();
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function createEmptyWorkspacePlan() {
    try {
      const created = await api("/api/plans", {
        method: "POST",
        body: JSON.stringify({
          name: "Untitled plan",
          renderPairs: [],
          activate: true,
        }),
      });
      activePlanId = created.id || "";
      activePlanSource = "workspace";
      if ($("workspacePlanName")) $("workspacePlanName").value = created.name || "Untitled plan";
      applyThreadPlan(created);
      previewPlanMeta.origin = "create";
      await refreshWorkspacePlansSidebar();
      paintPreviewModal();
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function createEmptyPlanFromSidebar() {
    const page = document.body.dataset.page || "";
    if (page === "create" || !activeThreadId) {
      await createEmptyWorkspacePlan();
      return;
    }
    await createEmptyThreadPlan();
  }

  async function renameWorkspacePlan(planId, current) {
    const name = prompt("Rename plan", current || "");
    if (name == null) return;
    const trimmed = String(name).trim();
    if (!trimmed || trimmed === current) return;
    try {
      await api(`/api/plans/${encodeURIComponent(planId)}`, {
        method: "PUT",
        body: JSON.stringify({ name: trimmed }),
      });
      if ($("workspacePlanName") && activePlanId === planId) {
        $("workspacePlanName").value = trimmed;
      }
      await refreshWorkspacePlansSidebar();
      setPreviewTitle();
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function deleteWorkspacePlan(planId, title) {
    if (!confirm(`Delete plan “${title || planId}”?`)) return;
    try {
      await api(`/api/plans/${encodeURIComponent(planId)}`, { method: "DELETE" });
      if (activePlanId === planId && activePlanSource === "workspace") {
        previewPairs = [];
        activePlanId = "";
      }
      await refreshWorkspacePlansSidebar();
      const loaded = await loadActiveWorkspacePlan({ paint: false });
      if (!loaded) paintPreviewModal();
      else {
        paintPreviewModal();
        refreshPlanTiktokStatus()
          .then(() => paintPreviewStatus())
          .catch(() => {});
      }
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  async function selectWorkspacePlan(planId) {
    if (!planId) return;
    if (activePlanSource === "workspace" && planId === activePlanId && previewPairs.length) {
      return;
    }
    const generation = ++planSelectionGeneration;
    pendingPlanId = planId;
    try {
      let plan = workspacePlanDetails.get(planId);
      if (!plan) {
        plan = await api(`/api/plans/${encodeURIComponent(planId)}`);
        if (generation !== planSelectionGeneration) return;
      }
      workspacePlanDetails.set(planId, plan);
      applyWorkspacePlan(plan);
      paintPreviewModal();
      refreshPlanTiktokStatus().then(() => paintPreviewStatus()).catch(() => {});
      setStatus(`Loaded workspace plan ${plan.name || planId}.`);
      // Save asynchronously: selection should never wait on a Firestore write.
      api(`/api/plans/${encodeURIComponent(planId)}/activate`, { method: "POST" })
        .then((activated) => {
          if (generation !== planSelectionGeneration) return;
          workspacePlanDetails.set(planId, activated);
          pendingPlanId = "";
          refreshWorkspacePlansSidebar();
        })
        .catch((err) => {
          if (generation !== planSelectionGeneration) return;
          pendingPlanId = "";
          setStatus(err.message || "Could not save the active plan.", true);
        });
      return;
    } catch (err) {
      if (generation === planSelectionGeneration) pendingPlanId = "";
      alert(err.message || String(err));
      return;
    }
    try {
      const plan = await api(`/api/plans/${encodeURIComponent(planId)}/activate`, {
        method: "POST",
      });
      applyWorkspacePlan(plan);
      paintPreviewModal();
      refreshPlanTiktokStatus()
        .then(() => paintPreviewStatus())
        .catch(() => {});
      await refreshWorkspacePlansSidebar();
      setStatus(`Loaded workspace plan “${plan.name || planId}”.`);
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  function applyWorkspacePlan(plan) {
    if (!plan || typeof plan !== "object") return;
    activePlanId = plan.id || activePlanId;
    if (plan.id) workspacePlanDetails.set(plan.id, plan);
    activePlanSource = "workspace";
    if ($("workspacePlanName")) {
      $("workspacePlanName").value = plan.name || plan.title || "";
    }
    applyThreadPlan({
      ...plan,
      mode: plan.mode || "custom",
      origin: "create",
    });
    previewPlanMeta.origin = "create";
    previewPlanMeta.title = plan.name || plan.title || "";
    if (!previewPlanMeta.mode) previewPlanMeta.mode = "custom";
  }

  function memoryCreatePairs() {
    const mem =
      typeof window.getWorkspaceRenderPairs === "function"
        ? window.getWorkspaceRenderPairs()
        : [];
    if (!Array.isArray(mem) || !mem.length) return [];
    return mem.map((p) => ({
      sourceId: p.sourceId,
      caption: p.caption,
      sourceName: p.sourceName || "",
      kind: p.kind || undefined,
      slides: p.slides || undefined,
      carouselId: p.carouselId || undefined,
      pinUrl: p.pinUrl || undefined,
      outputRel: p.outputRel || "",
      rendered: !!(p.outputRel || p.rendered),
      tiktokStatus: p.tiktokStatus || "",
    }));
  }

  async function loadActiveWorkspacePlan(opts = {}) {
    try {
      const data = await api("/api/plans");
      workspacePlans = Array.isArray(data.plans) ? data.plans.slice() : [];
      const aid =
        opts.planId ||
        data.activePlanId ||
        (workspacePlans.find((p) => p.active) || {}).id ||
        "";
      if (aid) {
        const plan = await api(`/api/plans/${encodeURIComponent(aid)}`);
        if ((plan.renderPairs || []).length) {
          applyWorkspacePlan(plan);
          if (opts.paint !== false) {
            paintPreviewModal();
          }
          return plan;
        }
        activePlanId = plan.id || aid;
        activePlanSource = "workspace";
        if ($("workspacePlanName")) {
          $("workspacePlanName").value = plan.name || plan.title || "";
        }
      }
    } catch {
      /* fall through to memory */
    }
    const mem = memoryCreatePairs();
    if (mem.length) {
      applyWorkspacePlan({
        id: activePlanId || "",
        name:
          ($("workspacePlanName") && $("workspacePlanName").value.trim()) ||
          "Untitled plan",
        renderPairs: mem,
        mode: "custom",
      });
      if (opts.paint !== false) paintPreviewModal();
      return { renderPairs: mem };
    }
    return null;
  }

  function applyThreadPlan(plan) {
    previewPairs = [];
    previewPlanMeta = {
      mode: "",
      origin: "",
      modelLabel: "",
      intent: previewPlanMeta.intent || "preview",
      videoCount: 0,
      captionCount: 0,
      batchId: previewPlanMeta.batchId || "",
      tiktokPosted: 0,
      tiktokFailed: 0,
      tiktokAccountId: "",
      tiktokAccountName: "",
      title: previewPlanMeta.title || "",
    };
    if (!plan || typeof plan !== "object") {
      syncPlanAccountSelect();
      return;
    }
    const stored = Array.isArray(plan.renderPairs) ? plan.renderPairs : [];
    previewPlanMeta.tiktokAccountId = String(plan.tiktokAccountId || "").trim();
    previewPlanMeta.tiktokAccountName = String(plan.tiktokAccountName || "").trim();
    if (!stored.length) {
      syncPlanAccountSelect();
      return;
    }

    const metaByCap = {};
    for (const row of plan.captionMeta || []) {
      if (row && row.text && !metaByCap[row.text]) metaByCap[row.text] = row;
    }
    const attachMeta = (pair) => {
      const m = metaByCap[pair.caption] || {};
      const sources = pair.sources || m.sources || [];
      const origin =
        pair.origin ||
        m.origin ||
        plan.origin ||
        (sources.length > 1 ? "/all" : plan.mode || "custom");
      const model =
        pair.model ||
        m.model ||
        (sources.length ? sources.join(" · ") : "");
      return { ...pair, origin, model, sources };
    };
    const sources = chatSourcesForUi();
    const byId = Object.fromEntries(sources.map((s) => [s.id, s]));
    const lib =
      typeof window.sources !== "undefined" && Array.isArray(window.sources)
        ? window.sources
        : [];
    for (const s of lib) {
      if (s && s.id && !byId[s.id]) byId[s.id] = s;
    }
    previewPairs = stored.map((p, i) =>
      attachMeta({
        sourceId: p.sourceId,
        sourceName:
          (byId[p.sourceId] && byId[p.sourceId].name) ||
          p.sourceName ||
          p.sourceId,
        caption: p.caption,
        captionIndex: i,
        model: p.model,
        origin: p.origin,
        sources: p.sources,
        kind: p.kind || undefined,
        slides: p.slides || undefined,
        carouselId: p.carouselId || undefined,
        pinUrl: p.pinUrl || undefined,
        tiktokStatus: p.tiktokStatus || "",
        tiktokAccountName: p.tiktokAccountName || "",
        tiktokUploadedAt: p.tiktokUploadedAt || "",
        tiktokPostUrl: p.tiktokPostUrl || "",
        outputRel: p.outputRel || "",
        batchId: p.batchId || plan.batchId || "",
        rendered: !!(p.outputRel || p.rendered),
        sparkCode: p.sparkCode || "",
        sparkStatus: p.sparkStatus || "",
        sparkError: p.sparkError || "",
        sparkFetchedAt: p.sparkFetchedAt || "",
        sparkGenerated: !!p.sparkGenerated,
      })
    );
    previewPlanMeta.mode = plan.mode || "custom";
    previewPlanMeta.origin = plan.origin || previewPlanMeta.origin || "";
    previewPlanMeta.batchId = plan.batchId || previewPlanMeta.batchId || "";
    previewPlanMeta.videoCount = new Set(previewPairs.map((p) => p.sourceId)).size;
    previewPlanMeta.captionCount = new Set(previewPairs.map((p) => p.caption)).size;
    applyTiktokCountsFromPairs();
    syncPlanAccountSelect();
  }

  function planUploadAccountId() {
    return String(previewPlanMeta.tiktokAccountId || "").trim();
  }

  async function fillPlanAccountSelect() {
    const sel = $("chatPreviewAccount");
    if (!sel) return;
    let accounts = [];
    try {
      accounts = await api("/api/tiktok/accounts");
    } catch {
      accounts = [];
    }
    const sparksOnly = createPlanIntent === "sparks";
    let ready = (accounts || []).filter(
      (a) => a.enabled && a.hasCookies && a.status !== "missing_cookies"
    );
    if (sparksOnly) {
      ready = ready.filter((a) => a.sparkCapable);
    }
    const cur = planUploadAccountId();
    if (sparksOnly) {
      sel.innerHTML = ready.length
        ? ready
            .map(
              (a) =>
                `<option value="${esc(a.id)}">${esc(a.name || a.id)}</option>`
            )
            .join("")
        : `<option value="">No DuoPlus-linked accounts</option>`;
      if (ready.length === 1) {
        sel.value = ready[0].id;
      } else if (cur && ready.some((a) => a.id === cur)) {
        sel.value = cur;
      } else if (ready.length) {
        sel.value = ready[0].id;
      } else {
        sel.value = "";
      }
      if (sel.value !== cur) {
        previewPlanMeta.tiktokAccountId = sel.value || "";
        const opt = sel.options[sel.selectedIndex];
        previewPlanMeta.tiktokAccountName = opt ? opt.textContent : "";
      }
      return;
    }
    sel.innerHTML =
      `<option value="">Settings round-robin…</option>` +
      ready
        .map(
          (a) =>
            `<option value="${esc(a.id)}">${esc(a.name || a.id)}</option>`
        )
        .join("");
    if (cur && ![...sel.options].some((o) => o.value === cur)) {
      const opt = document.createElement("option");
      opt.value = cur;
      opt.textContent =
        (previewPlanMeta.tiktokAccountName || cur) + " (not ready)";
      sel.appendChild(opt);
    }
    sel.value = cur || "";
  }

  function syncPlanAccountSelect() {
    const sel = $("chatPreviewAccount");
    if (!sel) return;
    const cur = planUploadAccountId();
    if (cur && ![...sel.options].some((o) => o.value === cur)) {
      fillPlanAccountSelect().catch(() => {});
      return;
    }
    if ([...sel.options].some((o) => o.value === cur)) sel.value = cur;
    else sel.value = "";
  }

  async function persistPlanAccountChoice() {
    const sel = $("chatPreviewAccount");
    if (!sel || !activeThreadId) return;
    const id = String(sel.value || "").trim();
    const name = id
      ? (sel.options[sel.selectedIndex] && sel.options[sel.selectedIndex].text) || ""
      : "";
    previewPlanMeta.tiktokAccountId = id;
    previewPlanMeta.tiktokAccountName = id ? String(name).replace(/\s*\(not ready\)\s*$/, "") : "";
    try {
      await api(`/api/chat/threads/${activeThreadId}/plan`, {
        method: "PUT",
        body: JSON.stringify({
          tiktokAccountId: id,
          tiktokAccountName: previewPlanMeta.tiktokAccountName,
          planId: activePlanId || undefined,
        }),
      });
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  function uploadSheetOpts(extra) {
    const opts = { ...(extra || {}) };
    const aid = planUploadAccountId();
    if (aid) {
      opts.accountId = aid;
      opts.accountName = previewPlanMeta.tiktokAccountName || "";
    }
    if (opts.fetchSparks == null && createPlanIntent === "sparks") {
      opts.fetchSparks = true;
    }
    if (opts.fromCreatePlan == null && (createPlanIntent === "sparks" || createPlanIntent === "upload")) {
      opts.fromCreatePlan = true;
    }
    if (!opts.pairs && previewPairs.length) {
      opts.pairs = previewPairs.map((p) => ({
        sourceId: p.sourceId,
        caption: p.caption,
      }));
    }
    if (!opts.planId && activePlanId) opts.planId = activePlanId;
    return opts;
  }

  async function ensureChatThreadForSparks() {
    if (activeThreadId) return activeThreadId;
    const thread = await api("/api/chat/threads", {
      method: "POST",
      body: JSON.stringify({
        model: selectedModel(),
        providerModels: selectedProviderModels(),
      }),
    });
    activeThreadId = thread.id;
    try {
      await refreshThreads({ force: true });
    } catch {}
    return activeThreadId;
  }

  async function syncPlanToThreadForSparks(pairs) {
    const tid = await ensureChatThreadForSparks();
    // `pairs` is the subset the caller wants the Spark job to process.  It is
    // not the plan itself.  Persisting that subset used to shrink a 10-row
    // plan to the one failed row when the user retried Fetch all sparks.
    const planPairs = previewPairs.length ? previewPairs : (pairs || []);
    const rows = planPairs.map((p) => ({
      sourceId: p.sourceId,
      caption: p.caption,
      sourceName: p.sourceName || "",
      kind: p.kind,
      slides: p.slides,
      carouselId: p.carouselId,
      pinUrl: p.pinUrl,
      tiktokStatus: p.tiktokStatus,
      tiktokAccountName: p.tiktokAccountName,
      tiktokUploadedAt: p.tiktokUploadedAt,
      tiktokPostUrl: p.tiktokPostUrl,
      outputRel: p.outputRel,
      sparkCode: p.sparkCode,
      sparkStatus: p.sparkStatus,
      sparkError: p.sparkError,
      sparkFetchedAt: p.sparkFetchedAt,
      sparkGenerated: p.sparkGenerated,
    }));
    await api(`/api/chat/threads/${encodeURIComponent(tid)}/plan`, {
      method: "PUT",
      body: JSON.stringify({
        renderPairs: rows,
        title:
          previewPlanMeta.title ||
          ($("workspacePlanName") && $("workspacePlanName").value) ||
          "Create upload + sparks",
        planId: activePlanId || undefined,
        mode: previewPlanMeta.mode || "custom",
        tiktokAccountId: planUploadAccountId() || previewPlanMeta.tiktokAccountId || "",
        tiktokAccountName: previewPlanMeta.tiktokAccountName || "",
      }),
    });
    return tid;
  }

  let sparkJobPollTimer = 0;
  let sparkJobPollDeadline = 0;
  function stopSparkJobPoll() {
    if (sparkJobPollTimer) {
      clearInterval(sparkJobPollTimer);
      sparkJobPollTimer = 0;
    }
    sparkJobPollDeadline = 0;
  }

  async function pollSparkJobUntilDone(jobId) {
    stopSparkJobPoll();
    if (!jobId) return null;
    const tick = async () => {
      const st = await api(`/api/spark/jobs/${encodeURIComponent(jobId)}`);
      if (typeof window.updateTtSheetSparkResults === "function") {
        window.updateTtSheetSparkResults(st);
      }
      mergeSparkJobIntoPreviewPairs(st);
      if (st.status === "running" || st.status === "queued") return st;
      stopSparkJobPoll();
      const n = (st.items || []).filter((i) => String(i.sparkCode || "").trim()).length;
      if (typeof showToast === "function") {
        if (n) showToast(`Fetched ${n} spark code${n === 1 ? "" : "s"}`);
        else if (st.status === "done") showToast("Spark fetch finished");
        else showToast(st.message || "Spark fetch ended", { error: true });
      }
      paintPreviewStatus();
      return st;
    };
    try {
      const first = await tick();
      if (first && (first.status === "running" || first.status === "queued")) {
        const total = Math.max(1, Number(first.total) || 1);
        sparkJobPollDeadline = Date.now() + total * 15 * 60 * 1000;
        sparkJobPollTimer = setInterval(() => {
          if (sparkJobPollDeadline && Date.now() >= sparkJobPollDeadline) {
            stopSparkJobPoll();
            if (typeof showToast === "function") {
              showToast("Spark status polling stopped after its safety limit. The job can still be checked again from the plan.", { error: true });
            }
            return;
          }
          tick().catch((err) => {
            stopSparkJobPoll();
            if (typeof showToast === "function") {
              showToast(err.message || String(err), { error: true });
            }
          });
        }, 2500);
      }
      return first;
    } catch (err) {
      stopSparkJobPoll();
      throw err;
    }
  }

  function mergeSparkJobIntoPreviewPairs(st) {
    const items = (st && st.items) || [];
    if (!items.length || !previewPairs.length) return;
    const jobTerminal = ["done", "failed", "cancelled"].includes(String((st && st.status) || ""));
    const byKey = {};
    for (const it of items) {
      byKey[`${it.sourceId}\0${String(it.caption || "").trim()}`] = it;
    }
    previewPairs = previewPairs.map((p) => {
      const hit = byKey[`${p.sourceId}\0${String(p.caption || "").trim()}`];
      if (!hit) return p;
      const code = String(hit.sparkCode || "").trim();
      // Never let stale item state from a terminal snapshot resurrect the
      // Fetching spinner in an already-open preview.
      const itemStatus = jobTerminal && ["pending", "queued", "running"].includes(String(hit.status || ""))
        ? (st.status === "cancelled" ? "cancelled" : "failed")
        : (hit.status || p.sparkStatus);
      return {
        ...p,
        sparkCode: code || p.sparkCode,
        sparkStatus: itemStatus,
        sparkError: hit.error || (jobTerminal ? st.message : "") || p.sparkError,
        sparkGenerated: looksLikeSparkCode(code) || p.sparkGenerated,
        tiktokPostUrl: hit.postUrl || p.tiktokPostUrl,
        tiktokStatus: hit.postUrl || p.tiktokStatus === "done" ? "done" : p.tiktokStatus,
      };
    });
  }

  async function startSparksAfterUpload(uploadSt, ctx = {}) {
    const selected = selectedPreviewPairObjects();
    const pairs =
      (ctx.pairs && ctx.pairs.length && ctx.pairs) ||
      (selected.length && selected) ||
      previewPairs;
    if (!pairs.length) {
      if (typeof showToast === "function") {
        showToast("No plan rows to fetch sparks for", { error: true });
      }
      return;
    }
    if ($("ttSheetResultsSparkStatus")) {
      $("ttSheetResultsSparkStatus").textContent = "Starting Spark fetch…";
    }

    // Show immediate feedback BEFORE the API request
    showAsyncStartFeedback({
      message: `Fetching Spark codes for ${pairs.length} post${pairs.length === 1 ? "" : "s"}…`,
      showDock: true,
      expandDock: false,
      toastMs: 4000
    });

    try {
      const tid = await syncPlanToThreadForSparks(pairs);
      // Give catalog a moment to settle post URLs from the upload job.
      await new Promise((r) => setTimeout(r, 1500));
      const job = await api(`/api/chat/threads/${encodeURIComponent(tid)}/plan/fetch-spark`, {
        method: "POST",
        body: JSON.stringify({
          planId: ctx.planId || activePlanId || undefined,
          pairs: pairs.map((p) => ({ sourceId: p.sourceId, caption: p.caption })),
        }),
      });
      previewPlanMeta.sparkJobId = job.id;
      // Update dock and start watching for persistence across refresh
      if (typeof window.watchSparkJob === "function") {
        window.watchSparkJob(job.id);
      }
      if (typeof window.updateTtSheetSparkResults === "function") {
        window.updateTtSheetSparkResults(job);
      }
      await pollSparkJobUntilDone(job.id);
      startSparkPoll();
      refreshPlanSparkStatus().catch(() => {});
    } catch (err) {
      if ($("ttSheetResultsSparkStatus")) {
        $("ttSheetResultsSparkStatus").textContent = err.message || String(err);
      }
      if (typeof showToast === "function") {
        showToast(err.message || String(err), { error: true });
      }
      // Post links from the upload results sheet are still available to copy.
    }
  }

  window.onTtUploadComplete = (st, ctx) => {
    const wantSparks = !!(ctx && ctx.fetchSparks);
    // Merge post URLs onto preview rows when possible.
    if (st && Array.isArray(st.items) && previewPairs.length) {
      const byCap = {};
      for (const it of st.items) {
        const cap = String(it.caption || it.description || "").trim();
        if (cap && it.postUrl) byCap[cap] = it.postUrl;
      }
      if (Object.keys(byCap).length) {
        previewPairs = previewPairs.map((p) => {
          const url = byCap[String(p.caption || "").trim()];
          if (!url) return p;
          return { ...p, tiktokPostUrl: url, tiktokStatus: "done" };
        });
      }
    }
    if (wantSparks) {
      startSparksAfterUpload(st, ctx || {}).catch((err) => {
        if (typeof showToast === "function") {
          showToast(err.message || String(err), { error: true });
        }
      });
    }
  };

  function openVideosModal() {
    renderChatSourceList();
    setChatVideosTab("library");
    showModal("chatVideosModal", true);
  }

  let imageLibraryCache = [];
  let imageLibraryFilter = "all";

  function setChatImagesTab(tab) {
    const add = $("chatImagesAddPane");
    const lib = $("chatImagesLibraryPane");
    document.querySelectorAll("#chatImagesModal .video-tab").forEach((btn) => {
      const on = btn.dataset.imagesTab === tab;
      btn.classList.toggle("active", on);
      btn.setAttribute("aria-selected", on ? "true" : "false");
    });
    if (add) add.classList.toggle("hidden", tab !== "add");
    if (lib) lib.classList.toggle("hidden", tab !== "library");
    if (tab === "library") {
      showChatImageLibraryTab().catch(() => {});
    }
  }

  function attachedLibraryIds() {
    return new Set(
      pendingAttachments
        .filter((a) => a && (a.kind === "image" || a.libraryId || a.imageId))
        .map((a) => a.libraryId || a.imageId)
        .filter(Boolean)
    );
  }

  function imageOriginLabel(origin) {
    const o = String(origin || "upload").toLowerCase();
    if (o === "pinterest") return "Pinterest";
    if (o === "chat") return "Chat";
    return "Upload";
  }

  function renderChatImageLibraryGrid() {
    const grid = $("chatImageLibraryGrid");
    if (!grid) return;
    const attached = attachedLibraryIds();
    let items = imageLibraryCache.slice();
    if (imageLibraryFilter !== "all") {
      items = items.filter(
        (img) => String(img.origin || "upload").toLowerCase() === imageLibraryFilter
      );
    }
    grid.innerHTML = "";
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "video-lib-empty";
      empty.textContent =
        imageLibraryFilter === "all"
          ? "No images yet. Upload stills or save from Pinterest."
          : `No ${imageLibraryFilter} images yet.`;
      grid.appendChild(empty);
      return;
    }
    for (const img of items) {
      const tile = document.createElement("div");
      const id = img.id || "";
      const isOn = attached.has(id);
      tile.className = "video-lib-tile" + (isOn ? " in-job" : "");
      tile.dataset.id = id;
      tile.innerHTML = `
        <div class="video-lib-thumb">
          <img alt="" />
          <span class="video-lib-badge"></span>
          ${isOn ? '<span class="video-lib-added">Attached</span>' : ""}
        </div>
        <span class="video-lib-name"></span>
        <span class="video-lib-meta"></span>
        <button type="button" class="video-lib-del" title="Permanently delete from library">✕</button>
      `;
      tile.querySelector(".video-lib-badge").textContent = imageOriginLabel(img.origin);
      tile.querySelector(".video-lib-name").textContent = img.name || "image";
      tile.querySelector(".video-lib-name").title = img.name || "";
      tile.title = isOn
        ? "Click to remove from this chat"
        : "Click to attach to this chat";
      tile.querySelector(".video-lib-meta").textContent =
        `${img.width || "?"}×${img.height || "?"}`;
      const thumb = tile.querySelector("img");
      thumb.src = withAuth(img.fileUrl || img.thumbUrl || `/api/images/${id}/file`);
      tile.addEventListener("click", (e) => {
        if (e.target.closest(".video-lib-del")) return;
        if (attachedLibraryIds().has(id)) {
          unqueueChatAttachment({ libraryId: id, imageId: id });
        } else {
          queueChatAttachment({
            libraryId: id,
            kind: "image",
            optimisticAttachment: {
              kind: "image",
              libraryId: id,
              name: img.name || "image",
              thumbUrl: img.fileUrl || img.thumbUrl || `/api/images/${id}/file`,
              width: img.width || 0,
              height: img.height || 0,
            },
          }).catch(() => {});
        }
      });
      tile.querySelector(".video-lib-del").addEventListener("click", (e) => {
        e.stopPropagation();
        purgeChatLibraryImage(img).catch((err) => alert(err.message || String(err)));
      });
      grid.appendChild(tile);
    }
  }
  window.refreshChatImageLibraryGrid = renderChatImageLibraryGrid;

  function syncChatImageLibrarySelection() {
    const grid = $("chatImageLibraryGrid");
    if (!grid) return;
    const attached = attachedLibraryIds();
    grid.querySelectorAll(".video-lib-tile[data-id]").forEach((tile) => {
      const on = attached.has(tile.dataset.id);
      tile.classList.toggle("in-job", on);
      tile.title = on ? "Click to remove from this chat" : "Click to attach to this chat";
      let badge = tile.querySelector(".video-lib-added");
      if (on && !badge) {
        badge = document.createElement("span");
        badge.className = "video-lib-added";
        tile.querySelector(".video-lib-thumb")?.appendChild(badge);
      }
      if (badge) {
        if (on) badge.textContent = "Attached";
        else badge.remove();
      }
    });
  }
  window.syncChatImageLibrarySelection = syncChatImageLibrarySelection;

  async function loadImageLibraryCache() {
    if (typeof window.refreshImageLibrary === "function") {
      await window.refreshImageLibrary({ render: false });
      imageLibraryCache = (typeof window.getImageLibraryCache === "function")
        ? (window.getImageLibraryCache() || [])
        : [];
      return imageLibraryCache;
    }
    const res = await api("/api/images");
    imageLibraryCache = res.items || [];
    if (typeof window.setImageLibraryCache === "function") {
      window.setImageLibraryCache(imageLibraryCache);
    }
    return imageLibraryCache;
  }

  async function showChatImageLibraryTab() {
    const grid = $("chatImageLibraryGrid");
    if (grid) grid.innerHTML = `<div class="video-lib-empty">Loading library…</div>`;
    try {
      await loadImageLibraryCache();
      renderChatImageLibraryGrid();
    } catch (err) {
      if (grid) {
        grid.innerHTML = `<div class="video-lib-empty">${esc(err.message || String(err))}</div>`;
      }
    }
  }

  async function purgeChatLibraryImage(img) {
    const name = (img && img.name) || "this image";
    if (!confirm(`Permanently delete ${name} from the library?`)) return;
    const id = img.id;
    await api(`/api/images/${encodeURIComponent(id)}`, { method: "DELETE" });
    unqueueChatAttachment({ libraryId: id, imageId: id });
    imageLibraryCache = imageLibraryCache.filter((i) => i.id !== id);
    renderChatImageLibraryGrid();
    if (typeof window.refreshImageLibrary === "function") {
      window.refreshImageLibrary().catch(() => {});
    }
  }

  function openImagesModal() {
    setChatImagesTab("library");
    showModal("chatImagesModal", true);
  }

  function setPreviewTitle() {
    const title = $("chatPreviewTitle");
    if (!title) return;
    let verb = "Preview plan";
    if (createPlanIntent === "sparks") verb = "Upload + Sparks plan";
    else if (createPlanIntent === "upload") verb = "Upload plan";
    else if (createPlanIntent === "render" || previewPlanMeta.intent === "render") verb = "Render plan";
    const activeChat = threadPlans.find((p) => p.id === activePlanId);
    const activeWs = workspacePlans.find((p) => p.id === activePlanId);
    const name =
      ($("workspacePlanName") && $("workspacePlanName").value.trim()) ||
      (activePlanSource === "workspace" && activeWs && (activeWs.name || activeWs.title)) ||
      (activePlanSource === "chat" && activeChat && activeChat.title) ||
      previewPlanMeta.title ||
      "";
    title.textContent = name ? verb + " · " + name : verb;
  }

  function paintPreviewModal() {
    setPreviewTitle();
    updatePreviewMeta();
    renderPreviewTable();
    syncPlanAccountSelect();
    schedulePreviewEstimate();
  }

  /** Update badges/meta without rebuilding carousel <img> tags (avoids endless reload). */
  function paintPreviewStatus() {
    setPreviewTitle();
    updatePreviewMeta();
    const tbody = $("chatPreviewTableBody");
    if (!tbody || !previewPairs.length) {
      renderPreviewTable();
      return;
    }
    const rows = tbody.querySelectorAll("tr.chat-plan-row");
    if (rows.length !== previewPairs.length) {
      renderPreviewTable();
      return;
    }
    previewPairs.forEach((pair, i) => {
      const tr = rows[i];
      if (!tr) return;
      const renderCell = tr.querySelector(".chat-plan-render");
      const ttCell = tr.querySelector(".chat-plan-tt");
      const sparkCell = tr.querySelector(".chat-plan-spark");
      if (renderCell) renderCell.innerHTML = planRenderBadge(pair);
      if (ttCell) ttCell.innerHTML = planTiktokBadge(pair);
      if (sparkCell) sparkCell.innerHTML = planSparkBadge(pair, i);
      const check = tr.querySelector("input[type=checkbox]");
      if (check) {
        const selectable = isPreviewPairSelectable(pair);
        check.checked = selectable && check.checked;
        check.disabled = !selectable;
        check.title = selectable ? "" : "Spark code already fetched";
      }
    });
    updatePreviewActionButtons(previewPlanStats());
    // A live Spark job owns this line while it is active.  Scheduling the
    // normal selection estimate here overwrote its progress a moment later,
    // making the footer flicker between job and selection messages.
    if (!previewPlanMeta.sparkJobActive) schedulePreviewEstimate();
  }

  async function saveWorkspacePlanFromPreview() {
    const name =
      ($("workspacePlanName") && $("workspacePlanName").value.trim()) ||
      (previewPlanMeta && (previewPlanMeta.title || previewPlanMeta.name)) ||
      "Untitled plan";
    const pairs = previewPairs.length
      ? previewPairs.map((p) => ({
          sourceId: p.sourceId,
          caption: p.caption,
          sourceName: p.sourceName || "",
          kind: p.kind || undefined,
          slides: p.slides || undefined,
          carouselId: p.carouselId || undefined,
        }))
      : [];
    const selected = selectedPreviewPairs();
    const list = await api("/api/plans");
    let planId = list.activePlanId || "";
    let plan;
    if (planId) {
      plan = await api(`/api/plans/${encodeURIComponent(planId)}`, {
        method: "PUT",
        body: JSON.stringify({ name, renderPairs: pairs, selected }),
      });
      plan = await api(`/api/plans/${encodeURIComponent(planId)}/activate`, {
        method: "POST",
      });
    } else {
      plan = await api("/api/plans", {
        method: "POST",
        body: JSON.stringify({ name, renderPairs: pairs, selected, activate: true }),
      });
      planId = plan.id;
    }
    if ($("workspacePlanName")) $("workspacePlanName").value = plan.name || name;
    activePlanId = planId;
    activePlanSource = "workspace";
    await api("/api/workspace", {
      method: "PUT",
      body: JSON.stringify({
        activePlanId: planId,
        renderPairs: selected.length ? selected : pairs,
        mode: pairs.length ? "custom" : undefined,
      }),
    });
    setStatus(`“${plan.name || name}” is the active plan.`);
    await refreshWorkspacePlansSidebar();
  }

  async function refreshWorkspacePlansSidebar() {
    try {
      const data = await api("/api/plans");
      workspacePlans = Array.isArray(data.plans) ? data.plans.slice() : [];
      workspacePlans.sort(
        (a, b) => (b.updatedAt || 0) - (a.updatedAt || 0)
      );
      if (
        activePlanSource === "workspace" &&
        data.activePlanId &&
        !activePlanId
      ) {
        activePlanId = data.activePlanId;
      }
    } catch {
      /* keep prior workspacePlans */
    }
    renderPlanSidebar();
    const layout = document.querySelector("#chatPreviewModal .chat-plan-layout");
    const collapsed = !layout || layout.classList.contains("is-plans-collapsed");
    setPlanSidebarCollapsed(collapsed);
  }

  function preferCreateWorkspacePlans() {
    const page = document.body.dataset.page || "";
    return page === "create" || previewPlanMeta.origin === "create";
  }

  async function openPreviewModal() {
    clearCreatePlanIntent();
    showModal("chatPreviewModal", true);
    setPlanSidebarCollapsed(true);
    // Paint cached/in-memory data before any network request. The modal feels
    // immediate and background refreshes only replace data when they arrive.
    paintPreviewModal();
    fillPlanAccountSelect().catch(() => {});
    loadDuoPlusFlag().then(() => {
      updatePreviewActionButtons(previewPlanStats(selectedPreviewPairObjects()));
    });

    const fromCreate = preferCreateWorkspacePlans();
    const hadPairs = previewPairs.length > 0;

    const finish = () =>
      refreshPlanTiktokStatus()
        .then(() => paintPreviewStatus())
        .catch(() => paintPreviewStatus());

    // Create page: workspace / in-memory pairs are the source of truth.
    if (fromCreate) {
      if (!hadPairs || activePlanSource !== "workspace") {
        const loaded = await loadActiveWorkspacePlan({ paint: false });
        if (!loaded && !previewPairs.length) {
          // Still try chat as last resort (e.g. user jumped Create ← Chat).
          if (activeThreadId) {
            await loadThreadPlan({ keepWorkspacePairs: true });
          }
        }
      }
      // Workspace plans were returned by loadActiveWorkspacePlan. Only the
      // chat list needs a background refresh here.
      if (activeThreadId) {
        await loadThreadPlan({ keepWorkspacePairs: true, keepPairs: true });
      }
      // Render the plan sidebar so workspace plans appear in Create view
      renderPlanSidebar();
      paintPreviewModal();
      finish();
      return;
    }

    // Chat page: thread plan first, then workspace fallback.
    if (previewPairs.length && activePlanSource === "chat") {
      paintPreviewModal();
      Promise.all([
        loadThreadPlan({ keepPairs: true }),
        refreshWorkspacePlansSidebar(),
      ])
        .then(() => finish())
        .catch(() => finish());
      return;
    }

    setPreviewTitle();
    const meta = $("chatPreviewMeta");
    if (meta) meta.textContent = "Loading plan…";
    try {
      await Promise.all([
        activeThreadId ? loadThreadPlan() : Promise.resolve(),
        refreshWorkspacePlansSidebar(),
      ]);
      // A new chat deliberately starts without a plan. Do not silently fall
      // back to the workspace's last plan here: that made `/preview` appear to
      // open a previous chat's work instead of the plan just made in this one.
    } catch (err) {
      setStatus(err.message || String(err), true);
      if (meta) meta.textContent = err.message || String(err);
      return;
    }
    paintPreviewModal();
    finish();
  }

  function pairOriginLabel(pair) {
    const sources = (pair && pair.sources) || [];
    if (pair && pair.model) return pair.model;
    if (sources.length) return sources.join(" · ");
    const origin = (pair && pair.origin) || previewPlanMeta.origin || "";
    if (origin === "/multiple") {
      return previewPlanMeta.modelLabel
        ? "/multiple · " + previewPlanMeta.modelLabel
        : "/multiple";
    }
    if (origin === "/all") {
      return previewPlanMeta.modelLabel
        ? "/all · " + previewPlanMeta.modelLabel
        : "/all";
    }
    if (previewPlanMeta.modelLabel && origin === "chat") {
      return previewPlanMeta.modelLabel;
    }
    if (origin && origin !== "matrix" && origin !== "pair" && origin !== "custom") {
      return origin;
    }
    const mode = previewPlanMeta.mode || origin || "matrix";
    const model = previewPlanMeta.modelLabel;
    return model ? mode + " · " + model : mode;
  }

  function updatePreviewMeta() {
    const el = $("chatPreviewMeta");
    if (!el) return;
    const n = previewPairs.length;
    if (!n) {
      el.textContent = "No plan yet.";
      return;
    }
    const st = previewPlanStats();
    const bits = [`${st.total} pair${st.total === 1 ? "" : "s"}`];
    if (st.rendered === st.total && st.total) bits.push("all rendered");
    else if (st.needsRender) bits.push(`${st.needsRender} to render`);
    if (st.posted) bits.push(`${st.posted} posted`);
    if (st.needsUpload) bits.push(`${st.needsUpload} to upload`);
    if (st.failed) bits.push(`${st.failed} failed`);
    const v = previewPlanMeta.videoCount || new Set(previewPairs.map((p) => p.sourceId)).size;
    const c = previewPlanMeta.captionCount || new Set(previewPairs.map((p) => p.caption)).size;
    if (v && c) {
      const carousel = previewPairs.some(isCarouselPair);
      if (carousel) {
        const slideN = Math.max(
          ...previewPairs.map((p) => (Array.isArray(p.slides) ? p.slides.length : 1)),
          1
        );
        bits.push(
          `${st.total} carousel post${st.total === 1 ? "" : "s"} · ${slideN} slide${slideN === 1 ? "" : "s"} each`
        );
      } else {
        bits.push(`${v} video(s) × ${c} caption(s)`);
      }
    }
    if (previewPlanMeta.mode && previewPlanMeta.mode !== "custom") {
      bits.push("mode " + previewPlanMeta.mode);
    }
    if (previewPlanMeta.origin === "/multiple" || previewPlanMeta.origin === "/all") {
      bits.push(
        previewPlanMeta.modelLabel
          ? previewPlanMeta.origin + " · " + previewPlanMeta.modelLabel
          : previewPlanMeta.origin
      );
    } else if (previewPlanMeta.modelLabel) {
      bits.push(previewPlanMeta.modelLabel);
    }
    el.textContent = bits.join(" · ");
  }

  function looksLikeSparkCode(code) {
    const s = String(code || "").trim();
    if (!s || s.toLowerCase() === "fail") return false;
    return /^#[A-Za-z0-9+/=_-]{20,}$/.test(s);
  }

  function sparkAlreadyGenerated(pair) {
    return looksLikeSparkCode(pair && pair.sparkCode);
  }

  // A real code is a completed, non-repeatable Spark result. Keep that row out
  // of the Plan selection so Select all always means "needs a Spark", rather
  // than quietly including posts which have already been authorized.
  function isPreviewPairSelectable(pair) {
    return !sparkAlreadyGenerated(pair);
  }

  function sparkFailLabel(err) {
    const e = String(err || "").trim();
    const low = e.toLowerCase();
    if (!e) return "Failed — retry";
    if (low.includes("balance empty") || low.includes("insufficient balance")) {
      return "Failed — top up DuoPlus balance";
    }
    if (
      low.includes("phone not ready") ||
      low.includes("busy configuring") ||
      low.includes("cannot be opened") ||
      low.includes("being configured")
    ) {
      return "Failed — phone not ready yet";
    }
    if (low.includes("api connection failed") || low.includes("duoplus api error")) {
      return "Failed — DuoPlus API connection";
    }
    if (
      low.includes("proxy") ||
      low.includes("connection check failed") ||
      low.includes("no internet") ||
      low.includes("err_invalid") ||
      low.includes("network failed")
    ) {
      return "Failed — check phone connection";
    }
    if (low.includes("stuck powering") || low.includes("timed out waiting for phone")) {
      return "Failed — phone power-on stuck";
    }
    if (low.includes("ad settings") || low.includes("spark ui failed")) {
      return "Failed — Ad settings / share UI";
    }
    if (low.includes("no duoplus phone") || low.includes("linked to this tiktok")) {
      return "Failed — link DuoPlus phone";
    }
    if (low.includes("not configured") || low.includes("duoplus_api_key")) {
      return "Failed — DuoPlus not configured";
    }
    const short =
      e.length > 64 ? e.slice(0, 61).replace(/\s+\S*$/, "") + "…" : e;
    return `Failed — ${short}`;
  }

  function planSparkBadge(pair, idx) {
    const code = (pair.sparkCode || "").trim();
    const st = pair.sparkStatus || "";
    if (looksLikeSparkCode(code)) {
      return `<div class="spark-cell"><span class="spark-code" title="${esc(code)}">${esc(code)}</span><button type="button" class="btn ghost spark-copy" data-idx="${idx}">Copy</button></div>`;
    }
    if (st === "running") {
      return `<span class="tt-badge"><span class="spin" aria-hidden="true"></span> Fetching…</span>`;
    }
    if (st === "pending" || st === "queued") {
      return `<span class="tt-badge"><span class="spin" aria-hidden="true"></span> Queued</span>`;
    }
    if (st === "failed") {
      const err = String(pair.sparkError || "").trim();
      const label = sparkFailLabel(err);
      return `<span class="tt-badge tt-badge-failed" title="${esc(err || label)}">${esc(
        label
      )}</span>`;
    }
    if (st === "skipped") {
      return `<span class="tt-badge">Skipped</span>`;
    }
    if (isPairPosted(pair)) {
      return `<span class="tt-badge">No code</span>`;
    }
    return `<span class="tt-badge">—</span>`;
  }

  function planTiktokBadge(pair) {
    if (pair.tiktokStatus === "done") {
      const who = pair.tiktokAccountName ? ` @${pair.tiktokAccountName}` : "";
      return `<span class="tt-badge tt-badge-posted">Posted${esc(who)}</span>`;
    }
    if (pair.tiktokStatus === "failed") {
      return `<span class="tt-badge tt-badge-failed">Failed</span>`;
    }
    return `<span class="tt-badge">Not posted</span>`;
  }

  function planRenderBadge(pair) {
    if (isCarouselPair(pair)) {
      const n = (pair.slides || []).length;
      return `<span class="tt-badge tt-badge-ok">${n} slide${n === 1 ? "" : "s"}</span>`;
    }
    if (pair.outputRel || pair.rendered) {
      return `<span class="tt-badge tt-badge-ok">Rendered</span>`;
    }
    return `<span class="tt-badge">Not rendered</span>`;
  }

  function isCarouselPair(pair) {
    return !!(pair && (pair.kind === "carousel" || (pair.slides && pair.slides.length)));
  }

  function isPairPosted(pair) {
    return !!(pair && pair.tiktokStatus === "done");
  }

  function isPairRendered(pair) {
    if (isCarouselPair(pair)) return true;
    return !!(pair && (pair.outputRel || pair.rendered));
  }

  function previewPlanStats(pairs) {
    const list = pairs || previewPairs;
    let rendered = 0;
    let posted = 0;
    let failed = 0;
    let needsUpload = 0;
    for (const p of list) {
      if (isPairRendered(p)) rendered += 1;
      if (isPairPosted(p)) posted += 1;
      else if (p.tiktokStatus === "failed") failed += 1;
      if (isPairRendered(p) && !isPairPosted(p)) needsUpload += 1;
    }
    const total = list.length;
    return {
      total,
      rendered,
      needsRender: Math.max(0, total - rendered),
      posted,
      failed,
      needsUpload,
    };
  }

  function updatePreviewActionButtons(st) {
    const renderBtn = $("chatPreviewMakeBtn");
    const uploadBtn = $("chatPreviewUploadBtn");
    const zipBtn = $("chatPreviewDownloadZipBtn");
    const notPostedBtn = $("chatPreviewSelectNotPosted");
    const sparkBtn = $("chatPreviewFetchSparkBtn");
    const copySparksBtn = $("chatPreviewCopySparksBtn");
    if (!renderBtn || !uploadBtn) return;
    const carouselPlan = previewPairs.length && previewPairs.every(isCarouselPair);
    const needsRender = previewPairs.some((p) => !isCarouselPair(p) && !isPairRendered(p));
    const needsUpload = previewPairs.some((p) => !isPairPosted(p));
    const hasSparks = previewPairs.some((p) => looksLikeSparkCode(p.sparkCode));
    renderBtn.hidden = !needsRender;
    uploadBtn.hidden = !needsUpload;
    renderBtn.disabled = !needsRender;
    uploadBtn.disabled = !needsUpload;
    const sendCreate = $("chatPreviewSendCreateBtn");
    if (sendCreate) sendCreate.hidden = !!carouselPlan;
    if (zipBtn) {
      const hasBatch = !!(previewPlanMeta.batchId && st.rendered > 0);
      zipBtn.classList.toggle("hidden", !hasBatch);
      zipBtn.hidden = !hasBatch;
    }
    if (notPostedBtn) {
      notPostedBtn.hidden = previewPlanStats(previewPairs).needsUpload <= 0;
    }
    if (sparkBtn) {
      sparkBtn.hidden = false;
      sparkBtn.disabled = !st.total || sparkPipelineStarting;
      sparkBtn.title = "Render selected rows, upload them to TikTok, then fetch their Spark codes";
    }
    if (copySparksBtn) {
      copySparksBtn.hidden = !hasSparks;
      copySparksBtn.classList.toggle("hidden", !hasSparks);
      copySparksBtn.disabled = !hasSparks;
    }
  }

  function updatePreviewSelectAllState() {
    const boxes = [
      ...document.querySelectorAll("#chatPreviewTableBody input[type=checkbox]"),
    ].filter((c) => !c.disabled);
    const el = $("chatPreviewSelectAll");
    if (!el) return;
    if (!boxes.length) {
      el.checked = false;
      el.indeterminate = false;
      return;
    }
    el.checked = boxes.every((c) => c.checked);
    el.indeterminate = !el.checked && boxes.some((c) => c.checked);
  }

  function setPreviewRowSelection(filterFn) {
    document
      .querySelectorAll("#chatPreviewTableBody input[type=checkbox]")
      .forEach((c) => {
        const pair = previewPairs[Number(c.dataset.idx)];
        c.checked = isPreviewPairSelectable(pair) && !!filterFn(pair);
      });
    updatePreviewSelectAllState();
    schedulePreviewEstimate();
  }

  function mergePlanTiktokHit(pair, hit) {
    const onTt =
      !!hit.onTiktok ||
      pair.tiktokStatus === "done" ||
      hit.tiktokStatus === "done";
    let status = "done";
    if (!onTt) {
      if (pair.tiktokStatus === "failed" || hit.tiktokStatus === "failed") {
        status = "failed";
      } else {
        status = pair.tiktokStatus || hit.tiktokStatus || "";
      }
    }
    return {
      ...pair,
      tiktokStatus: status,
      tiktokAccountName: hit.tiktokAccountName || pair.tiktokAccountName || "",
      tiktokUploadedAt: hit.tiktokUploadedAt || pair.tiktokUploadedAt || "",
      outputRel: hit.outputRel || pair.outputRel || "",
      rendered: !!(
        hit.rendered ||
        hit.outputRel ||
        pair.outputRel ||
        pair.rendered ||
        isCarouselPair(pair) ||
        isCarouselPair(hit)
      ),
      kind: hit.kind || pair.kind || "",
      slides: hit.slides || pair.slides || [],
      carouselId: hit.carouselId || pair.carouselId || pair.sourceId || "",
      tiktokPostUrl: hit.tiktokPostUrl || pair.tiktokPostUrl || "",
      sparkCode: looksLikeSparkCode(hit.sparkCode || pair.sparkCode)
        ? (hit.sparkCode || pair.sparkCode)
        : "",
      sparkStatus: hit.sparkStatus || pair.sparkStatus || "",
      sparkError: hit.sparkError || pair.sparkError || "",
      sparkFetchedAt: hit.sparkFetchedAt || pair.sparkFetchedAt || "",
      sparkGenerated: looksLikeSparkCode(hit.sparkCode || pair.sparkCode),
    };
  }

  function mergeTiktokStatusPayload(tt) {
    if (!tt || typeof tt !== "object") return false;
    if (tt.batchId) previewPlanMeta.batchId = tt.batchId;
    const map = Object.fromEntries(
      (tt.items || []).map((it) => [`${it.sourceId}\0${it.caption}`, it])
    );
    if (Object.keys(map).length) {
      previewPairs = previewPairs.map((p) =>
        mergePlanTiktokHit(p, map[`${p.sourceId}\0${p.caption}`] || {})
      );
    }
    previewPlanMeta.tiktokPosted = Number(tt.posted) || 0;
    previewPlanMeta.tiktokFailed =
      Number(tt.failed) ||
      previewPairs.filter((p) => p.tiktokStatus === "failed").length;
    applyTiktokCountsFromPairs();
    return previewPlanMeta.tiktokPosted > 0 || previewPlanMeta.tiktokFailed > 0;
  }

  async function enrichPreviewPairsFromOutputs() {
    if (!previewPairs.length) return;
    try {
      const w = await api("/api/workspace");
      const pairs = previewPairs.map((p) => ({
        sourceId: p.sourceId,
        caption: p.caption,
        kind: p.kind,
        slides: p.slides,
        carouselId: p.carouselId,
      }));
      const sourceIds = [...new Set(pairs.map((p) => p.sourceId).filter(Boolean))];
      const captions = [...new Set(pairs.map((p) => p.caption).filter(Boolean))];
      if (!sourceIds.length || !captions.length) return;
      const resolved = await api("/api/batch/resolve-outputs", {
        method: "POST",
        body: JSON.stringify({
          sourceIds,
          captions,
          pairs,
          mode: "custom",
          settings: currentCaptionSettings(w),
          nameTemplate: w.nameTemplate,
          skipExisting: true,
          dedupe: false,
        }),
      });
      const readyMap = Object.fromEntries(
        (resolved.ready || []).map((r) => [`${r.sourceId}\0${r.caption}`, r])
      );
      if (!Object.keys(readyMap).length) return;
      previewPairs = previewPairs.map((p) => {
        const hit = readyMap[`${p.sourceId}\0${p.caption}`];
        if (!hit) return p;
        return {
          ...p,
          outputRel: hit.path || p.outputRel || "",
          rendered: true,
        };
      });
    } catch {
      /* optional enrichment */
    }
  }

  async function refreshPlanTiktokStatus() {
    if (!previewPairs.length) return;
    applyTiktokCountsFromPairs();
    if (activeThreadId) {
      try {
        mergeTiktokStatusPayload(
          await api(`/api/chat/threads/${encodeURIComponent(activeThreadId)}/plan/tiktok-status`)
        );
        await refreshPlanSparkStatus();
        return;
      } catch {
        /* fall through */
      }
    }
    // Workspace plans keep execution state in the render catalog. Re-read the
    // authoritative enriched plan on every preview refresh; the batch endpoint
    // only reports TikTok upload state and otherwise leaves an old in-memory
    // sparkStatus="running" badge untouched after an interrupted Spark job.
    if (activePlanSource === "workspace" && activePlanId) {
      try {
        const plan = await api(`/api/plans/${encodeURIComponent(activePlanId)}`);
        if (Array.isArray(plan.renderPairs)) {
          workspacePlanDetails.set(activePlanId, plan);
          mergeTiktokStatusPayload({ items: plan.renderPairs });
        }
      } catch {
        /* fall back to the batch status below */
      }
    }
    // Workspace / Create plans: resolve renders from catalog without a chat thread.
    await enrichPreviewPairsFromOutputs();
    applyTiktokCountsFromPairs();
    const batchId = previewPlanMeta.batchId;
    if (!batchId) return;
    try {
      mergeTiktokStatusPayload(await api(`/api/batch/${batchId}/tiktok-status`));
    } catch {
      /* stored plan rows are enough */
    }
  }

  function stopSparkPoll() {
    if (sparkPollTimer) {
      clearInterval(sparkPollTimer);
      sparkPollTimer = 0;
    }
    sparkPollDeadline = 0;
  }

  function startSparkPoll() {
    if (sparkPollTimer) return;
    const total = Math.max(1, previewPairs.length || 1);
    sparkPollDeadline = Date.now() + total * 15 * 60 * 1000;
    sparkPollTimer = setInterval(() => {
      if (sparkPollDeadline && Date.now() >= sparkPollDeadline) {
        stopSparkPoll();
        setStatus(
          "Spark status polling stopped after its safety limit. Reopen the plan to check the job again.",
          true
        );
        return;
      }
      refreshPlanSparkStatus().catch(() => {});
    }, 2500);
  }

  async function refreshPlanSparkStatus() {
    if (!activeThreadId || !previewPairs.length) return;
    try {
      const payload = await api(
        `/api/chat/threads/${encodeURIComponent(activeThreadId)}/plan/spark-status` +
          (activePlanId ? `?plan_id=${encodeURIComponent(activePlanId)}` : "")
      );
      mergeTiktokStatusPayload(payload);
      const job = payload && payload.job;
      if (job && job.id) previewPlanMeta.sparkJobId = job.id;
      const eta = $("chatPreviewEta");
      if (job && (job.status === "running" || job.status === "queued")) {
        // The durable plan is updated as each result is persisted.  Overlay
        // the live job too, so the specific row changes immediately even if
        // that storage update is still in flight.
        const jobItems = Object.fromEntries(
          (job.items || []).map((item) => [
            `${item.sourceId || ""}\0${item.caption || ""}`,
            {
              ...item,
              sparkStatus: item.status || "pending",
              sparkError: item.error || "",
              sparkGenerated: !!item.sparkGenerated,
            },
          ])
        );
        if (Object.keys(jobItems).length) {
          previewPairs = previewPairs.map((pair) =>
            mergePlanTiktokHit(
              pair,
              jobItems[`${pair.sourceId || ""}\0${pair.caption || ""}`] || {}
            )
          );
        }
        previewPlanMeta.sparkJobActive = true;
        startSparkPoll();
        if (eta && job.message) {
          eta.textContent = job.message;
        } else if (eta) {
          eta.textContent = `${job.fetched || 0}/${job.total || 0} sparks fetched` +
            (job.currentPhone ? ` (${job.currentPhone})` : "");
        }
      } else {
        previewPlanMeta.sparkJobActive = false;
        stopSparkPoll();
      }
      paintPreviewStatus();
    } catch {
      /* ignore */
    }
  }

  async function fetchSparkForSelected() {
    if (sparkFetchStarting) return;
    const selected = selectedPreviewPairObjects();
    if (!selected.length) {
      alert("Select one or more posted rows to fetch Spark codes.");
      return;
    }
    const eligible = selected.filter(
      (p) => isPairPosted(p) && !sparkAlreadyGenerated(p)
    );
    if (!eligible.length) {
      alert("The selected rows already have Spark codes or have not been posted.");
      return;
    }
    const missingUrl = eligible.filter((p) => !String(p.tiktokPostUrl || "").trim());
    if (missingUrl.length && eligible.every((p) => !String(p.tiktokPostUrl || "").trim())) {
      // Server still looks up catalog post URLs; only warn if none look posted.
    }
    try {
      sparkFetchStarting = true;
      const sparkBtn = $("chatPreviewFetchSparkBtn");
      if (sparkBtn) sparkBtn.disabled = true;

      // Show immediate feedback BEFORE the API request
      showAsyncStartFeedback({
        message: `Fetching Spark codes for ${eligible.length} post${eligible.length === 1 ? "" : "s"}…`,
        showDock: true,
        expandDock: false,
        toastMs: 4000
      });

      const tid = await syncPlanToThreadForSparks(eligible);
      const job = await api(`/api/chat/threads/${encodeURIComponent(tid)}/plan/fetch-spark`, {
        method: "POST",
        body: JSON.stringify({
          planId: activePlanId || undefined,
          pairs: eligible.map((p) => ({ sourceId: p.sourceId, caption: p.caption })),
        }),
      });
      previewPlanMeta.sparkJobId = job.id;
      // Update dock and start watching for persistence across refresh
      if (typeof window.watchSparkJob === "function") {
        window.watchSparkJob(job.id);
      }
      startSparkPoll();
      await refreshPlanSparkStatus();
    } catch (err) {
      alert(err.message || String(err));
    } finally {
      sparkFetchStarting = false;
      updatePreviewActionButtons(previewPlanStats(selectedPreviewPairObjects()));
    }
  }

  async function copyAllSparks() {
    // Prefer every real code on the plan (complete set), not only checked rows.
    const fromPlan = (previewPairs || [])
      .filter((p) => looksLikeSparkCode(p.sparkCode))
      .map((p) => String(p.sparkCode || "").trim());
    const fromSelected = selectedPreviewPairObjects()
      .filter((p) => looksLikeSparkCode(p.sparkCode))
      .map((p) => String(p.sparkCode || "").trim());
    const codes = [...new Set(fromPlan.length ? fromPlan : fromSelected)];
    if (!codes.length) {
      alert("No spark codes on this plan yet.");
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

  async function loadDuoPlusFlag() {
    if (window.config && typeof window.config.duoPlusConfigured !== "undefined") {
      duoPlusConfigured = !!window.config.duoPlusConfigured;
      return;
    }
    try {
      const cfg = await api("/api/config");
      duoPlusConfigured = !!cfg.duoPlusConfigured;
    } catch {
      duoPlusConfigured = false;
    }
  }

  function renderPreviewTable() {
    hidePlanHover();
    const tbody = $("chatPreviewTableBody");
    if (!tbody) return;
    const planKey = `${activePlanSource}:${activePlanId}`;
    const existingBoxes = [
      ...tbody.querySelectorAll("input[type=checkbox]"),
    ];
    const prevChecked = existingBoxes.length && !resetPreviewSelection && planKey === previewSelectionPlanKey
      ? new Set(
          existingBoxes.filter((c) => c.checked).map((c) => Number(c.dataset.idx))
        )
      : null;
    if (!previewPairs.length) {
      const onCreate = (document.body.dataset.page || "") === "create";
      tbody.innerHTML = onCreate
        ? '<tr><td colspan="8" class="chat-plan-empty">No plan yet. Add videos and captions on Create, or open Plan after setting pairs.</td></tr>'
        : '<tr><td colspan="8" class="chat-plan-empty">No plan yet. Ask for a render plan or type /preview.</td></tr>';
      const eta = $("chatPreviewEta");
      if (eta) eta.textContent = "";
      updatePreviewActionButtons({ needsRender: 0, needsUpload: 0, posted: 0, total: 0 });
      return;
    }
    const frag = document.createDocumentFragment();
    previewPairs.forEach((pair, i) => {
      const tr = document.createElement("tr");
      tr.className = "chat-plan-row";
      tr.innerHTML = `
        <td class="chat-plan-check"><label class="check"><input type="checkbox" ${isPreviewPairSelectable(pair) ? "checked" : "disabled"} data-idx="${i}" title="${isPreviewPairSelectable(pair) ? "" : "Spark code already fetched"}" /></label></td>
        <td class="chat-plan-num" data-label="#">${i + 1}</td>
        <td class="chat-plan-video" data-label="Media"></td>
        <td class="chat-plan-cap" data-label="Caption"></td>
        <td class="chat-plan-render" data-label="Rendered"></td>
        <td class="chat-plan-tt" data-label="TikTok"></td>
        <td class="chat-plan-spark" data-label="Spark"></td>
        <td class="chat-plan-origin" data-label="Model"></td>`;
      const videoCell = tr.querySelector(".chat-plan-video");
      if (isCarouselPair(pair)) {
        const slides = pair.slides || [];
        videoCell.innerHTML =
          `<span class="carousel-thumbs">` +
          slides
            .map(
              (id) =>
                `<img alt="" data-slide-id="${esc(id)}" src="${esc(withAuth("/api/images/" + encodeURIComponent(id) + "/file"))}" />`
            )
            .join("") +
          `</span>`;
        videoCell.title = `${slides.length}-slide carousel`;
        videoCell.querySelectorAll("img").forEach((img) => {
          img.addEventListener("error", () => {
            img.removeAttribute("src");
            img.classList.add("thumb-missing");
          }, { once: true });
        });
      } else {
        videoCell.textContent = pair.sourceName || pair.sourceId || "";
        videoCell.dataset.sourceId = pair.sourceId || "";
        bindPlanVideoHover(videoCell, pair.sourceId);
      }
      tr.querySelector(".chat-plan-cap").textContent = pair.caption || "";
      tr.querySelector(".chat-plan-cap").title = pair.caption || "";
      tr.querySelector(".chat-plan-render").innerHTML = planRenderBadge(pair);
      tr.querySelector(".chat-plan-tt").innerHTML = planTiktokBadge(pair);
      tr.querySelector(".chat-plan-spark").innerHTML = planSparkBadge(pair, i);
      tr.querySelector(".chat-plan-origin").textContent = pairOriginLabel(pair);
      frag.appendChild(tr);
    });
    tbody.replaceChildren(frag);
    previewSelectionPlanKey = planKey;
    resetPreviewSelection = false;
    if (prevChecked) {
      tbody.querySelectorAll("input[type=checkbox]").forEach((c) => {
        const pair = previewPairs[Number(c.dataset.idx)];
        c.checked = isPreviewPairSelectable(pair) && prevChecked.has(Number(c.dataset.idx));
      });
    }
    updatePreviewSelectAllState();
    schedulePreviewEstimate();
  }

  function selectedPreviewPairObjects() {
    const root = $("chatPreviewTableBody");
    if (!root) return [];
    return [...root.querySelectorAll("input[type=checkbox]:checked")]
      .map((c) => previewPairs[Number(c.dataset.idx)])
      .filter(Boolean);
  }

  function selectedPreviewPairs() {
    return selectedPreviewPairObjects().map((p) => ({
      sourceId: p.sourceId,
      caption: p.caption,
    }));
  }

  function schedulePreviewEstimate() {
    clearTimeout(previewEtaTimer);
    previewEtaTimer = setTimeout(() => {
      refreshPreviewEstimate().catch(() => {});
    }, 280);
  }

  async function refreshPreviewEstimate() {
    const el = $("chatPreviewEta");
    if (!el) return;
    const selected = selectedPreviewPairObjects();
    if (!selected.length) {
      el.textContent = "";
      updatePreviewActionButtons({ needsRender: 0, needsUpload: 0, posted: 0, total: 0 });
      return;
    }
    const carouselOnly = selected.every(isCarouselPair);
    if (carouselOnly) {
      el.textContent = `${selected.length} photo carousel${selected.length === 1 ? "" : "s"} · native TikTok photos (no burn-in render)`;
      updatePreviewActionButtons({
        needsRender: 0,
        needsUpload: selected.filter((p) => !p.tiktokPostUrl).length,
        posted: selected.filter((p) => p.tiktokPostUrl).length,
        total: selected.length,
      });
      return;
    }
    const st = previewPlanStats(selected);
    updatePreviewActionButtons(st);
    const fmt = window.fmtDuration || ((s) => (s ? `${s}s` : ""));
    const parts = [];
    try {
      if (st.needsRender > 0) {
        const toRender = selected.filter((p) => !isPairRendered(p));
        const est = await api("/api/batch/estimate", {
          method: "POST",
          body: JSON.stringify({
            sourceIds: [...new Set(toRender.map((p) => p.sourceId))],
            captions: toRender.map((p) => p.caption),
            mode: previewPlanMeta.mode || "custom",
            pairs: toRender.map((p) => ({ sourceId: p.sourceId, caption: p.caption })),
            dedupe: true,
          }),
        });
        const dur = fmt(est.estimatedSeconds);
        if (est.jobs && dur) {
          parts.push(`${dur} render ${est.jobs}`);
        }
      }
      if (st.needsUpload > 0) {
        const delay = Number($("ttDelay")?.value) || 10;
        const eng = ($("ttUploadEngine") && $("ttUploadEngine").value) || "studio";
        const sec = window.estimateUploadSeconds
          ? window.estimateUploadSeconds(st.needsUpload, delay, eng)
          : st.needsUpload * 55;
        const dur = fmt(sec);
        if (dur) parts.push(`${dur} upload ${st.needsUpload}`);
      }
      if (!parts.length && st.posted === st.total) {
        el.textContent = "All selected posted";
      } else if (parts.length) {
        el.textContent = `About ${parts.join(" · ")}`;
      } else {
        el.textContent = "";
      }
    } catch {
      el.textContent = "";
    }
  }

  function normalizeOutputRel(path) {
    let p = String(path || "").replace(/\\/g, "/");
    const marker = "/output/";
    const idx = p.toLowerCase().lastIndexOf(marker);
    if (idx >= 0) p = p.slice(idx + marker.length);
    else if (p.startsWith("output/")) p = p.slice("output/".length);
    return p;
  }

  async function startBatchFromPairs(pairs, { openUpload, extraUploadPaths, fetchSparks, sparkPairs } = {}) {
    if (!pairs.length) {
      alert("Select at least one row.");
      return null;
    }
    const captions = [...new Set(pairs.map((p) => p.caption))];
    const sourceIds = [...new Set(pairs.map((p) => p.sourceId))];
    const w = await api("/api/workspace");
    const useEndCaptions = $("chatPreviewUseEndCaptions") ? $("chatPreviewUseEndCaptions").checked : true;
    const res = await api("/api/batch/start", {
      method: "POST",
      body: JSON.stringify({
        sourceIds,
        captions,
        pairs,
        mode: w.mode || "matrix",
        settings: currentCaptionSettings(w),
        createdVia: "chat",
        chatThreadId: activeThreadId || threadIdFromHash() || "",
        offerId: w.offerId || undefined,
        nameTemplate: w.nameTemplate,
        skipExisting: true,
        dedupe: !!w.dedupe,
        useEndCaptions,
      }),
    });
    if (activeThreadId && res.batchId) {
      previewPlanMeta.batchId = res.batchId;
      api(`/api/chat/threads/${activeThreadId}/plan`, {
        method: "PUT",
        body: JSON.stringify({
          batchId: res.batchId,
          planId: activePlanId || undefined,
        }),
      }).catch(() => {});
    }
    showModal("chatPreviewModal", false);
    const eta = (window.fmtDuration && res.estimatedSeconds)
      ? ` · about ${window.fmtDuration(res.estimatedSeconds)}`
      : "";
    if (res.status === "queued") {
      setStatus(
        openUpload
          ? `Queued render #${res.queuePosition || "?"} (${res.jobs} video(s))${eta}, then uploading…`
          : `Queued render #${res.queuePosition || "?"} (${res.jobs} video(s))${eta}…`
      );
    } else {
      setStatus(
        openUpload
          ? `Rendering ${res.jobs} video(s)${eta}, then uploading…`
          : `Rendering ${res.jobs} video(s)${eta}…`
      );
    }
    const onDone = async (st) => {
      if (st && (st.status === "running" || st.status === "queued")) return;
      setStatus("Render finished.");
      if (openUpload && typeof window.openTtUploadSheet === "function") {
        let batchPaths = [];
        try {
          const latest =
            st && st.items ? st : await api(`/api/batch/${res.batchId}`);
          batchPaths = (latest.items || [])
            .filter(
              (it) =>
                (it.status === "done" || it.status === "skipped") && it.output
            )
            .map((it) => normalizeOutputRel(it.output))
            .filter(Boolean);
        } catch {}
        const merged = [];
        const seen = new Set();
        for (const p of [...(extraUploadPaths || []), ...batchPaths]) {
          if (!p || seen.has(p)) continue;
          seen.add(p);
          merged.push(p);
        }
        try {
          const sheetExtra = {
            autoStart: true,
            fetchSparks: !!fetchSparks,
            fromCreatePlan: !!(fetchSparks || createPlanIntent === "upload" || createPlanIntent === "sparks"),
            pairs: (sparkPairs || pairs).map((p) => ({ sourceId: p.sourceId, caption: p.caption })),
            planId: activePlanId || undefined,
          };
          if (merged.length) {
            await window.openTtUploadSheet(
              uploadSheetOpts({ paths: merged, ...sheetExtra })
            );
          } else {
            await window.openTtUploadSheet(
              uploadSheetOpts({ batchId: res.batchId, ...sheetExtra })
            );
          }
        } catch (err) {
          alert(err.message || String(err));
        }
      } else {
        openResultsModal({ batchId: res.batchId });
      }
      if (window.loadOutputs) window.loadOutputs().catch(() => {});
      window.onChatBatchDone = null;
      window._chatBatchDoneFor = null;
    };
    window.onChatBatchDone = onDone;
    window._chatBatchDoneFor = res.batchId;
    if (typeof window.watchBatch === "function") {
      window.watchBatch(res.batchId);
    } else {
      setStatus("Render started — open Create to watch progress.", true);
    }
    return res;
  }

  async function makeSelectedFromPreview() {
    try {
      const pairs = previewPairs
        .filter((p) => !isCarouselPair(p) && !isPairRendered(p))
        .map((p) => ({ sourceId: p.sourceId, caption: p.caption }));
      await startBatchFromPairs(pairs, { openUpload: false });
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function sendSelectedToCreate() {
    const objects = selectedPreviewPairObjects();
    if (objects.length && objects.every(isCarouselPair)) {
      alert("Photo carousels stay in Plan — they do not go to Create (no burn-in). Use Upload instead.");
      return;
    }
    const pairs = objects.map((p) => ({ sourceId: p.sourceId, caption: p.caption }));
    if (!pairs.length) {
      alert("Select at least one row to send to Create.");
      return;
    }
    const btn = $("chatPreviewSendCreateBtn");
    if (btn) btn.disabled = true;
    try {
      const captions = [...new Set(pairs.map((p) => p.caption).filter(Boolean))];
      const sourceIds = [...new Set(pairs.map((p) => p.sourceId).filter(Boolean))];
      const captionMeta = captions.map((text) => {
        const hit = previewPairs.find((p) => p.caption === text) || {};
        return {
          text,
          sources: hit.sources || [],
          origin: hit.origin || "chat",
          model: hit.model || "",
        };
      });
      if (window.cancelWorkspaceSave) window.cancelWorkspaceSave();
      await api("/api/workspace", {
        method: "PUT",
        body: JSON.stringify({
          captions: captions.join("\n"),
          renderPairs: pairs,
          mode: "custom",
          captionMeta,
        }),
      });
      try {
        const planName =
          ($("workspacePlanName") && $("workspacePlanName").value.trim()) ||
          (previewPlanMeta && previewPlanMeta.title) ||
          "Chat plan";
        const created = await api("/api/plans", {
          method: "POST",
          body: JSON.stringify({
            name: planName,
            renderPairs: pairs,
            selected: pairs,
            activate: true,
          }),
        });
        if ($("workspacePlanName")) $("workspacePlanName").value = created.name || planName;
        await api("/api/workspace", {
          method: "PUT",
          body: JSON.stringify({ activePlanId: created.id }),
        });
      } catch {
        /* workspace plans optional if API down */
      }
      try {
        const applied = await api("/api/library/apply", {
          method: "POST",
          body: JSON.stringify({ addIds: sourceIds, removeIds: [] }),
        });
        if (Array.isArray(applied && applied.sources) && window.setSources) {
          window.setSources(applied.sources);
          if (window.renderSources) window.renderSources();
        } else if (window.reloadJobSources) {
          await window.reloadJobSources();
        }
      } catch {
        if (window.reloadJobSources) await window.reloadJobSources();
        else if (window.setSources) {
          const list = await api("/api/sources");
          window.setSources(list);
          if (window.renderSources) window.renderSources();
        }
      }
      if (window.loadWorkspaceRemote) await window.loadWorkspaceRemote();
      showModal("chatPreviewModal", false);
      if (typeof window.navigateAppPage === "function") {
        window.navigateAppPage("create");
      } else {
        window.location.hash = "#/create";
      }
      setStatus(
        "Sent " +
          pairs.length +
          " pair" +
          (pairs.length === 1 ? "" : "s") +
          " (" +
          sourceIds.length +
          " video" +
          (sourceIds.length === 1 ? "" : "s") +
          ") to Create."
      );
    } catch (err) {
      alert(err.message || String(err));
    } finally {
      if (btn) btn.disabled = false;
    }
  }

  async function uploadSelectedFromPreview(uploadMode, forcePairs = null) {
    const wantSparks = uploadMode === "sparks";
    const chosen = forcePairs || (wantSparks ? selectedPreviewPairObjects() : previewPairs);
    const sparkPairs = chosen
      .filter((p) => !sparkAlreadyGenerated(p))
      .map((p) => ({ sourceId: p.sourceId, caption: p.caption }));
    const objects = chosen.filter((p) => !isPairPosted(p));
    if (!objects.length) {
      if (wantSparks && sparkPairs.length) {
        await fetchSparkForSelected();
      } else {
        alert(wantSparks
          ? "Every selected row already has a Spark code."
          : "Every row in this plan has already been uploaded.");
      }
      return;
    }
    if (objects.every(isCarouselPair)) {
      try {
        const res = await api("/api/tiktok/carousel-upload", {
          method: "POST",
          body: JSON.stringify({
            threadId: activeThreadId,
            planId: activePlanId || undefined,
            pairs: objects.map((p) => ({
              kind: "carousel",
              carouselId: p.carouselId || p.sourceId,
              sourceId: p.sourceId,
              slides: p.slides || [],
              caption: p.caption,
            })),
            accountId: planUploadAccountId() || undefined,
          }),
        });
        setStatus(`Carousel upload started (${res.uploadId || res.id}).`);
        await refreshPlanTiktokStatus();
        if (carouselStatusTimer) clearInterval(carouselStatusTimer);
        const uploadId = res.uploadId || res.id;
        let checkingCarouselStatus = false;
        carouselStatusTimer = setInterval(async () => {
          if (checkingCarouselStatus) return;
          checkingCarouselStatus = true;
          try {
            const st = await api(`/api/tiktok/upload/${encodeURIComponent(uploadId)}`);
            await refreshPlanTiktokStatus();
            if (st.status === "done" || st.status === "cancelled" || st.status === "failed") {
              clearInterval(carouselStatusTimer);
              carouselStatusTimer = 0;
              if (wantSparks && st.status === "done") {
                await startSparksAfterUpload(st, {
                  fetchSparks: true,
                  pairs: sparkPairs,
                  planId: activePlanId || undefined,
                });
              }
            }
          } catch {
            refreshPlanTiktokStatus().catch(() => {});
          } finally {
            checkingCarouselStatus = false;
          }
        }, 4000);
        setTimeout(() => {
          if (carouselStatusTimer) {
            clearInterval(carouselStatusTimer);
            carouselStatusTimer = 0;
          }
        }, 12 * 60 * 1000);
      } catch (err) {
        alert(err.message || String(err));
      }
      return;
    }
    const pairs = objects.map((p) => ({ sourceId: p.sourceId, caption: p.caption }));
    if (!pairs.length) {
      alert("Select at least one row.");
      return;
    }
    try {
      const w = await api("/api/workspace");
      const sourceIds = [...new Set(pairs.map((p) => p.sourceId))];
      const captions = [...new Set(pairs.map((p) => p.caption))];
      const resolved = await api("/api/batch/resolve-outputs", {
        method: "POST",
        body: JSON.stringify({
          sourceIds,
          captions,
          pairs,
          mode: "custom",
          settings: currentCaptionSettings(w),
          nameTemplate: w.nameTemplate,
          skipExisting: true,
          dedupe: false,
        }),
      });
      const readyPaths = resolved.paths || [];
      const missing = (resolved.missing || []).map((m) => ({
        sourceId: m.sourceId,
        caption: m.caption,
      }));

      if (!missing.length && readyPaths.length) {
        showModal("chatPreviewModal", false);
        setStatus(`Reusing ${readyPaths.length} already-rendered video(s)…`);
        if (typeof window.openTtUploadSheet === "function") {
          await window.openTtUploadSheet(
            uploadSheetOpts({
              paths: readyPaths,
              autoStart: true,
              fetchSparks: wantSparks,
              fromCreatePlan: true,
              pairs: wantSparks ? sparkPairs : pairs,
              planId: activePlanId || undefined,
            })
          );
        } else {
          alert("Upload sheet unavailable.");
        }
        if (wantSparks) {
          setStatus("Upload started — Spark codes will appear when the job finishes.");
        } else {
          setStatus("");
        }
        return;
      }

      if (!missing.length) {
        alert("No rendered outputs found for the selected rows yet. Render them first.");
        return;
      }

      await startBatchFromPairs(missing, {
        openUpload: true,
        extraUploadPaths: readyPaths,
        fetchSparks: wantSparks,
        sparkPairs: wantSparks ? sparkPairs : undefined,
      });
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function runSelectedRenderUploadAndSparks() {
    if (sparkPipelineStarting) return;
    if (!selectedPreviewPairObjects().length) {
      alert("Select at least one row.");
      return;
    }
    await loadDuoPlusFlag();
    if (!duoPlusConfigured) {
      alert("Configure DuoPlus before fetching Spark codes.");
      return;
    }
    if (!planUploadAccountId()) {
      alert("Pick a DuoPlus-linked account under Post as (Sparks login).");
      return;
    }
    const btn = $("chatPreviewFetchSparkBtn");
    sparkPipelineStarting = true;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "Starting…";
    }
    try {
      // uploadSelectedFromPreview handles both paths: reuse rendered outputs,
      // or render first and automatically open/start the upload. Its completion
      // callback then launches the Spark job.
      await uploadSelectedFromPreview("sparks");
    } finally {
      sparkPipelineStarting = false;
      if (btn) {
        btn.disabled = false;
        btn.textContent = "Sparks";
        updatePreviewActionButtons(previewPlanStats(selectedPreviewPairObjects()));
      }
    }
  }

  async function pollBatchThenResults(batchId) {
    stopPollBatch();
    if (typeof window.watchBatch !== "function") {
      setStatus("Render started — open Create to watch progress.");
      return;
    }
    window.onChatBatchDone = (st) => {
      if (st && (st.status === "running" || st.status === "queued")) return;
      setStatus("Render finished.");
      openResultsModal({ batchId });
      if (window.loadOutputs) window.loadOutputs().catch(() => {});
      window.onChatBatchDone = null;
      window._chatBatchDoneFor = null;
    };
    window._chatBatchDoneFor = batchId;
    window.watchBatch(batchId);
  }

  function stopPollBatch() {
    if (typeof window.stopPoll === "function" && resultsBatchId) {
      window.stopPoll("batch:" + resultsBatchId);
    }
  }

  async function openResultsModal(opts = {}) {
    showModal("chatResultsModal", true);
    const meta = $("chatResultsMeta");
    if (meta) meta.textContent = "Loading…";
    resultsBatchId = opts.batchId || "";
    try {
      if (resultsBatchId) {
        const st = await api(`/api/batch/${resultsBatchId}`);
        resultsItems = (st.items || [])
          .filter((it) => (it.status === "done" || it.status === "skipped") && it.output)
          .map((it) => {
            const path = it.output;
            const filename = String(path).split("/").pop();
            return { path, filename, caption: it.caption || "" };
          });
      } else {
        const res = await api("/api/outputs?limit=100");
        resultsItems = res.items || [];
      }
      if (meta) {
        meta.textContent = resultsItems.length
          ? `${resultsItems.length} rendered file(s)`
          : "No renders yet.";
      }
      await renderResultsGrid();
    } catch (err) {
      if (meta) meta.textContent = err.message || String(err);
    }
  }

  async function renderResultsGrid() {
    const grid = $("chatResultsGrid");
    if (!grid) return;
    grid.innerHTML = "";
    await freshAuthUrl("/api/output");
    for (const it of resultsItems) {
      const playUrl = withAuth(
        `/api/output/${encodeURIComponent(it.path).replace(/%2F/g, "/")}`
      );
      const tile = document.createElement("div");
      tile.className = "chat-result-tile";
      tile.innerHTML = `
        <label class="check chat-tile-check" title="Select this render">
          <input type="checkbox" checked data-path="${esc(it.path)}" />
        </label>
        <video class="chat-result-video" controls preload="metadata" playsinline></video>
        <div class="chat-tile-cap"></div>`;
      tile.querySelector(".chat-result-video").src = playUrl;
      tile.querySelector(".chat-tile-cap").textContent = it.filename;
      tile.querySelector(".chat-tile-cap").title = it.filename || "";
      grid.appendChild(tile);
    }
    if ($("chatResultsSelectAll")) $("chatResultsSelectAll").checked = true;
  }

  async function uploadSelectedResults() {
    if (!resultsBatchId) {
      alert("Render a chat batch first, then upload those files.");
      return;
    }
    const checked = [
      ...document.querySelectorAll("#chatResultsGrid input[type=checkbox]:checked"),
    ];
    if (!checked.length) {
      alert("Select at least one video.");
      return;
    }
    try {
      if (typeof window.openTtUploadSheet === "function") {
        showModal("chatResultsModal", false);
        await window.openTtUploadSheet(uploadSheetOpts({ batchId: resultsBatchId }));
        return;
      }
      const names = checked
        .map((c) => (c.dataset.path || "").split("/").pop())
        .filter(Boolean);
      const preview = names.slice(0, 5).map((n) => `• ${n}`).join("\n");
      if (
        !confirm(
          `Upload this batch (${names.length} video(s)) to TikTok?\n\n${preview}${
            names.length > 5 ? "\n…" : ""
          }`
        )
      ) {
        return;
      }
      const body = { batchId: resultsBatchId };
      const aid = planUploadAccountId();
      if (aid) body.accountId = aid;
      const res = await api("/api/tiktok/upload", {
        method: "POST",
        body: JSON.stringify(body),
      });
      setStatus(`Upload started (${res.uploadId || res.id}). Check Settings for progress.`);
      showModal("chatResultsModal", false);
      window.location.hash = "#/settings";
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  // ----- Offers panel -----
  function openOffers(on) {
    const panel = $("chatOffersPanel");
    if (!panel) return;
    panel.classList.toggle("hidden", !on);
    if (on && typeof window.loadHooksPage === "function") {
      window.loadHooksPage().catch(() => {});
    }
  }

  function slashToken(text) {
    const s = (text || "").trimStart();
    if (!s.startsWith("/") || s.includes("\n")) return "";
    // Only while typing the command name (before args / first space).
    const m = s.match(/^\/[^\s]*/);
    return (m ? m[0] : "").toLowerCase();
  }

  function matchingSlashCommands(text) {
    const token = slashToken(text);
    if (!token) return [];
    // Once a full command is typed and a space follows, hide the menu.
    const s = (text || "").trimStart();
    if (/\s/.test(s)) return [];
    return slashCommands.filter((c) => (c.cmd || "").toLowerCase().startsWith(token));
  }

  function hideSlashMenu() {
    const menu = $("chatSlashMenu");
    if (!menu) return;
    menu.classList.add("hidden");
    menu.hidden = true;
    menu.innerHTML = "";
  }

  function renderSlashMenu() {
    const input = $("chatInput");
    const menu = $("chatSlashMenu");
    if (!input || !menu) return;
    const matches = matchingSlashCommands((input.value || "").trimStart());
    if (!matches.length) {
      hideSlashMenu();
      return;
    }
    if (slashIndex >= matches.length) slashIndex = 0;
    if (slashIndex < 0) slashIndex = matches.length - 1;
    menu.hidden = false;
    menu.classList.remove("hidden");
    menu.innerHTML = "";
    matches.forEach((c, i) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "chat-slash-item" + (i === slashIndex ? " active" : "") + (c.enabled === false ? " off" : "");
      btn.setAttribute("role", "option");
      btn.innerHTML = `<span class="chat-slash-cmd"></span><span class="chat-slash-hint"></span>`;
      btn.querySelector(".chat-slash-cmd").textContent = c.cmd;
      btn.querySelector(".chat-slash-hint").textContent =
        c.enabled === false ? (c.hint || "") + " (not configured)" : (c.hint || "");
      btn.addEventListener("mousedown", (e) => {
        e.preventDefault();
        applySlashCommand(c);
      });
      menu.appendChild(btn);
    });
  }

  function applySlashCommand(cmd) {
    const input = $("chatInput");
    if (!input || !cmd) return;
    hideSlashMenu();
    if (cmd.id === "help") {
      input.value = "/help";
      sendMessage();
      return;
    }
    if (cmd.id === "multiple") {
      openMultiplePicker();
      return;
    }
    input.value = (cmd.cmd || "/research") + " ";
    input.focus();
    const len = input.value.length;
    input.setSelectionRange(len, len);
  }

  function providerLabel(p) {
    return (
      { anthropic: "Claude", openai: "OpenAI", xai: "Grok", gemini: "Gemini" }[p] || p
    );
  }

  function multipleBoxes() {
    return [...document.querySelectorAll("#chatMultipleList input[type=checkbox]")];
  }

  function updateMultipleCount() {
    const boxes = multipleBoxes();
    const n = boxes.filter((c) => c.checked && !c.disabled).length;
    const el = $("chatMultipleCount");
    if (el) el.textContent = n + " selected";
    boxes.forEach((c) => {
      const row = c.closest(".chat-multiple-item");
      if (row) row.classList.toggle("is-on", c.checked);
    });
    if ($("chatMultipleGo")) $("chatMultipleGo").disabled = n < 1;
  }

  function setMultipleChecked(on, onlyEnabled) {
    multipleBoxes().forEach((c) => {
      if (onlyEnabled && c.disabled) return;
      if (!c.disabled) c.checked = !!on;
    });
    updateMultipleCount();
  }

  function closeMultiplePicker() {
    showModal("chatMultipleModal", false);
  }

  function openMultiplePicker() {
    const list = $("chatMultipleList");
    if (!list || !$("chatMultipleModal")) {
      const input = $("chatInput");
      if (input) {
        input.value = "/multiple ";
        input.focus();
      }
      return;
    }
    const remembered = new Set(multipleModelIds);
    const byProvider = {};
    for (const m of modelsCatalog) {
      if (!byProvider[m.provider]) byProvider[m.provider] = [];
      byProvider[m.provider].push(m);
    }
    list.innerHTML = "";
    for (const p of Object.keys(byProvider)) {
      const group = document.createElement("div");
      group.className = "chat-multiple-group";
      const title = document.createElement("div");
      title.className = "chat-multiple-group-title";
      title.textContent = providerLabel(p);
      group.appendChild(title);
      for (const m of byProvider[p]) {
        const row = document.createElement("label");
        row.className = "chat-multiple-item" + (m.enabled ? "" : " off");
        row.innerHTML =
          '<input type="checkbox" />' +
          '<span class="chat-multiple-item-body">' +
          '<span class="chat-multiple-item-label"></span>' +
          '<span class="chat-multiple-item-id"></span>' +
          "</span>";
        const box = row.querySelector("input");
        box.value = m.id;
        box.disabled = !m.enabled;
        box.checked = !!m.enabled && (remembered.size ? remembered.has(m.id) : false);
        row.querySelector(".chat-multiple-item-label").textContent =
          m.label + (m.enabled ? "" : " (off)");
        row.querySelector(".chat-multiple-item-id").textContent = m.id;
        box.addEventListener("change", updateMultipleCount);
        group.appendChild(row);
      }
      list.appendChild(group);
    }
    updateMultipleCount();
    showModal("chatMultipleModal", true);
  }

  function confirmMultiplePicker() {
    const ids = multipleBoxes()
      .filter((c) => c.checked && !c.disabled)
      .map((c) => c.value)
      .filter(Boolean);
    if (!ids.length) return;
    saveMultipleModelIds(ids);
    closeMultiplePicker();
    const input = $("chatInput");
    if (!input) return;
    const cur = (input.value || "").trimStart();
    const rest = cur.replace(/^\s*\/(multiple|multi|models)\b\s*/i, "").trim();
    // Keep prompt if they already typed one; models travel in the request body.
    input.value = "/multiple " + (rest || "");
    input.focus();
    const len = input.value.length;
    input.setSelectionRange(len, len);
    growComposer();
  }

  function onChatInputKeydown(e) {
    const menu = $("chatSlashMenu");
    const menuOpen = menu && !menu.classList.contains("hidden") && !menu.hidden;
    if (menuOpen) {
      const matches = matchingSlashCommands(($("chatInput").value || "").trimStart());
      if (e.key === "ArrowDown") {
        e.preventDefault();
        slashIndex = (slashIndex + 1) % Math.max(matches.length, 1);
        renderSlashMenu();
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        slashIndex = (slashIndex - 1 + matches.length) % Math.max(matches.length, 1);
        renderSlashMenu();
        return;
      }
      if (e.key === "Tab") {
        e.preventDefault();
        applySlashCommand(matches[slashIndex] || matches[0]);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        hideSlashMenu();
        return;
      }
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        applySlashCommand(matches[slashIndex] || matches[0]);
        return;
      }
    }
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  }

  // ----- Init -----
  function initChat(opts = {}) {
    if (!$("chatSendBtn") && !$("chatNewBtn")) return;
    if (_chatInited) {
      // Recents lives in the sidebar on every page — always keep the list warm.
      refreshThreads().catch(() => {});
      if (opts.prefetchModels) loadModels().catch(() => {});
      return;
    }
    _chatInited = true;

    $("chatNewBtn")?.addEventListener("click", () => {
      newChat()
        .then(() => {
          document.body.classList.remove("sidebar-open");
          if ($("sidebarBackdrop")) $("sidebarBackdrop").classList.add("hidden");
        })
        .catch((e) => setStatus(e.message, true));
    });
    $("chatSendBtn")?.addEventListener("click", () => sendMessage());
    document.querySelectorAll("[data-usage-range]").forEach((btn) => {
      btn.addEventListener("click", () => {
        usageRange = btn.dataset.usageRange || "session";
        document.querySelectorAll("[data-usage-range]").forEach((other) => {
          const on = other === btn;
          other.classList.toggle("active", on);
          other.setAttribute("aria-selected", on ? "true" : "false");
        });
        refreshUsage().catch(() => {});
      });
    });
    renderUsage({ inputTokens: 0, outputTokens: 0, costUsd: 0, models: [] });
    $("chatInput")?.addEventListener("keydown", onChatInputKeydown);
    $("chatInput")?.addEventListener("input", () => {
      slashIndex = 0;
      renderSlashMenu();
      growComposer();
    });
    $("chatInput")?.addEventListener("blur", () => {
      setTimeout(hideSlashMenu, 120);
    });
    $("chatProvider").addEventListener("change", () => {
      fillModelSelect(preferredModelFor($("chatProvider").value));
      persistThreadModel().catch(() => {});
    });
    $("chatModel").addEventListener("change", () => {
      persistThreadModel().catch(() => {});
    });

    function closeChatSheets() {
      document.body.classList.remove("chat-usage-open");
      if ($("chatUsageToggle") && window.matchMedia("(max-width: 1199px)").matches) {
        $("chatUsageToggle").setAttribute("aria-pressed", "false");
      }
    }

    $("chatVideosBtn").addEventListener("click", () => openVideosModal());
    if ($("chatImagesBtn")) {
      $("chatImagesBtn").addEventListener("click", () => openImagesModal());
    }
    document.querySelectorAll("#chatImagesModal .video-tab").forEach((btn) => {
      btn.addEventListener("click", () => setChatImagesTab(btn.dataset.imagesTab));
    });
    document.querySelectorAll("[data-image-lib-filters]").forEach((root) => {
      root.addEventListener("click", (e) => {
        const btn = e.target.closest(".video-lib-filter");
        if (!btn) return;
        imageLibraryFilter = btn.dataset.imageLibFilter || "all";
        root.querySelectorAll(".video-lib-filter").forEach((b) => {
          b.classList.toggle("active", b === btn);
        });
        renderChatImageLibraryGrid();
      });
    });
    if ($("chatImageDropZone") && $("chatImageFileInput")) {
      const zone = $("chatImageDropZone");
      const input = $("chatImageFileInput");
      zone.addEventListener("click", () => input.click());
      ["dragenter", "dragover"].forEach((ev) =>
        zone.addEventListener(ev, (e) => {
          e.preventDefault();
          zone.classList.add("drag");
        })
      );
      ["dragleave", "drop"].forEach((ev) =>
        zone.addEventListener(ev, (e) => {
          e.preventDefault();
          zone.classList.remove("drag");
        })
      );
      zone.addEventListener("drop", (e) => {
        const files = [...(e.dataTransfer.files || [])];
        uploadChatImageFiles(files).catch(() => {});
      });
      input.addEventListener("change", () => {
        const files = [...input.files];
        input.value = "";
        uploadChatImageFiles(files).catch(() => {});
      });
    }
    if ($("chatImagePinterestSearchBtn") && $("chatImagePinterestQuery") && window.bindPinterestSearchPanel) {
      window.bindPinterestSearchPanel({
        queryId: "chatImagePinterestQuery",
        btnId: "chatImagePinterestSearchBtn",
        statusId: "chatImagePinterestStatus",
        gridId: "chatImagePinterestResults",
        addRowId: "chatImagePinterestAddRow",
        addBtnId: "chatImagePinterestAddSelected",
        mediaType: "image",
        onAddSelected: async (chosen) => {
          const status = $("chatImagePinterestStatus");
          if (!chosen.length) {
            if (status) status.textContent = "Select pins first.";
            return;
          }
          for (const pin of chosen) {
            const imageUrl = pin.imageUrl || pin.previewUrl || "";
            if (!imageUrl) continue;
            if (status) status.textContent = `Saving ${pin.title || pin.url}…`;
            try {
              const img = await api("/api/images/from-url", {
                method: "POST",
                body: JSON.stringify({
                  url: imageUrl,
                  pinUrl: pin.url || pin.pinUrl,
                  title: pin.title || "",
                }),
              });
              if (img && img.id) {
                await queueChatAttachment({ libraryId: img.id, kind: "image" });
              }
            } catch (err) {
              if (status) status.textContent = err.message || String(err);
            }
          }
          await loadImageLibraryCache().catch(() => {});
          renderChatImageLibraryGrid();
          if (status) status.innerHTML = `<span class="ok">✓ images attached</span>`;
        },
      });
    }

    async function uploadChatImageFiles(files) {
      const status = $("chatImageUploadStatus");
      for (const file of files || []) {
        if (status) status.textContent = `Uploading ${file.name}…`;
        try {
          await queueChatAttachment({ file });
        } catch (err) {
          if (status) {
            status.innerHTML = `<span class="err">${esc(file.name)}: ${esc(err.message || err)}</span>`;
          }
        }
      }
      if (status) status.textContent = "";
      await loadImageLibraryCache().catch(() => {});
      renderChatImageLibraryGrid();
      if (typeof window.refreshImageLibrary === "function") {
        window.refreshImageLibrary().catch(() => {});
      }
    }
    if ($("chatUsageToggle")) {
      const saved = localStorage.getItem(LS_USAGE);
      if (saved === "collapsed") document.body.classList.add("chat-usage-collapsed");
      $("chatUsageToggle").setAttribute("aria-pressed", saved !== "collapsed" ? "true" : "false");
      $("chatUsageToggle").addEventListener("click", () => {
        if (window.matchMedia("(max-width: 1199px)").matches) {
          const on = !document.body.classList.contains("chat-usage-open");
          document.body.classList.toggle("chat-usage-open", on);
          $("chatUsageToggle").setAttribute("aria-pressed", on ? "true" : "false");
          return;
        }
        const collapsed = document.body.classList.toggle("chat-usage-collapsed");
        localStorage.setItem(LS_USAGE, collapsed ? "collapsed" : "open");
        $("chatUsageToggle").setAttribute("aria-pressed", collapsed ? "false" : "true");
      });
    }
    if ($("chatUsageClose")) {
      $("chatUsageClose").addEventListener("click", () => closeChatSheets());
    }
    if ($("chatMessages")) {
      $("chatMessages").addEventListener("click", (e) => {
        const btn = e.target.closest("[data-slash]");
        if (!btn) return;
        const slash = btn.dataset.slash || "";
        if (/^\/multiple\b/i.test(slash.trim())) {
          openMultiplePicker();
          return;
        }
        const input = $("chatInput");
        if (!input) return;
        input.value = slash;
        input.focus();
        growComposer();
      });
    }
    const wrap = document.querySelector(".chat-composer-wrap");
    if (wrap) {
      wrap.addEventListener("dragover", (e) => {
        e.preventDefault();
        wrap.classList.add("is-drop");
      });
      wrap.addEventListener("dragleave", () => wrap.classList.remove("is-drop"));
      wrap.addEventListener("drop", (e) => {
        e.preventDefault();
        wrap.classList.remove("is-drop");
        const files = [...(e.dataTransfer.files || [])];
        files.forEach((file) => queueChatAttachment({ file }).catch(() => {}));
      });
      wrap.addEventListener("paste", (e) => {
        const files = [...(e.clipboardData && e.clipboardData.files || [])];
        if (!files.length) return;
        e.preventDefault();
        files.forEach((file) => queueChatAttachment({ file }).catch(() => {}));
      });
    }
    document.querySelectorAll("#chatVideosModal .video-tab").forEach((btn) => {
      btn.addEventListener("click", () => setChatVideosTab(btn.dataset.videosTab));
    });
    $("chatPreviewBtn").addEventListener("click", () => {
      previewPlanMeta.intent = "preview";
      openPreviewModal().catch((err) => setStatus(err.message || String(err), true));
    });
    if ($("chatPlanNewBtn")) {
      $("chatPlanNewBtn").addEventListener("click", () => {
        createEmptyPlanFromSidebar().catch((err) =>
          setStatus(err.message || String(err), true)
        );
      });
    }
    if ($("chatPlanSidebarToggle")) {
      $("chatPlanSidebarToggle").addEventListener("click", () => {
        const layout = document.querySelector("#chatPreviewModal .chat-plan-layout");
        const collapsed = !layout || layout.classList.contains("is-plans-collapsed");
        setPlanSidebarCollapsed(!collapsed);
      });
    }
    initPlanHover();
    $("chatOffersBtn").addEventListener("click", () => openOffers(true));
    if ($("chatOffersClose")) $("chatOffersClose").addEventListener("click", () => openOffers(false));
    if ($("chatOffersBackdrop")) $("chatOffersBackdrop").addEventListener("click", () => openOffers(false));

    const closePairs = [
      ["chatVideosClose", "chatVideosModal"],
      ["chatVideosBackdrop", "chatVideosModal"],
      ["chatVideosDone", "chatVideosModal"],
      ["chatImagesClose", "chatImagesModal"],
      ["chatImagesBackdrop", "chatImagesModal"],
      ["chatImagesDone", "chatImagesModal"],
      ["chatPreviewClose", "chatPreviewModal"],
      ["chatPreviewBackdrop", "chatPreviewModal"],
      ["chatResultsClose", "chatResultsModal"],
      ["chatResultsBackdrop", "chatResultsModal"],
      ["chatCaptionPickClose", "chatCaptionPickModal"],
      ["chatCaptionPickBackdrop", "chatCaptionPickModal"],
      ["chatCaptionPickSkip", "chatCaptionPickModal"],
    ];
    for (const [btn, modal] of closePairs) {
      if ($(btn)) $(btn).addEventListener("click", () => showModal(modal, false));
    }

    if ($("chatPreviewMakeBtn")) {
      $("chatPreviewMakeBtn").addEventListener("click", () => makeSelectedFromPreview());
    }
    if ($("createPlanConfirmBtn")) {
      $("createPlanConfirmBtn").addEventListener("click", () => {
        confirmCreatePlanIntent().catch((err) => alert(err.message || String(err)));
      });
    }
    if ($("chatPreviewSendCreateBtn")) {
      $("chatPreviewSendCreateBtn").addEventListener("click", () => {
        sendSelectedToCreate().catch((err) => alert(err.message || String(err)));
      });
    }
    if ($("chatPreviewUploadBtn")) {
      $("chatPreviewUploadBtn").addEventListener("click", () => {
        uploadSelectedFromPreview("upload").catch((err) =>
          alert(err.message || String(err))
        );
      });
    }
    if ($("workspacePlanActivateBtn")) {
      // Bound once in app.js via wireThisIsThePlanButton (Create vs Chat dispatcher).
      if (typeof window.wireThisIsThePlanButton === "function") {
        window.wireThisIsThePlanButton();
      }
    }
    if ($("chatPreviewFetchSparkBtn")) {
      $("chatPreviewFetchSparkBtn").addEventListener("click", () => {
        runSelectedRenderUploadAndSparks().catch((err) => alert(err.message || String(err)));
      });
    }
    if ($("chatPreviewCopySparksBtn")) {
      $("chatPreviewCopySparksBtn").addEventListener("click", () => {
        copyAllSparks().catch((err) => alert(err.message || String(err)));
      });
    }
    if ($("chatPreviewDownloadZipBtn")) {
      $("chatPreviewDownloadZipBtn").addEventListener("click", () => {
        const id = previewPlanMeta.batchId;
        if (!id) {
          alert("Render the plan first, then download the ZIP.");
          return;
        }
        window.location.href = withAuth(`/api/batch/${id}/zip`);
      });
    }
    if ($("chatPreviewAccount")) {
      $("chatPreviewAccount").addEventListener("change", () => {
        persistPlanAccountChoice().catch(() => {});
      });
    }
    if ($("chatPreviewSelectAll")) {
      $("chatPreviewSelectAll").addEventListener("change", () => {
        const on = $("chatPreviewSelectAll").checked;
        $("chatPreviewSelectAll").indeterminate = false;
        document
          .querySelectorAll("#chatPreviewTableBody input[type=checkbox]")
          .forEach((c) => {
            const pair = previewPairs[Number(c.dataset.idx)];
            c.checked = on && isPreviewPairSelectable(pair);
          });
        schedulePreviewEstimate();
      });
    }
    if ($("chatPreviewSelectNotPosted")) {
      $("chatPreviewSelectNotPosted").addEventListener("click", () => {
        setPreviewRowSelection((pair) => !isPairPosted(pair));
      });
    }
    const previewBody = $("chatPreviewTableBody");
    if (previewBody) {
      previewBody.addEventListener("change", (e) => {
        if (e.target && e.target.matches("input[type=checkbox]")) {
          updatePreviewSelectAllState();
          schedulePreviewEstimate();
        }
      });
      previewBody.addEventListener("click", (e) => {
        const btn = e.target && e.target.closest && e.target.closest("[data-idx].spark-copy");
        if (!btn) return;
        const pair = previewPairs[Number(btn.dataset.idx)];
        const code = pair && String(pair.sparkCode || "").trim();
        if (!code) return;
        navigator.clipboard.writeText(code).catch(() => alert(code));
      });
    }
    if ($("chatResultsSelectAll")) {
      $("chatResultsSelectAll").addEventListener("change", () => {
        const on = $("chatResultsSelectAll").checked;
        document.querySelectorAll("#chatResultsGrid input[type=checkbox]").forEach((c) => {
          c.checked = on;
        });
      });
    }
    if ($("chatResultsUploadBtn")) {
      $("chatResultsUploadBtn").addEventListener("click", () => uploadSelectedResults());
    }

    if ($("chatCaptionPickSelectAll")) {
      $("chatCaptionPickSelectAll").addEventListener("click", () => setCaptionPickChecked(true));
    }
    if ($("chatCaptionPickSelectNone")) {
      $("chatCaptionPickSelectNone").addEventListener("click", () => setCaptionPickChecked(false));
    }
    [
      ["chatMultipleClose", "chatMultipleModal"],
      ["chatMultipleBackdrop", "chatMultipleModal"],
      ["chatMultipleCancel", "chatMultipleModal"],
    ].forEach(([id, modal]) => {
      if ($(id)) {
        $(id).addEventListener("click", () => showModal(modal, false));
      }
    });
    if ($("chatMultipleSelectAll")) {
      $("chatMultipleSelectAll").addEventListener("click", () => setMultipleChecked(true, true));
    }
    if ($("chatMultipleSelectNone")) {
      $("chatMultipleSelectNone").addEventListener("click", () => setMultipleChecked(false));
    }
    if ($("chatMultipleGo")) {
      $("chatMultipleGo").addEventListener("click", () => confirmMultiplePicker());
    }
    if ($("chatCaptionPickProvider")) {
      $("chatCaptionPickProvider").addEventListener("change", () => {
        const p = $("chatCaptionPickProvider").value;
        fillCaptionPickModelSelect(preferredModelFor(p));
      });
    }
    if ($("chatCaptionPickModel")) {
      $("chatCaptionPickModel").addEventListener("change", () => {
        const p = $("chatCaptionPickProvider") && $("chatCaptionPickProvider").value;
        const m = $("chatCaptionPickModel").value;
        rememberSelection(p, m);
      });
    }
    if ($("chatCaptionPickAiBtn")) {
      $("chatCaptionPickAiBtn").addEventListener("click", () => {
        applyCaptionPickAi().catch((err) => setStatus(err.message || String(err), true));
      });
    }
    if ($("chatCaptionPickGo")) {
      $("chatCaptionPickGo").addEventListener("click", () => {
        proceedCaptionPick().catch((err) => setStatus(err.message || String(err), true));
      });
    }
    document.addEventListener("keydown", (e) => {
      if (e.key !== "Escape") return;
      const lb = $("chatAttachLightbox");
      if (lb && !lb.classList.contains("hidden")) {
        closeAttachLightbox();
        return;
      }
      hideSlashMenu();
      document.body.classList.remove("chat-usage-open");
      document.body.classList.remove("sidebar-open");
      if ($("sidebarBackdrop")) $("sidebarBackdrop").classList.add("hidden");
      [
        "chatVideosModal",
        "chatImagesModal",
        "chatPreviewModal",
        "chatResultsModal",
        "chatCaptionPickModal",
        "chatMultipleModal",
        "chatOffersPanel",
      ].forEach((id) => {
        const el = $(id);
        if (el && !el.classList.contains("hidden")) showModal(id, false);
      });
      if (typeof window.closeAllOverlays === "function") window.closeAllOverlays();
    });

    // Chat videos drop/url
    if ($("chatDropZone") && $("chatFileInput")) {
      $("chatDropZone").addEventListener("click", () => $("chatFileInput").click());
      $("chatFileInput").addEventListener("change", async () => {
        const files = [...$("chatFileInput").files];
        $("chatFileInput").value = "";
        for (const file of files) {
          const fd = new FormData();
          fd.append("file", file);
          const src = await api("/api/sources/upload?addToJob=false", {
            method: "POST",
            body: fd,
          });
          if (src && src.id) await queueChatAttachment({ sourceId: src.id });
        }
        await refreshSourcesFromServer();
      });
    }
    if ($("chatAddUrls")) {
      $("chatAddUrls").addEventListener("click", async () => {
        const lines = ($("chatUrlInput").value || "").split("\n").map((l) => l.trim()).filter(Boolean);
        if (!lines.length) return;
        if (typeof window.addSourceUrls === "function") {
          await window.addSourceUrls(lines, $("chatUrlStatus"), { addToJob: false });
        } else {
          for (const url of lines) {
            try {
              await api("/api/sources/url", {
                method: "POST",
                body: JSON.stringify({ url, addToJob: false }),
              });
            } catch (err) {
              if ($("chatUrlStatus")) $("chatUrlStatus").textContent = err.message;
            }
          }
        }
        $("chatUrlInput").value = "";
        await refreshSourcesFromServer();
      });
    }
    if ($("chatPinterestSearchBtn") && $("chatPinterestQuery") && window.bindPinterestSearchPanel) {
      window.bindPinterestSearchPanel({
        queryId: "chatPinterestQuery",
        btnId: "chatPinterestSearchBtn",
        statusId: "chatPinterestStatus",
        gridId: "chatPinterestResults",
        addRowId: "chatPinterestAddRow",
        addBtnId: "chatPinterestAddSelected",
        libraryBtnId: "chatPinterestAddToLibrary",
        mediaType: "video",
        onResults: () => scheduleUsageRefresh(),
        onAddSelected: async (chosen) => {
          const status = $("chatPinterestStatus");
          if (!chosen.length) {
            if (status) status.textContent = "Select pins first";
            return;
          }
          for (const pin of chosen) {
            await queueChatAttachment(pinAttachmentFromPin(pin));
          }
          if (status) {
            status.textContent =
              chosen.length + " pin(s) added to chat (not downloaded yet)";
          }
        },
      });
    }
    ["chatEndCapEnabled", "chatEndCapText", "chatEndCapDuration"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("change", () => persistChatEndCap().catch(() => {}));
    });
    // Recents is always visible in the sidebar — load thread index immediately.
    // Models wait until the Chat page opens (loadChatPage).
    const cached = readThreadsCache();
    if (cached && cached.threads && cached.threads.length) {
      threads = cached.threads;
      renderThreadList();
    } else {
      paintThreadsLoading();
    }
    refreshThreads().catch(() => {});
    if (opts.prefetchModels) loadModels().catch(() => {});
  }

  async function loadChatPage() {
    if (!$("chatThreadList")) return;
    try {
      await Promise.all([loadModels(), refreshThreads()]);
      // Paint Recents immediately; open the full thread after.
      const fromHash = threadIdFromHash();
      if (fromHash && threads.some((t) => t.id === fromHash)) {
        await openThread(fromHash);
      } else if (activeThreadId && threads.some((t) => t.id === activeThreadId)) {
        await openThread(activeThreadId);
      } else if (threads.length) {
        await openThread(threads[0].id);
      } else {
        $("chatMessages").innerHTML = emptyStateHtml();
        scheduleUsageRefresh();
      }
      const draft = sessionStorage.getItem("captionToolBcChatDraft");
      if (draft) {
        sessionStorage.removeItem("captionToolBcChatDraft");
        const input = $("chatInput");
        if (input) {
          input.value = draft;
          input.dispatchEvent(new Event("input"));
        }
      }
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  window.initChat = initChat;
  window.loadChatPage = loadChatPage;
  window.newChatFromBc = newChatFromBc;
  window.openPreviewModal = openPreviewModal;
  window.openCreatePlanReview = openCreatePlanReview;
  window.saveWorkspacePlanFromPreview = saveWorkspacePlanFromPreview;
})();
