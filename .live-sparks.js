(function () {
  const $ = (id) => document.getElementById(id);
  const CACHE_KEY = "captionToolSparksCache_v1";
  let rows = [];
  let offers = [];
  let postbackUrl = "";
  let loading = false;

  function readCache() {
    try {
      const raw = localStorage.getItem(CACHE_KEY);
      const data = raw ? JSON.parse(raw) : null;
      return data && Array.isArray(data.sparks) ? data : null;
    } catch (err) {
      return null;
    }
  }
  function writeCache(data) {
    try {
      localStorage.setItem(CACHE_KEY, JSON.stringify({
        sparks: data.sparks || [],
        offers: data.offers || [],
        variants: data.variants || [],
        syncedAt: data.syncedAt || "",
      }));
    } catch (err) {}
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }
  function money(value) {
    return Number(value || 0).toLocaleString(undefined, { style: "currency", currency: "USD" });
  }
  function num(value) {
    return Math.round(Number(value || 0)).toLocaleString();
  }
  const openGroups = new Set();

  function isPlaceholderReco(row) {
    return !String(row.adId || "").trim() && /^reco-\d+$/i.test(String(row.adName || "").trim());
  }

  function isRealRun(row) {
    if (!String(row.launchDate || "").trim()) return false;
    const item = String(row.itemId || "").trim();
    const ad = String(row.adId || "").trim();
    if (item && ad && item === ad) {
      return Number(row.spend || 0) > 0
        || Number(row.impressions || 0) > 0
        || Number(row.clicks || 0) > 0;
    }
    return Boolean(ad || item);
  }

  function accountOf(row) {
    const label = String(row.accountName || "")
      .replace(/\u00c2\u00b7/g, "/")
      .replace(/\u00b7/g, "/")
      .replace(/\uFFFD/g, "")
      .replace(/\s*\/\s*/g, " / ")
      .trim();
    if (label) return label;
    return [row.profileNo, row.bcName, row.accountNumber]
      .map((part) => String(part || "").trim())
      .filter(Boolean)
      .join(" · ") || "—";
  }

  function isPayloadRunRow(row) {
    const name = String(row.adName || "");
    const lander = String(row.landerId || row.landerName || "").toLowerCase();
    if (lander.includes("payload-reco") || lander.includes("payload reco")) return true;
    return /^image carousel 1_payload reco/i.test(name);
  }

  function statusOf(row) {
    const op = String(row.operationStatus || "").toUpperCase();
    const sec = String(row.secondaryStatus || "").toUpperCase();
    const delivered = Number(row.spend || 0) > 0
      || Number(row.impressions || 0) > 0
      || Number(row.clicks || 0) > 0;
    if (/PUNISH|SUSPEND|FROZEN|AUDIT_DENY|REJECT/.test(sec)) return { key: "suspended", label: "Suspended" };
    if (sec.includes("EXCEED")) return { key: "budget", label: "Budget" };
    if (sec.includes("DISABLE") || sec.includes("DELETE") || op === "DISABLE") {
      return delivered ? { key: "suspended", label: "Suspended" } : { key: "off", label: "Off" };
    }
    if (sec === "AD_STATUS_DELIVERY_OK" && (op === "ENABLE" || op === "")) return { key: "running", label: "Running" };
    if (row.running === true) return { key: "running", label: "Running" };
    if (!op && !sec) return { key: "unknown", label: "—" };
    const label = sec.replace(/^AD_STATUS_/, "").replace(/_/g, " ").toLowerCase();
    return { key: label ? "off" : "unknown", label: label || "—" };
  }

  function statusSummary(rows) {
    const statuses = rows.map(statusOf);
    for (const key of ["suspended", "off", "budget", "running"]) {
      const matched = statuses.filter((status) => status.key === key);
      if (!matched.length) continue;
      const label = matched[0].label + (matched.length < rows.length ? ` (${matched.length}/${rows.length})` : "");
      return { key, label };
    }
    return { key: "unknown", label: "—" };
  }

  function statusBadge(status) {
    return `<span class="spark-status is-${status.key}">${escapeHtml(status.label)}</span>`;
  }

  function parseTime(value) {
    const text = String(value || "").trim();
    if (!text) return 0;
    const stamp = Date.parse(text);
    return Number.isFinite(stamp) ? stamp : 0;
  }

  function stoppedAfterMs(row) {
    const status = statusOf(row);
    if (!["suspended", "off", "budget"].includes(status.key)) return 0;
    const start = parseTime(row.approvedAt) || parseTime(row.createTime);
    const end = parseTime(row.stoppedAt || row.statusChangedAt);
    if (!start || !end || end <= start) return 0;
    return end - start;
  }

  function durationText(ms) {
    if (!ms) return "—";
    const minutes = Math.round(ms / 60000);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    const rest = minutes % 60;
    if (hours < 24) return rest ? `${hours}h ${rest}m` : `${hours}h`;
    const days = Math.floor(hours / 24);
    const tail = hours % 24;
    return tail ? `${days}d ${tail}h` : `${days}d`;
  }

  function profitOf(row) {
    return Number(row.affRevenue || 0) - Number(row.spend || 0);
  }

  function sumOf(list, key) {
    return list.reduce((total, row) => total + Number(row[key] || 0), 0);
  }

  function videoLink(row) {
    const postUrl = String(row.postUrl || "");
    const postMatch = postUrl.match(/tiktok\.com\/(?:@[\w.\-]+\/)?(video|photo)\/\d{15,20}/i);
    if (!postMatch) return "—";
    const label = postMatch[1].toLowerCase() === "photo" ? "TikTok photo" : "TikTok video";
    return `<a href="${escapeHtml(postUrl)}" target="_blank" rel="noopener noreferrer">${label}</a>`;
  }

  function adCells(row) {
    return `<tr>
        <td>${escapeHtml(row.adName || row.itemId || "—")}</td>
        <td>${videoLink(row)}</td>
        <td>${escapeHtml(row.launchDate || "—")}</td>
        <td>${statusBadge(statusOf(row))}</td>
        <td>${escapeHtml(row.offerName || row.offerId || "—")}</td>
        <td>${escapeHtml(row.landerName || "—")}</td>
        <td>${escapeHtml(row.networkName || "—")}</td>
        <td class="spark-code">${escapeHtml(row.campaignId || "—")}</td>
        <td>${escapeHtml(accountOf(row))}</td>
        <td>${money(row.spend)}</td>
        <td>${num(row.impressions)}</td>
        <td>${num(row.clicks)}</td>
        <td>${num(row.conversions)}</td>
        <td>${num(row.affConversions)}</td>
        <td>${money(row.affRevenue)}</td>
      </tr>`;
  }

  function sparkKey(row) {
    const item = String(row.itemId || "").trim();
    const ad = String(row.adId || "").trim();
    const lander = String(row.landerId || row.landerName || "").trim().toLowerCase();
    if (item && item !== ad) return `item:${item}|${lander}`;
    const name = String(row.adName || "").trim().toLowerCase();
    if (name) return `name:${name}|${lander}`;
    return `ad:${ad}`;
  }

  function isMislabeledPayload(row) {
    const name = String(row.adName || "");
    const lander = String(row.landerId || row.landerName || "").toLowerCase();
    const namedPayload = /payload[\s._-]*reco/i.test(name);
    const onPayload = lander.includes("payload-reco")
      || lander.includes("payload reco")
      || /asu\.playgetpaid\.com/i.test(String(row.landingUrl || ""));
    return namedPayload && !onPayload;
  }

  function originalOf(rows) {
    return [...rows].sort((a, b) => {
      const misA = isMislabeledPayload(a) ? 1 : 0;
      const misB = isMislabeledPayload(b) ? 1 : 0;
      if (misA !== misB) return misA - misB;
      const when = String(a.launchDate || "").localeCompare(String(b.launchDate || ""));
      if (when) return when;
      const adA = Number(a.adId || 0);
      const adB = Number(b.adId || 0);
      if (adA !== adB) return adA - adB;
      return String(a.adName || "").localeCompare(String(b.adName || ""));
    })[0];
  }

  function runsDetail(rows) {
    const lines = [...rows].sort((a, b) => {
      const when = String(b.launchDate || "").localeCompare(String(a.launchDate || ""));
      if (when) return when;
      return String(a.adName || "").localeCompare(String(b.adName || ""), undefined, { numeric: true });
    });
    return `<tr class="spark-fold-row"><td colspan="15"><table class="spark-launches"><thead><tr><th>Launched</th><th>Ad</th><th>Lander</th><th>Account / BC</th><th>Campaign</th><th>Status</th><th>Stopped after</th><th>Impr.</th><th>Clicks</th><th>Conv.</th><th>Aff conv.</th><th>Aff rev.</th><th>Aff CPA</th><th>Spend</th><th>Profit</th></tr></thead><tbody>${
      lines.map((row) => `<tr>
          <td>${escapeHtml(row.launchDate || "—")}</td>
          <td>${escapeHtml(row.adName || row.itemId || "—")}</td>
          <td>${escapeHtml(row.landerName || "—")}</td>
          <td>${escapeHtml(accountOf(row))}</td>
          <td class="spark-code">${escapeHtml(row.campaignId || "—")}</td>
          <td>${statusBadge(statusOf(row))}</td>
          <td>${durationText(stoppedAfterMs(row))}</td>
          <td>${num(row.impressions)}</td>
          <td>${num(row.clicks)}</td>
          <td>${num(row.conversions)}</td>
          <td>${num(row.affConversions)}</td>
          <td>${money(row.affRevenue)}</td>
          <td>${row.affConversions ? money(row.affCpa) : "—"}</td>
          <td>${money(row.spend)}</td>
          <td>${money(profitOf(row))}</td>
        </tr>`).join("")
    }</tbody></table></td></tr>`;
  }

  function payloadRun(rows) {
    const date = String(rows[0].launchDate || "");
    const campaigns = [...new Set(rows.map((row) => String(row.campaignId || "")).filter(Boolean))];
    const accounts = [...new Set(rows.map((row) => accountOf(row)).filter((label) => label && label !== "—"))];
    const suspendedRows = rows.filter((row) => statusOf(row).key === "suspended");
    const timings = (suspendedRows.length ? suspendedRows : rows)
      .map((row) => stoppedAfterMs(row))
      .filter((ms) => ms > 0);
    return {
      date,
      rows,
      campaigns,
      accounts,
      status: statusSummary(rows),
      stopped: timings.length ? Math.min(...timings) : 0,
      impressions: sumOf(rows, "impressions"),
      clicks: sumOf(rows, "clicks"),
      conversions: sumOf(rows, "conversions"),
      affConversions: sumOf(rows, "affConversions"),
      spend: sumOf(rows, "spend"),
      profit: rows.reduce((total, row) => total + profitOf(row), 0),
    };
  }

  function payloadDetail(runs) {
    return `<tr class="spark-fold-row"><td colspan="15"><table class="spark-launches"><thead><tr><th>Launched</th><th>Ads</th><th>Campaign</th><th>Account / BC</th><th>Status</th><th>Stopped after</th><th>Impr.</th><th>Clicks</th><th>Conv.</th><th>Aff conv.</th><th>Spend</th><th>Profit</th></tr></thead><tbody>${
      runs.map((run) => `<tr>
          <td>${escapeHtml(run.date || "—")}</td>
          <td>${run.rows.length}</td>
          <td class="spark-code">${escapeHtml(run.campaigns.join(", ") || "—")}</td>
          <td>${escapeHtml(run.accounts.join(", ") || "—")}</td>
          <td>${statusBadge(run.status)}</td>
          <td>${durationText(run.stopped)}</td>
          <td>${num(run.impressions)}</td>
          <td>${num(run.clicks)}</td>
          <td>${num(run.conversions)}</td>
          <td>${num(run.affConversions)}</td>
          <td>${money(run.spend)}</td>
          <td>${money(run.profit)}</td>
        </tr>`).join("")
    }</tbody></table></td></tr>`;
  }

  function groupRow(group) {
    const rows = group.rows;
    const original = group.original;
    const spend = sumOf(rows, "spend");
    const launched = group.kind === "payload" && group.runs.length
      ? group.runs[0].date
      : original.launchDate || "";
    const open = openGroups.has(group.key);
    const parent = `<tr>
        <td><button type="button" class="spark-fold" data-fold="${escapeHtml(group.key)}" aria-expanded="${open ? "true" : "false"}"><span class="spark-chevron" aria-hidden="true"></span>${escapeHtml(group.name)}</button></td>
        <td>${group.count} run${group.count === 1 ? "" : "s"}</td>
        <td>${escapeHtml(launched || "—")}</td>
        <td>${statusBadge(statusSummary(rows))}</td>
        <td>${escapeHtml(original.offerName || original.offerId || "—")}</td>
        <td>${escapeHtml(original.landerName || "—")}</td>
        <td>${escapeHtml(original.networkName || "—")}</td>
        <td class="spark-code">${escapeHtml(original.campaignId || "—")}</td>
        <td>${escapeHtml(accountOf(original))}</td>
        <td>${money(spend)}</td>
        <td>${num(sumOf(rows, "impressions"))}</td>
        <td>${num(sumOf(rows, "clicks"))}</td>
        <td>${num(sumOf(rows, "conversions"))}</td>
        <td>${num(affConversions)}</td>
        <td>${money(sumOf(rows, "affRevenue"))}</td>
      </tr>`;
    if (!open) return parent;
    return parent + (group.kind === "payload" ? payloadDetail(group.runs) : runsDetail(rows));
  }

  function buildGroups(listed) {
    const payloadRows = listed.filter(isPayloadRunRow);
    const rest = listed.filter((row) => !isPayloadRunRow(row));
    const buckets = new Map();
    for (const row of rest) {
      const key = sparkKey(row);
      const bucket = buckets.get(key);
      if (bucket) bucket.push(row);
      else buckets.set(key, [row]);
    }
    const groups = [];
    for (const [key, bucket] of buckets) {
      const original = originalOf(bucket);
      if (bucket.length > 1) {
        groups.push({
          kind: "runs",
          key: "runs:" + key,
          name: String(original.adName || key),
          original,
          rows: bucket,
          count: bucket.length,
        });
      } else {
        groups.push({ kind: "ad", row: original, name: String(original.adName || "") });
      }
    }
    if (payloadRows.length) {
      const byDate = new Map();
      for (const row of payloadRows) {
        const date = String(row.launchDate || "").trim() || "unknown";
        const bucket = byDate.get(date);
        if (bucket) bucket.push(row);
        else byDate.set(date, [row]);
      }
      const runs = [...byDate.values()]
        .map(payloadRun)
        .sort((a, b) => b.date.localeCompare(a.date));
      groups.push({
        kind: "payload",
        key: "payload",
        name: "Payload spark 1",
        original: originalOf(runs[0].rows),
        rows: payloadRows,
        runs,
        count: runs.length,
      });
    }
    return groups;
  }

  const sortState = { key: "spend", dir: -1 };

  function sortValue(group, key) {
    const single = group.kind === "ad";
    const row = single ? group.row : group.original;
    if (key === "name") return group.name;
    if (key === "video") return String(row.postUrl || "");
    if (key === "launched") return String(row.launchDate || "");
    if (key === "offer") return String(row.offerName || row.offerId || "");
    if (key === "lander") return String(row.landerName || "");
    if (key === "network") return String(row.networkName || "");
    if (key === "campaign") return String(row.campaignId || "");
    if (key === "account") return accountOf(row);
    if (key === "status") return single ? statusOf(row).label : statusSummary(group.rows).label;
    const rows = single ? [group.row] : group.rows;
    return sumOf(rows, key);
  }

  function bindSort() {
    const table = $("sparkTable");
    if (!table || typeof table.querySelectorAll !== "function") return;
    table.querySelectorAll("thead th").forEach((th) => {
      const key = th.getAttribute("data-sort");
      if (!key) return;
      th.classList.add("is-sortable");
      if (th.dataset.sortBound) return;
      th.dataset.sortBound = "1";
      th.addEventListener("click", () => {
        if (sortState.key === key) sortState.dir = -sortState.dir;
        else {
          sortState.key = key;
          sortState.dir = key === "name" || key === "launched" ? 1 : -1;
        }
        renderTable();
      });
    });
  }

  function markSort() {
    const table = $("sparkTable");
    if (!table || typeof table.querySelectorAll !== "function") return;
    table.querySelectorAll("thead th").forEach((th) => {
      const key = th.getAttribute("data-sort") || "";
      if (key && key === sortState.key) th.dataset.dir = sortState.dir > 0 ? "asc" : "desc";
      else delete th.dataset.dir;
    });
  }

  function renderTable() {
    const body = $("sparkTableBody");
    if (!body) return;
    if (!body.dataset.foldBound) {
      body.dataset.foldBound = "1";
      body.addEventListener("click", (event) => {
        const button = event.target.closest(".spark-fold");
        if (!button) return;
        const key = button.getAttribute("data-fold") || "";
        if (openGroups.has(key)) openGroups.delete(key);
        else openGroups.add(key);
        renderTable();
      });
    }
    bindSort();
    const query = ($("sparkSearch")?.value || "").trim().toLowerCase();
    const listed = rows.filter((row) => !isPlaceholderReco(row) && isRealRun(row));
    const groups = buildGroups(listed);
    const visible = groups
      .filter((group) => !query || JSON.stringify(group).toLowerCase().includes(query))
      .sort((a, b) => {
        const av = sortValue(a, sortState.key);
        const bv = sortValue(b, sortState.key);
        const cmp = typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv), undefined, { numeric: true });
        return cmp * sortState.dir;
      });
    const count = $("sparkCount");
    if (count) count.textContent = `${visible.length} spark${visible.length === 1 ? "" : "s"}`;
    if (!visible.length) {
      body.innerHTML = `<tr><td colspan="15" class="hint">${rows.length ? "No sparks match that search." : "No sparks yet."}</td></tr>`;
      markSort();
      return;
    }
    body.innerHTML = visible.map((group) => (
      group.kind === "ad" ? adCells(group.row) : groupRow(group)
    )).join("");
    markSort();
  }

  function renderVariants(variants) {
    const host = $("sparkVariants");
    if (!host) return;
    if (!variants || !variants.length) {
      host.innerHTML = "";
      return;
    }
    host.innerHTML = `<table class="bc-table"><thead><tr><th>Lander variant</th><th>Conversions</th><th>Revenue</th></tr></thead><tbody>${
      variants.map((row) => `<tr><td>${escapeHtml(row.variant)}</td><td>${num(row.conversions)}</td><td>${money(row.revenue)}</td></tr>`).join("")
    }</tbody></table>`;
  }

  function field(label, value, attrs) {
    return `<label>${escapeHtml(label)}<input ${attrs} value="${escapeHtml(value || "")}" /></label>`;
  }

  function renderRouting() {
    const host = $("sparkRoutingBody");
    const chip = $("sparkRouteChip");
    if (!host) return;
    const active = offers.find((offer) => offer.postbackUrl) || offers[0];
    postbackUrl = (active && active.postbackUrl) || "";
    if (chip) {
      const status = active ? `${active.networkName || "No network"}${active.routingStatus ? " · " + active.routingStatus : ""}` : "No offers";
      chip.textContent = status;
    }
    if (!offers.length) {
      host.innerHTML = "<p class=\"hint\">Add an offer, then attach a lander and a network.</p>";
      return;
    }
    host.innerHTML = offers.map((offer) => {
      const lander = (offer.landers || [])[0] || {};
      const network = (offer.networks || []).find((item) => item.id === offer.activeNetworkId) || (offer.networks || [])[0] || {};
      const variant = (lander.variants || [])[0] || { path: "/", weight: 1, name: "A" };
      const variantB = (lander.variants || [])[1] || { path: "/v2/", weight: 0, name: "B" };
      return `<form class="spark-offer-card" data-offer-id="${escapeHtml(offer.id)}">
        <h3>${escapeHtml(offer.name || offer.id)}</h3>
        <div class="spark-mini">
          ${field("Lander name", lander.name, "name=\"landerName\" placeholder=\"Rico\"")}
          ${field("Lander origin", lander.origin, "name=\"landerOrigin\" placeholder=\"https://…\"")}
          ${field("Variant A path", variant.path, "name=\"pathA\"")}
          ${field("Variant A weight", variant.weight, "name=\"weightA\" type=\"number\" min=\"0\"")}
          ${field("Variant B path", variantB.path, "name=\"pathB\"")}
          ${field("Variant B weight", variantB.weight, "name=\"weightB\" type=\"number\" min=\"0\"")}
          ${field("Network", network.name || network.key, "name=\"networkName\" placeholder=\"Glitchy\"")}
          ${field("Click URL", network.clickUrl, "name=\"clickUrl\" placeholder=\"https://…\"")}
          ${field("Click id param", network.clickIdParam || "source", "name=\"clickIdParam\" placeholder=\"source\"")}
          ${field("Cap", network.capLimit || "", "name=\"capLimit\" type=\"number\" min=\"0\" placeholder=\"25\"")}
          <label>Cap mode<select name="capMode">
            <option value="manual"${network.capMode !== "api" ? " selected" : ""}>Manual count</option>
            <option value="api"${network.capMode === "api" ? " selected" : ""}>API (CKYA)</option>
          </select></label>
          <label>Window<select name="capWindow">
            <option value="daily"${network.capWindow !== "global" ? " selected" : ""}>Daily</option>
            <option value="global"${network.capWindow === "global" ? " selected" : ""}>Global</option>
          </select></label>
          ${field("API URL", network.apiUrl, "name=\"apiUrl\" placeholder=\"https://…/{offerId}\"")}
          ${field("API offer id", network.apiOfferId, "name=\"apiOfferId\"")}
          ${field("Fallback network", (offer.networks || [])[1] ? (offer.networks[1].name || "") : "", "name=\"networkName2\" placeholder=\"CKYA\"")}
          ${field("Fallback click URL", (offer.networks || [])[1] ? (offer.networks[1].clickUrl || "") : "", "name=\"clickUrl2\"")}
          ${field("Fallback cap", (offer.networks || [])[1] ? (offer.networks[1].capLimit || "") : "", "name=\"capLimit2\" type=\"number\" min=\"0\" placeholder=\"100\"")}
        </div>
        <div class="row gap wrap">
          <button class="btn primary" type="submit">Save routing</button>
          <span class="hint">${offer.capRemaining == null ? "" : "Cap left " + offer.capRemaining}</span>
        </div>
      </form>`;
    }).join("");
    host.querySelectorAll("form").forEach((form) => {
      form.addEventListener("submit", (event) => {
        event.preventDefault();
        saveRouting(form).catch((err) => {
          window.alert(err.message || "Could not save routing");
        });
      });
    });
  }

  function val(form, name) {
    const el = form.elements[name];
    return el ? String(el.value || "").trim() : "";
  }

  async function saveRouting(form) {
    const offerId = form.getAttribute("data-offer-id");
    const offer = offers.find((item) => item.id === offerId) || {};
    const lander = (offer.landers || [])[0] || {};
    const network = (offer.networks || [])[0] || {};
    const variants = [
      {
        id: ((lander.variants || [])[0] || {}).id || "",
        name: "A",
        path: val(form, "pathA") || "/",
        weight: Number(val(form, "weightA") || 0),
      },
    ];
    if (val(form, "pathB") || Number(val(form, "weightB") || 0) > 0) {
      variants.push({
        id: ((lander.variants || [])[1] || {}).id || "",
        name: "B",
        path: val(form, "pathB") || "/v2/",
        weight: Number(val(form, "weightB") || 0),
      });
    }
    const networks = [{
      id: network.id || "",
      key: (val(form, "networkName") || network.key || "custom").toLowerCase(),
      name: val(form, "networkName") || "Network",
      clickUrl: val(form, "clickUrl"),
      clickIdParam: val(form, "clickIdParam") || "source",
      capMode: val(form, "capMode") || "manual",
      capWindow: val(form, "capWindow") || "daily",
      capLimit: Number(val(form, "capLimit") || 0),
      apiUrl: val(form, "apiUrl"),
      apiOfferId: val(form, "apiOfferId"),
      fallbackOrder: 1,
    }];
    if (val(form, "clickUrl2")) {
      const second = (offer.networks || [])[1] || {};
      networks.push({
        id: second.id || "",
        key: (val(form, "networkName2") || second.key || "ckya").toLowerCase(),
        name: val(form, "networkName2") || "Fallback",
        clickUrl: val(form, "clickUrl2"),
        clickIdParam: second.clickIdParam || "s2",
        capMode: second.capMode || "manual",
        capWindow: second.capWindow || "global",
        capLimit: Number(val(form, "capLimit2") || second.capLimit || 0),
        apiUrl: second.apiUrl || "",
        apiOfferId: second.apiOfferId || "",
        fallbackOrder: 2,
      });
    }
    const body = {
      activeLanderId: offer.activeLanderId || lander.id || "",
      activeNetworkId: offer.activeNetworkId || network.id || "",
      landers: [{
        id: lander.id || "",
        name: val(form, "landerName") || "Lander",
        origin: val(form, "landerOrigin"),
        variants,
      }],
      networks,
    };
    await window.api(`/api/hooks/offers/${encodeURIComponent(offerId)}/routing`, {
      method: "PUT",
      body: JSON.stringify(body),
    });
    await loadSparksPage({ sync: false });
  }

  const SPARK_CACHE = "captionToolSparksCache_v1";
  let buttonWaiting = false;

  function readCache() {
    try {
      const raw = localStorage.getItem(SPARK_CACHE);
      return raw ? JSON.parse(raw) : null;
    } catch (err) {
      return null;
    }
  }

  function writeCache(data) {
    try {
      localStorage.setItem(SPARK_CACHE, JSON.stringify({
        sparks: data.sparks || [],
        offers: data.offers || [],
        variants: data.variants || [],
        savedAt: new Date().toISOString(),
      }));
    } catch (err) {}
  }

  function applyPayload(data) {
    const incoming = data && data.sparks;
    if (incoming && incoming.length) {
      rows = incoming;
      writeCache(data);
    }
    if (data && data.offers && data.offers.length) offers = data.offers;
    renderRouting();
    renderVariants((data && data.variants) || []);
    renderTable();
  }

  function showCached() {
    const cached = readCache();
    if (!cached || !(cached.sparks || []).length) return false;
    rows = cached.sparks;
    offers = cached.offers || offers;
    renderRouting();
    renderVariants(cached.variants || []);
    renderTable();
    const chip = $("sparkSyncChip");
    if (chip) chip.textContent = "Saved";
    return true;
  }

  async function loadSparksPage(opts) {
    const body = $("sparkTableBody");
    if (!body || typeof window.api !== "function") return;
    if (!rows.length) showCached();
    if (loading) return;
    loading = true;
    const chip = $("sparkSyncChip");
    if (chip && !rows.length) chip.textContent = "Loading…";
    try {
      const data = await window.api("/api/provision/sparks");
      if ((data.sparks || []).length) {
        applyPayload(data);
        if (chip) {
          chip.textContent = data.tiktokError || "Saved";
          chip.classList.toggle("is-error", !!data.tiktokError);
        }
      } else if (!rows.length) {
        renderTable();
      }
    } catch (err) {
      if (!rows.length && body) {
        body.innerHTML = `<tr><td colspan="15" class="hint">${escapeHtml(err.message || "Could not load sparks")}</td></tr>`;
      } else if (chip) {
        chip.textContent = "Saved";
      }
    } finally {
      loading = false;
    }
  }

  function setRefreshSpin(on) {
    const btn = $("sparkRefreshBtn");
    if (!btn) return;
    if (on) {
      if (!btn.dataset.origLabel) btn.dataset.origLabel = btn.textContent || "Refresh";
      btn.innerHTML = `<span class="spin" aria-hidden="true"></span>`;
      btn.disabled = true;
    } else if (btn.dataset.origLabel) {
      btn.textContent = btn.dataset.origLabel;
      delete btn.dataset.origLabel;
      btn.disabled = false;
    }
  }

  async function refreshFromTikTok(fromButton) {
    if (fromButton) buttonWaiting = true;
    setRefreshSpin(buttonWaiting);
    if (loading) return;
    loading = true;
    try {
      const data = await window.api("/api/provision/sparks/refresh", {
        method: "POST",
        body: "{}",
      });
      if ((data.sparks || []).length) applyPayload(data);
      const chip = $("sparkSyncChip");
      if (chip) {
        chip.textContent = data.tiktokError || "Updated";
        chip.classList.toggle("is-error", !!data.tiktokError);
      }
    } catch (err) {
      const chip = $("sparkSyncChip");
      if (chip) {
        chip.textContent = err && err.message ? err.message : "TikTok refresh failed";
        chip.classList.add("is-error");
      }
    } finally {
      loading = false;
      buttonWaiting = false;
      setRefreshSpin(false);
    }
  }

  const search = $("sparkSearch");
  if (search) search.addEventListener("input", () => renderTable());
  const refresh = $("sparkRefreshBtn");
  if (refresh) refresh.addEventListener("click", () => refreshFromTikTok(true).catch(() => {}));
  const copyBtn = $("sparkPostbackBtn");
  if (copyBtn) {
    copyBtn.addEventListener("click", async () => {
      if (!postbackUrl) return;
      try {
        await navigator.clipboard.writeText(postbackUrl);
        copyBtn.textContent = "Copied";
        setTimeout(() => { copyBtn.textContent = "Copy postback URL"; }, 1200);
      } catch (err) {
        window.prompt("Postback URL", postbackUrl);
      }
    });
  }
  showCached();
  window.loadSparksPage = (opts) => loadSparksPage(opts || { sync: false });
})();
