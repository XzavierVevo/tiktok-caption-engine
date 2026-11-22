/* Shared DOM / fetch / poll helpers. Loaded before app.js, chat.js, hooks.js, bc.js. */
"use strict";

function $(id) {
  return document.getElementById(id);
}

function escapeHtml(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function debounce(fn, ms) {
  let t = 0;
  return (...args) => {
    if (t) clearTimeout(t);
    t = setTimeout(() => {
      t = 0;
      fn(...args);
    }, ms);
  };
}

function showToast(msg, { error = false, ms = 4000 } = {}) {
  let host = document.getElementById("appToastHost");
  if (!host) {
    host = document.createElement("div");
    host.id = "appToastHost";
    host.className = "app-toast-host";
    host.setAttribute("aria-live", "polite");
    document.body.appendChild(host);
  }
  const el = document.createElement("div");
  el.className = "app-toast" + (error ? " is-error" : "");
  el.textContent = String(msg || "");
  host.appendChild(el);
  setTimeout(() => {
    el.classList.add("is-out");
    setTimeout(() => el.remove(), 280);
  }, ms);
}

function showModal(id, on) {
  const el = typeof id === "string" ? document.getElementById(id) : id;
  if (!el) return;
  el.classList.toggle("hidden", !on);
  syncModalOpen();
}

function syncModalOpen() {
  const open = [...document.querySelectorAll(".crop-modal, .slide-over, .emoji-picker, dialog")].some(
    (el) => el && !el.classList.contains("hidden") && el.getAttribute("hidden") == null
  );
  document.body.classList.toggle("modal-open", open);
}

async function authHeaders({ force = false, timeoutMs = 8000 } = {}) {
  const auth = window.__auth;
  if (!auth) return {};
  let token = !force && auth.token ? auth.token : null;
  if (!token && auth.getToken) {
    try {
      const got = auth.getToken({ force });
      if (timeoutMs > 0) {
        token = await Promise.race([
          got,
          new Promise((_, rej) =>
            setTimeout(() => rej(new Error("auth timeout")), timeoutMs)
          ),
        ]);
      } else {
        token = await got;
      }
    } catch {
      token = auth.token || null;
    }
  } else if (!token) {
    token = auth.token || null;
  }
  return token ? { Authorization: "Bearer " + token } : {};
}

function withAuth(url) {
  const token = window.__auth && window.__auth.token;
  // Native <img>, <video>, and download navigation cannot attach the
  // Authorization header used by api(). The API explicitly accepts `auth`
  // for these same-origin resource URLs.
  if (token && typeof url === "string" && /^\/api(?:\/|\?|$)/.test(url)) {
    const sep = url.includes("?") ? "&" : "?";
    return `${url}${sep}auth=${encodeURIComponent(token)}`;
  }
  return url;
}

async function setAuthSrc(el, path) {
  if (!el) return;
  if (el._authObjectUrl) {
    try {
      URL.revokeObjectURL(el._authObjectUrl);
    } catch {
      /* ignore */
    }
    el._authObjectUrl = "";
  }
  if (!path) {
    el.removeAttribute("src");
    return;
  }
  const headers = await authHeaders();
  const res = await fetch(path, { headers });
  if (!res.ok) throw new Error(String(res.status));
  const blob = await res.blob();
  const obj = URL.createObjectURL(blob);
  el._authObjectUrl = obj;
  el.src = obj;
}

async function hydrateAuthMedia(root) {
  const scope = root || document;
  const nodes = scope.querySelectorAll ? scope.querySelectorAll("[data-auth-src]") : [];
  for (const el of nodes) {
    const path = el.getAttribute("data-auth-src");
    try {
      await setAuthSrc(el, path);
    } catch {
      el.removeAttribute("src");
    }
  }
}

async function api(path, opts = {}) {
  const timeoutMs = opts.timeoutMs;
  const fetchOpts = { ...opts };
  delete fetchOpts.timeoutMs;
  let timer = null;
  if (timeoutMs && !fetchOpts.signal && typeof AbortController !== "undefined") {
    const ac = new AbortController();
    fetchOpts.signal = ac.signal;
    timer = setTimeout(() => {
      try {
        ac.abort();
      } catch {
        /* ignore */
      }
    }, timeoutMs);
  }
  const doFetch = async (forceAuth) => {
    const auth = await authHeaders({ force: forceAuth });
    const headers = {
      ...auth,
      ...(fetchOpts.headers || {}),
    };
    if (fetchOpts.body != null && !(fetchOpts.body instanceof FormData) && !headers["Content-Type"]) {
      headers["Content-Type"] = "application/json";
    }
    return fetch(path, { ...fetchOpts, headers });
  };
  try {
    let res = await doFetch(false);
    if (res.status === 401 && window.__auth && window.__auth.getToken) {
      res = await doFetch(true);
    }
    if (!res.ok) {
      let detail = res.statusText;
      try {
        detail = (await res.json()).detail || detail;
      } catch {
        /* ignore */
      }
      throw new Error(detail);
    }
    const ct = res.headers.get("content-type") || "";
    return ct.includes("application/json") ? res.json() : res;
  } finally {
    if (timer) clearTimeout(timer);
  }
}

const _pollStops = Object.create(null);

function pollUntil(fn, { interval = 1500, key } = {}) {
  if (key && _pollStops[key]) _pollStops[key]();
  let timer = 0;
  let stopped = false;
  let fails = 0;
  const stop = () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    timer = 0;
    if (key && _pollStops[key] === stop) delete _pollStops[key];
  };
  if (key) _pollStops[key] = stop;
  const tick = async () => {
    if (stopped) return;
    let wait = interval;
    let cont = false;
    try {
      const result = await fn();
      if (result && typeof result === "object" && "continue" in result) {
        cont = !!result.continue;
        if (result.interval) wait = result.interval;
        if (cont) fails = 0;
      } else {
        cont = !!result;
        if (cont) fails = 0;
      }
    } catch (err) {
      fails += 1;
      console.warn(`Poll failed (attempt ${fails}/8):`, err);

      if (fails >= 8) {
        // Connection lost after 8 failures - notify user
        stop();

        // Show long-duration error notification (60 seconds)
        showToast(
          "⚠️ Connection lost - job monitoring stopped. Refresh page to reconnect.",
          { error: true, ms: 60000 }
        );

        console.error("Polling stopped after 8 consecutive failures for key:", key);
        return;
      }
      wait = Math.min(15000, Math.max(interval, 2500) * Math.pow(1.4, fails - 1));
      cont = true;
    }
    if (!stopped && cont) timer = setTimeout(tick, wait);
    else if (!cont) stop();
  };
  tick();
  return stop;
}

