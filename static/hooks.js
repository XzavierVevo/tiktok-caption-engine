/* Affiliate hook generator — UI (depends on core.js: $, api) */
"use strict";

(function () {
  const $ = (id) => window.$(id);
  const api = (...args) => window.api(...args);
  let offers = [];
  let selectedOfferId = "";
  let lastGenerated = [];
  let likedSet = new Set(); // lowercase texts for selected offer
  let generating = false;

  function fmtTime(ts) {
    if (!ts) return "";
    try {
      return new Date(ts * 1000).toLocaleString();
    } catch {
      return "";
    }
  }

  function offerById(id) {
    return offers.find((o) => o.id === id) || null;
  }

  function refreshLikedSet(offer) {
    likedSet = new Set();
    if (!offer) return;
    for (const item of offer.liked || []) {
      const t = (item.text || "").trim().toLowerCase();
      if (t) likedSet.add(t);
    }
  }

  function setStatus(msg, isError) {
    const el = $("hooksGenStatus");
    if (!el) return;
    el.textContent = msg || "";
    el.classList.toggle("is-error", !!isError);
  }

  function fillOfferSelects() {
    const gen = $("hooksGenOffer");
    const hist = $("hooksHistoryOffer");
    if (!gen || !hist) return;
    const genVal = gen.value || selectedOfferId;
    const histVal = hist.value;
    gen.innerHTML = "";
    hist.innerHTML = '<option value="">All offers</option>';
    if (!offers.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "No offers yet";
      gen.appendChild(opt);
    } else {
      for (const o of offers) {
        const a = document.createElement("option");
        a.value = o.id;
        a.textContent = `${o.name} (${o.winnerCount || 0} winners)`;
        gen.appendChild(a);
        const b = document.createElement("option");
        b.value = o.id;
        b.textContent = o.name;
        hist.appendChild(b);
      }
    }
    if (genVal && offers.some((o) => o.id === genVal)) gen.value = genVal;
    else if (selectedOfferId && offers.some((o) => o.id === selectedOfferId)) {
      gen.value = selectedOfferId;
    }
    if (histVal) hist.value = histVal;
  }

  function renderOffers() {
    const list = $("hooksOfferList");
    if (!list) return;
    list.innerHTML = "";
    if (!offers.length) {
      list.innerHTML = '<li class="hooks-empty">No offers yet — add one above.</li>';
      return;
    }
    for (const o of offers) {
      const li = document.createElement("li");
      if (o.id === selectedOfferId) li.classList.add("selected");
      li.dataset.id = o.id;
      li.innerHTML = `
        <div class="hooks-offer-meta">
          <div class="hooks-offer-name"></div>
          <div class="hooks-offer-sub"></div>
          <div class="hooks-offer-stats"></div>
        </div>
        <div class="hooks-offer-actions">
          <button type="button" class="btn ghost hooks-select-btn">Select</button>
          <button type="button" class="btn ghost hooks-edit-btn">Edit</button>
          <button type="button" class="btn danger hooks-del-btn">Delete</button>
        </div>`;
      li.querySelector(".hooks-offer-name").textContent = o.name;
      const urlEl = li.querySelector(".hooks-offer-sub");
      if (o.url) {
        urlEl.textContent = o.url;
        urlEl.title = o.url;
      } else {
        urlEl.textContent = "No link set";
        urlEl.classList.add("hooks-muted");
      }
      const statsEl = li.querySelector(".hooks-offer-stats");
      statsEl.innerHTML = `
        <span class="hooks-stat">${o.winnerCount || 0} winners</span>
        <span class="hooks-stat">${o.likedCount || 0} liked</span>`;
      if (o.payoutType && o.payoutType !== "unknown") {
        const pay = o.payoutType === "rev_share"
          ? (o.payoutSharePercent ? o.payoutSharePercent + "% share" : "rev share")
          : (o.payoutAmount ? "$" + o.payoutAmount : o.payoutType);
        const paySpan = document.createElement("span");
        paySpan.className = "hooks-stat";
        paySpan.textContent = pay;
        statsEl.appendChild(paySpan);
      }
      li.querySelector(".hooks-select-btn").addEventListener("click", () => selectOffer(o.id));
      li.querySelector(".hooks-edit-btn").addEventListener("click", () => startEditOffer(o));
      li.querySelector(".hooks-del-btn").addEventListener("click", () => deleteOffer(o.id, o.name));
      li.addEventListener("click", (e) => {
        if (e.target.closest("button")) return;
        selectOffer(o.id);
      });
      list.appendChild(li);
    }
  }

  function renderWinners() {
    const hint = $("hooksWinnerHint");
    const body = $("hooksWinnerBody");
    const list = $("hooksWinnerList");
    const offer = offerById(selectedOfferId);
    if (!selectedOfferId || !offer) {
      hint.textContent = "Select an offer to manage winner hooks.";
      hint.classList.remove("hidden");
      body.classList.add("hidden");
      return;
    }
    hint.classList.add("hidden");
    body.classList.remove("hidden");
    list.innerHTML = "";
    const winners = offer.winners || [];
    if (!winners.length) {
      list.innerHTML = '<li class="hooks-empty">No winners yet — paste one that converted and click Add.</li>';
      return;
    }
    for (const w of winners) {
      const li = document.createElement("li");
      li.innerHTML = `
        <div class="hooks-item-text"></div>
        <div class="hooks-item-actions">
          <button type="button" class="btn ghost hooks-win-edit">Edit</button>
          <button type="button" class="btn danger hooks-win-del">Delete</button>
        </div>`;
      li.querySelector(".hooks-item-text").textContent = w.text;
      li.querySelector(".hooks-win-edit").addEventListener("click", () => {
        $("hooksWinnerInput").value = w.text;
        $("hooksWinnerEditId").value = w.id;
        $("hooksWinnerAdd").textContent = "Save";
        $("hooksWinnerInput").focus();
      });
      li.querySelector(".hooks-win-del").addEventListener("click", async () => {
        if (!confirm("Delete this winner hook?")) return;
        try {
          await api(`/api/hooks/offers/${offer.id}/winners/${w.id}`, { method: "DELETE" });
          await loadOffers({ keepSelection: true });
        } catch (err) {
          alert(err.message || String(err));
        }
      });
      list.appendChild(li);
    }
  }

  function renderResults(hooks, opts = {}) {
    const list = $("hooksGenResults");
    if (!list) return;
    list.innerHTML = "";
    lastGenerated = Array.isArray(hooks) ? hooks.slice() : [];
    const showActions = !opts.readonly;
    $("hooksUseCaptionsBtn").classList.toggle("hidden", !lastGenerated.length || !showActions);
    $("hooksCopyAllBtn").classList.toggle("hidden", !lastGenerated.length || !showActions);
    if (!lastGenerated.length) return;

    for (const text of lastGenerated) {
      const li = document.createElement("li");
      const liked = likedSet.has((text || "").trim().toLowerCase());
      if (liked) li.classList.add("liked");
      li.innerHTML = `
        <div class="hooks-result-text"></div>
        <div class="hooks-result-actions">
          <button type="button" class="btn ghost hooks-copy">Copy</button>
          <button type="button" class="btn ghost hooks-like">${liked ? "Unlike" : "Like"}</button>
          <button type="button" class="btn hooks-save-winner">Save as winner</button>
        </div>`;
      li.querySelector(".hooks-result-text").textContent = text;
      li.querySelector(".hooks-copy").addEventListener("click", async () => {
        try {
          await navigator.clipboard.writeText(text);
        } catch {
          /* ignore */
        }
      });
      li.querySelector(".hooks-like").addEventListener("click", () => toggleLike(text, li));
      li.querySelector(".hooks-save-winner").addEventListener("click", () => saveAsWinner(text));
      list.appendChild(li);
    }
  }

  function renderHistoryRuns(runs) {
    const wrap = $("hooksHistoryList");
    if (!wrap) return;
    wrap.innerHTML = "";
    if (!runs || !runs.length) {
      wrap.innerHTML = '<div class="hooks-empty">No generation history for this filter yet.</div>';
      return;
    }
    const offerNames = Object.fromEntries(offers.map((o) => [o.id, o.name]));
    for (const run of runs) {
      const card = document.createElement("div");
      card.className = "hooks-history-run";
      const modeLabel =
        run.mode === "new_angles"
          ? "New angles"
          : run.mode === "mix"
            ? "Mix"
            : run.mode === "chat"
              ? "Chat"
              : "Variations";
      const head = document.createElement("div");
      head.className = "hooks-history-head";
      head.innerHTML = `<strong></strong><span class="hooks-muted"></span>`;
      head.querySelector("strong").textContent =
        offerNames[run.offerId] || run.offerId || "Offer";
      head.querySelector(".hooks-muted").textContent =
        `${modeLabel} · ${run.count} · ${fmtTime(run.createdAt)}`;
      card.appendChild(head);
      if (run.extraInstructions) {
        const extra = document.createElement("div");
        extra.className = "hooks-muted";
        extra.textContent = "Extra: " + run.extraInstructions;
        card.appendChild(extra);
      }
      const ul = document.createElement("ul");
      ul.className = "hooks-result-list";
      for (const text of run.hooks || []) {
        const li = document.createElement("li");
        const liked = likedSet.has((text || "").trim().toLowerCase());
        if (liked) li.classList.add("liked");
        li.innerHTML = `
          <div class="hooks-result-text"></div>
          <div class="hooks-result-actions">
            <button type="button" class="btn ghost hooks-copy">Copy</button>
            <button type="button" class="btn ghost hooks-like">${liked ? "Unlike" : "Like"}</button>
            <button type="button" class="btn hooks-save-winner">Save as winner</button>
          </div>`;
        li.querySelector(".hooks-result-text").textContent = text;
        const offerId = run.offerId;
        li.querySelector(".hooks-copy").addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(text);
          } catch {}
        });
        li.querySelector(".hooks-like").addEventListener("click", async () => {
          if (!offerId) return;
          await toggleLike(text, li, offerId);
        });
        li.querySelector(".hooks-save-winner").addEventListener("click", async () => {
          if (!offerId) return;
          await saveAsWinner(text, offerId);
        });
        ul.appendChild(li);
      }
      card.appendChild(ul);
      wrap.appendChild(card);
    }
  }

  async function loadOffers(opts = {}) {
    offers = await api("/api/hooks/offers");
    if (!opts.keepSelection || !offers.some((o) => o.id === selectedOfferId)) {
      const prefer = window.selectedHookOfferId || "";
      if (prefer && offers.some((o) => o.id === prefer)) {
        selectedOfferId = prefer;
      } else if (!offers.some((o) => o.id === selectedOfferId)) {
        selectedOfferId = offers[0] ? offers[0].id : "";
      }
    }
    window.selectedHookOfferId = selectedOfferId;
    refreshLikedSet(offerById(selectedOfferId));
    fillOfferSelects();
    renderOffers();
    renderWinners();
  }

  function selectOffer(id) {
    selectedOfferId = id || "";
    window.selectedHookOfferId = selectedOfferId;
    refreshLikedSet(offerById(selectedOfferId));
    if ($("hooksGenOffer") && selectedOfferId) $("hooksGenOffer").value = selectedOfferId;
    if ($("hooksHistoryOffer")) $("hooksHistoryOffer").value = selectedOfferId;
    renderOffers();
    renderWinners();
    loadHistory().catch(() => {});
    if (typeof window.scheduleWorkspaceSave === "function") window.scheduleWorkspaceSave();
  }

  function clearOfferForm() {
    $("hooksOfferName").value = "";
    $("hooksOfferUrl").value = "";
    $("hooksOfferNotes").value = "";
    if ($("hooksOfferPayoutType")) $("hooksOfferPayoutType").value = "unknown";
    if ($("hooksOfferPayoutAmount")) $("hooksOfferPayoutAmount").value = "";
    if ($("hooksOfferPayoutShare")) $("hooksOfferPayoutShare").value = "";
    if ($("hooksOfferPayoutNote")) $("hooksOfferPayoutNote").value = "";
    $("hooksOfferEditId").value = "";
    $("hooksOfferSave").textContent = "Add offer";
    $("hooksOfferCancelEdit").classList.add("hidden");
  }

  function startEditOffer(o) {
    $("hooksOfferName").value = o.name || "";
    $("hooksOfferUrl").value = o.url || "";
    $("hooksOfferNotes").value = o.notes || "";
    if ($("hooksOfferPayoutType")) $("hooksOfferPayoutType").value = o.payoutType || "unknown";
    if ($("hooksOfferPayoutAmount")) $("hooksOfferPayoutAmount").value = o.payoutAmount || "";
    if ($("hooksOfferPayoutShare")) $("hooksOfferPayoutShare").value = o.payoutSharePercent || "";
    if ($("hooksOfferPayoutNote")) $("hooksOfferPayoutNote").value = o.payoutNote || "";
    $("hooksOfferEditId").value = o.id;
    $("hooksOfferSave").textContent = "Save changes";
    $("hooksOfferCancelEdit").classList.remove("hidden");
    $("hooksOfferName").focus();
  }

  async function saveOffer() {
    const name = ($("hooksOfferName").value || "").trim();
    const url = ($("hooksOfferUrl").value || "").trim();
    const notes = ($("hooksOfferNotes").value || "").trim();
    const payoutType = ($("hooksOfferPayoutType") && $("hooksOfferPayoutType").value) || "unknown";
    const payoutAmount = ($("hooksOfferPayoutAmount") && $("hooksOfferPayoutAmount").value) || "";
    const payoutSharePercent = ($("hooksOfferPayoutShare") && $("hooksOfferPayoutShare").value) || "";
    const payoutNote = ($("hooksOfferPayoutNote") && $("hooksOfferPayoutNote").value) || "";
    const payout = {
      payoutType,
      payoutAmount: payoutAmount === "" ? null : Number(payoutAmount),
      payoutSharePercent: payoutSharePercent === "" ? null : Number(payoutSharePercent),
      payoutNote,
      currency: "USD",
    };
    const editId = ($("hooksOfferEditId").value || "").trim();
    if (!name) {
      alert("Offer name is required");
      return;
    }
    try {
      if (editId) {
        await api(`/api/hooks/offers/${editId}`, {
          method: "PUT",
          body: JSON.stringify({ name, url, notes, ...payout }),
        });
      } else {
        const created = await api("/api/hooks/offers", {
          method: "POST",
          body: JSON.stringify({ name, url, notes, ...payout }),
        });
        selectedOfferId = created.id;
      }
      clearOfferForm();
      await loadOffers({ keepSelection: true });
      await loadHistory();
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function deleteOffer(id, name) {
    if (!confirm(`Delete offer “${name}” and its winners/history?`)) return;
    try {
      await api(`/api/hooks/offers/${id}`, { method: "DELETE" });
      if (selectedOfferId === id) selectedOfferId = "";
      clearOfferForm();
      await loadOffers();
      await loadHistory();
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function saveWinner() {
    const offer = offerById(selectedOfferId);
    if (!offer) {
      alert("Select an offer first");
      return;
    }
    const text = ($("hooksWinnerInput").value || "").trim();
    const editId = ($("hooksWinnerEditId").value || "").trim();
    if (!text) {
      alert("Paste a winner hook first");
      return;
    }
    try {
      if (editId) {
        await api(`/api/hooks/offers/${offer.id}/winners/${editId}`, {
          method: "PUT",
          body: JSON.stringify({ text }),
        });
      } else {
        await api(`/api/hooks/offers/${offer.id}/winners`, {
          method: "POST",
          body: JSON.stringify({ text }),
        });
      }
      $("hooksWinnerInput").value = "";
      $("hooksWinnerEditId").value = "";
      $("hooksWinnerAdd").textContent = "Add";
      await loadOffers({ keepSelection: true });
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function toggleLike(text, li, offerId) {
    const oid = offerId || selectedOfferId || ($("hooksGenOffer") && $("hooksGenOffer").value);
    if (!oid) {
      alert("Select an offer first");
      return;
    }
    const key = (text || "").trim().toLowerCase();
    const wasLiked = likedSet.has(key);
    try {
      if (wasLiked) {
        const q = new URLSearchParams({ text });
        await api(`/api/hooks/offers/${oid}/likes?${q}`, { method: "DELETE" });
        likedSet.delete(key);
        if (li) {
          li.classList.remove("liked");
          const btn = li.querySelector(".hooks-like");
          if (btn) btn.textContent = "Like";
        }
      } else {
        await api(`/api/hooks/offers/${oid}/likes`, {
          method: "POST",
          body: JSON.stringify({ text }),
        });
        likedSet.add(key);
        if (li) {
          li.classList.add("liked");
          const btn = li.querySelector(".hooks-like");
          if (btn) btn.textContent = "Unlike";
        }
      }
      // Refresh counts on offers without wiping selection
      offers = await api("/api/hooks/offers");
      fillOfferSelects();
      renderOffers();
      if (oid === selectedOfferId) refreshLikedSet(offerById(selectedOfferId));
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function saveAsWinner(text, offerId) {
    const oid = offerId || selectedOfferId || ($("hooksGenOffer") && $("hooksGenOffer").value);
    if (!oid) {
      alert("Select an offer first");
      return;
    }
    try {
      await api(`/api/hooks/offers/${oid}/winners`, {
        method: "POST",
        body: JSON.stringify({ text }),
      });
      if (oid === selectedOfferId || !selectedOfferId) {
        selectedOfferId = oid;
      }
      await loadOffers({ keepSelection: true });
    } catch (err) {
      alert(err.message || String(err));
    }
  }

  async function generate() {
    if (generating) return;
    const offerId = ($("hooksGenOffer") && $("hooksGenOffer").value) || "";
    if (!offerId) {
      setStatus("Add and select an offer first.", true);
      return;
    }
    const count = Number(($("hooksGenCount") && $("hooksGenCount").value) || 5);
    const mode = ($("hooksGenMode") && $("hooksGenMode").value) || "variations";
    const extra = ($("hooksGenExtra") && $("hooksGenExtra").value) || "";
    const randomWords = !$("hooksRandomWord") || $("hooksRandomWord").checked;
    generating = true;
    $("hooksGenerateBtn").disabled = true;
    setStatus("Generating…");
    try {
      const start = await api("/api/hooks/generate", {
        method: "POST",
        body: JSON.stringify({
          offerId,
          count,
          mode,
          extraInstructions: extra,
          randomWords,
        }),
      });
      const jobId = start.jobId || start.id;
      if (!jobId) throw new Error("No generate job id returned");
      try {
        localStorage.setItem("captionToolHooksGenJob", jobId);
      } catch {}
      await pollHookJob(jobId, offerId);
    } catch (err) {
      setStatus(err.message || String(err), true);
      generating = false;
      $("hooksGenerateBtn").disabled = false;
    }
  }

  async function pollHookJob(jobId, offerId) {
    generating = true;
    if ($("hooksGenerateBtn")) $("hooksGenerateBtn").disabled = true;
    setStatus("Generating… (safe to close this tab — it keeps going)");
    const poll = async () => {
      const st = await api(`/api/hooks/generate/${jobId}`);
      if (st.status === "running" || st.status === "queued") {
        return true;
      }
      try {
        localStorage.removeItem("captionToolHooksGenJob");
      } catch {}
      generating = false;
      if ($("hooksGenerateBtn")) $("hooksGenerateBtn").disabled = false;
      if (st.status === "failed") {
        setStatus(st.error || "Generation failed", true);
        return false;
      }
      selectedOfferId = offerId || st.offerId || selectedOfferId;
      refreshLikedSet(offerById(selectedOfferId));
      renderResults(st.hooks || []);
      const words = Array.isArray(st.randomWords) ? st.randomWords.filter(Boolean) : [];
      const wordNote = words.length ? ` Words: ${words.join(", ")}.` : "";
      setStatus(`Generated ${(st.hooks || []).length} hooks.${wordNote}`);
      await loadHistory();
      return false;
    };
    if (typeof window.pollUntil === "function") {
      window.pollUntil(async () => {
        try {
          return await poll();
        } catch (err) {
          const msg = String(err && err.message ? err.message : err);
          if (/not found|404/i.test(msg)) {
            try {
              localStorage.removeItem("captionToolHooksGenJob");
            } catch {}
            generating = false;
            if ($("hooksGenerateBtn")) $("hooksGenerateBtn").disabled = false;
            setStatus("Generate job lost (server restarted).", true);
            return false;
          }
          return { continue: true, interval: 2500 };
        }
      }, { interval: 1500, key: "hooks-gen:" + jobId });
    } else {
      // Fallback if app.js pollUntil is not ready yet
      while (true) {
        const cont = await poll();
        if (!cont) break;
        await new Promise((r) => setTimeout(r, 1500));
      }
    }
  }

  async function resumeHookJobs() {
    let jobId = "";
    try {
      jobId = localStorage.getItem("captionToolHooksGenJob") || "";
    } catch {}
    try {
      const active = await api("/api/jobs/active");
      const hooksJobs = (active && active.hooks) || [];
      if (hooksJobs.length && hooksJobs[0].jobId) {
        jobId = hooksJobs[0].jobId;
      }
    } catch {}
    if (!jobId) return;
    try {
      const st = await api(`/api/hooks/generate/${jobId}`);
      if (st.status === "running" || st.status === "queued") {
        await pollHookJob(jobId, st.offerId || "");
      } else if (st.status === "done") {
        try {
          localStorage.removeItem("captionToolHooksGenJob");
        } catch {}
        selectedOfferId = st.offerId || selectedOfferId;
        refreshLikedSet(offerById(selectedOfferId));
        renderResults(st.hooks || []);
        setStatus(`Generated ${(st.hooks || []).length} hooks.`);
        await loadHistory();
      } else {
        try {
          localStorage.removeItem("captionToolHooksGenJob");
        } catch {}
      }
    } catch {
      try {
        localStorage.removeItem("captionToolHooksGenJob");
      } catch {}
    }
  }

  async function loadHistory() {
    const offerId = ($("hooksHistoryOffer") && $("hooksHistoryOffer").value) || "";
    const q = offerId ? `?offerId=${encodeURIComponent(offerId)}` : "";
    // When filtering by a specific offer, align liked state for like buttons
    if (offerId) {
      refreshLikedSet(offerById(offerId));
    } else if (selectedOfferId) {
      refreshLikedSet(offerById(selectedOfferId));
    }
    const runs = await api(`/api/hooks/history${q}`);
    renderHistoryRuns(runs);
  }

  function useAsCaptions() {
    if (!lastGenerated.length) return;
    const ta = $("captionsInput");
    if (!ta) return;
    const block = lastGenerated.join("\n");
    const existing = ta.value.trim();
    ta.value = existing ? existing + "\n" + block : block;
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    if (typeof window.scheduleWorkspaceSave === "function") window.scheduleWorkspaceSave();
    if (typeof window.updatePreviewSelectors === "function") window.updatePreviewSelectors();
    window.location.hash = "#/create";
    // Close offers panel if open
    const panel = document.getElementById("chatOffersPanel");
    if (panel) panel.classList.add("hidden");
  }

  async function copyAll() {
    if (!lastGenerated.length) return;
    try {
      await navigator.clipboard.writeText(lastGenerated.join("\n"));
      setStatus("Copied all hooks.");
    } catch {
      setStatus("Could not copy to clipboard.", true);
    }
  }

  let _hooksInited = false;
  function initHooks() {
    if (!$("hooksOfferSave")) return;
    if (_hooksInited) return;
    _hooksInited = true;

    $("hooksOfferSave").addEventListener("click", () => saveOffer());
    $("hooksOfferCancelEdit").addEventListener("click", () => clearOfferForm());
    $("hooksWinnerAdd").addEventListener("click", () => saveWinner());
    $("hooksWinnerInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        e.preventDefault();
        saveWinner();
      }
    });
    $("hooksGenerateBtn").addEventListener("click", () => generate());
    $("hooksUseCaptionsBtn").addEventListener("click", () => useAsCaptions());
    $("hooksCopyAllBtn").addEventListener("click", () => copyAll());
    $("hooksGenOffer").addEventListener("change", () => {
      const id = $("hooksGenOffer").value;
      if (id) selectOffer(id);
    });
    $("hooksHistoryOffer").addEventListener("change", () => {
      loadHistory().catch((err) => setStatus(err.message || String(err), true));
    });
  }

  async function loadHooksPage() {
    if (!$("hooksOfferList")) return;
    try {
      await loadOffers({ keepSelection: true });
      await loadHistory();
      await resumeHookJobs();
    } catch (err) {
      setStatus(err.message || String(err), true);
    }
  }

  window.initHooks = initHooks;
  window.loadHooksPage = loadHooksPage;
  window.resumeHookJobs = resumeHookJobs;

  window.addEventListener("captionToolReady", () => {
    resumeHookJobs().catch(() => {});
  });
})();
