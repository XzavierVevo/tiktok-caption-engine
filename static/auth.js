/* Firebase Google sign-in gate.
 *
 * Fetches /api/auth-config to learn the mode:
 *   firebase → full login gate with Google popup
 *   dev      → local development, no auth (uid "dev" server-side)
 *
 * Exposes window.__auth = { token, user, mode, getToken(), signOut() } and
 * resolves window.AuthReady once the user is signed in (app.js boots then).
 *
 * Reveal the app shell as soon as Firebase reports a user — do not wait on
 * getIdToken() for paint. Token fetch continues in parallel.
 */

const authState = window.__auth;

function show(el, on) {
  if (el) el.classList.toggle("hidden", !on);
}

function revealApp(user) {
  show(document.getElementById("loginScreen"), false);
  show(document.getElementById("appShell"), true);
  const chip = document.getElementById("userChip");
  const avatar = document.getElementById("userAvatar");
  const name = document.getElementById("userName");
  if (user) {
    if (avatar) {
      if (user.photoURL) {
        avatar.src = user.photoURL;
        avatar.classList.remove("hidden");
        avatar.textContent = "";
      } else {
        avatar.removeAttribute("src");
        const initials = (user.displayName || user.email || "?").trim().slice(0, 2).toUpperCase();
        avatar.alt = initials;
        avatar.dataset.initials = initials;
      }
    }
    if (name) name.textContent = user.displayName || user.email || "";
    if (chip) chip.classList.remove("hidden");
  } else {
    if (name) name.textContent = "Dev mode";
    if (avatar) {
      avatar.removeAttribute("src");
      avatar.alt = "DV";
    }
  }
}

function setLoginStatus(msg, isError) {
  const el = document.getElementById("loginStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.classList.toggle("is-error", !!isError);
}

function warmContainer() {
  // Fire-and-forget: wake the Python container after auth so boot APIs are warm.
  try {
    fetch("/healthz", { method: "GET", credentials: "same-origin" }).catch(() => {});
  } catch {}
  // Prefetch BC inventory once api()/bc.js are registered (AuthReady can resolve
  // before defer scripts finish).
  const kick = (attempt) => {
    if (typeof window.prefetchBcInventory === "function") {
      window.prefetchBcInventory();
      return;
    }
    if ((attempt || 0) < 40) {
      setTimeout(() => kick((attempt || 0) + 1), 50);
    }
  };
  setTimeout(() => kick(0), 0);
}

async function main() {
  setLoginStatus("Restoring session…");
  let cfg;
  try {
    // Wedged localhost (CLOSE_WAIT storm on 8765) otherwise hangs forever
    // on this fetch and never clears "Restoring session…".
    const ac =
      typeof AbortController !== "undefined" ? new AbortController() : null;
    const timer = ac
      ? setTimeout(() => ac.abort(), 8000)
      : null;
    try {
      const res = await fetch("/api/auth-config", ac ? { signal: ac.signal } : {});
      if (!res.ok) throw new Error(`auth-config ${res.status}`);
      cfg = await res.json();
    } finally {
      if (timer) clearTimeout(timer);
    }
  } catch {
    setLoginStatus(
      "Cannot reach the server. If local, run start.bat (or free_port_8765.ps1) then refresh.",
      true
    );
    return;
  }

  authState.mode = cfg.mode;

  if (cfg.mode === "dev") {
    authState.getToken = async () => null;
    authState.signOut = () => {};
    setLoginStatus("");
    revealApp(null);
    window.__resolveAuthReady();
    warmContainer();
    return;
  }

  if (cfg.mode !== "firebase" || !cfg.firebase || !cfg.firebase.apiKey) {
    setLoginStatus("Authentication is not configured on this server.", true);
    return;
  }

  const { initializeApp } = await import(
    "https://www.gstatic.com/firebasejs/10.12.2/firebase-app.js"
  );
  const {
    getAuth,
    GoogleAuthProvider,
    signInWithPopup,
    onIdTokenChanged,
    signOut,
  } = await import("https://www.gstatic.com/firebasejs/10.12.2/firebase-auth.js");

  const app = initializeApp(cfg.firebase);
  const auth = getAuth(app);
  const provider = new GoogleAuthProvider();
  let resolved = false;

  authState.getToken = async ({ force = false } = {}) => {
    if (!auth.currentUser) return null;
    if (!force && authState.token) return authState.token;
    const t = await auth.currentUser.getIdToken(!!force);
    authState.token = t;
    return t;
  };
  authState.signOut = async () => {
    await signOut(auth);
    window.location.reload();
  };

  onIdTokenChanged(auth, async (user) => {
    authState.user = user;
    if (user) {
      // Paint immediately — don't wait on getIdToken for the shell.
      setLoginStatus("");
      revealApp(user);
      // Reveal immediately, then fetch token in parallel so a hung getIdToken
      // cannot leave AuthReady unresolved with a painted-but-dead shell.
      if (!resolved) {
        resolved = true;
        window.__resolveAuthReady();
        warmContainer();
      }
      try {
        authState.token = await user.getIdToken();
      } catch {
        /* api() retries via getToken with its own timeout */
      }
    } else {
      authState.token = null;
      setLoginStatus("");
      show(document.getElementById("loginScreen"), true);
      show(document.getElementById("appShell"), false);
    }
  });

  // Keep the cached token fresh for ?auth= media URLs.
  setInterval(() => {
    if (auth.currentUser) {
      auth.currentUser.getIdToken(true).then((t) => { authState.token = t; }).catch(() => {});
    }
  }, 10 * 60 * 1000);

  const btn = document.getElementById("loginBtn");
  if (btn) {
    btn.addEventListener("click", async () => {
      setLoginStatus("Opening Google sign-in…");
      try {
        await signInWithPopup(auth, provider);
        setLoginStatus("");
      } catch (err) {
        setLoginStatus(err.message || "Sign-in failed", true);
      }
    });
  }
  const out = document.getElementById("signOutBtn");
  if (out) out.addEventListener("click", () => authState.signOut());
}

main();