function stopPoll(key) {
  if (key && _pollStops[key]) _pollStops[key]();
}

window.$ = $;
window.escapeHtml = escapeHtml;
window.debounce = debounce;
window.showToast = showToast;
window.showModal = showModal;
window.syncModalOpen = syncModalOpen;
window.authHeaders = authHeaders;
window.withAuth = withAuth;
window.setAuthSrc = setAuthSrc;
window.hydrateAuthMedia = hydrateAuthMedia;
window.api = api;
window.pollUntil = pollUntil;
window.stopPoll = stopPoll;

async function authDownload(url, filename) {
  const res = await fetch(url, { headers: await authHeaders() });
  if (!res.ok) throw new Error(String(res.status));
  const blob = await res.blob();
  const obj = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = obj;
  a.download = filename || "download";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(obj), 30_000);
}
window.authDownload = authDownload;

/**
 * Shows immediate visual feedback when an async operation starts.
 * Call this BEFORE the API request to show status to the user immediately.
 *
 * @param {Object} opts Options
 * @param {string} opts.message - Message to display in toast notification
 * @param {boolean} opts.showDock - Whether to show the progress dock (default: true)
 * @param {boolean} opts.expandDock - Whether to expand the dock (default: false)
 * @param {string} opts.toastMs - Duration for toast (default: 3000)
 *
 * @example
 * showAsyncStartFeedback({
 *   message: "Fetching Spark codes…",
 *   showDock: true,
 *   expandDock: false
 * });
 */
function showAsyncStartFeedback(opts = {}) {
  const { message = "Starting…", showDock = true, expandDock = false, toastMs = 3000 } = opts;

  // Show toast notification immediately
  if (message) {
    showToast(message, { ms: toastMs });
  }

  // Show progress dock if requested
  if (showDock && typeof window.showProgressDock === "function") {
    window.showProgressDock({ expand: expandDock });
  }
}
window.showAsyncStartFeedback = showAsyncStartFeedback;
