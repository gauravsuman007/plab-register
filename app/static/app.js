function $(sel) { return document.querySelector(sel); }
function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(method, path, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  if (!r.ok) {
    let detail = r.statusText;
    try { detail = (await r.json()).detail || detail; } catch (e) { /* ignore */ }
    throw new Error(detail);
  }
  return r.json();
}

// -------------------------------------------------------------------------- //
// Tailscale
// -------------------------------------------------------------------------- //
function renderTailscale(st) {
  const loggedOutEl = $("#tailscaleLoggedOut");
  const loggedInEl = $("#tailscaleLoggedIn");
  const select = $("#tsExitNodeSelect");
  const badge = $("#egressBadge");

  if (!st.configured) {
    loggedOutEl.style.display = "none";
    loggedInEl.style.display = "none";
    badge.textContent = "Direct";
    badge.classList.remove("good");
    return;
  }
  loggedOutEl.style.display = st.logged_in ? "none" : "block";
  loggedInEl.style.display = st.logged_in ? "block" : "none";

  if (st.logged_in) {
    const nodes = st.exit_nodes || [];
    const key = nodes.map(n => n.id).join(",");
    if (select.dataset.nodesKey !== key) {
      select.innerHTML = `<option value="">Direct (no exit node)</option>` +
        nodes.map(n => `<option value="${escapeHtml(n.id)}">${escapeHtml(n.name || n.hostname)}${n.online ? "" : " (offline)"}</option>`).join("");
      select.dataset.nodesKey = key;
    }
    select.value = st.exit_node_id || "";
  }

  if (st.exit_node_id) {
    badge.textContent = `Exit: ${st.exit_node_name || st.exit_node_id}`;
    badge.classList.add("good");
  } else {
    badge.textContent = "Direct";
    badge.classList.remove("good");
  }
}

async function refreshTailscale() {
  try { renderTailscale(await api("GET", "/api/tailscale/status")); } catch (e) { /* ignore */ }
}

function initTailscale() {
  $("#tsLoginBtn").addEventListener("click", async () => {
    const btn = $("#tsLoginBtn");
    const urlEl = $("#tsLoginUrl");
    const msgEl = $("#tsLoginMsg");
    btn.disabled = true;
    msgEl.textContent = "Starting login...";
    try {
      const res = await api("POST", "/api/tailscale/login", {});
      if (res.already_logged_in) {
        msgEl.textContent = "Already logged in.";
        await refreshTailscale();
      } else {
        urlEl.style.display = "inline";
        urlEl.href = res.url;
        urlEl.textContent = res.url;
        msgEl.textContent = "Open this link to approve - updates automatically.";
        let tries = 0;
        const iv = setInterval(async () => {
          tries++;
          const st = await api("GET", "/api/tailscale/status").catch(() => null);
          if (st) renderTailscale(st);
          if ((st && st.logged_in) || tries > 40) {
            clearInterval(iv);
            if (st && st.logged_in) { msgEl.textContent = "Logged in."; urlEl.style.display = "none"; }
          }
        }, 3000);
      }
    } catch (e) {
      msgEl.textContent = `Error: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  $("#tsLogoutBtn").addEventListener("click", async () => {
    await api("POST", "/api/tailscale/logout", {});
    await refreshTailscale();
  });

  $("#tsExitNodeSelect").addEventListener("change", async e => {
    const st = await api("POST", "/api/tailscale/exit-node", { node_id: e.target.value || null });
    renderTailscale(st);
  });

  refreshTailscale();
  setInterval(refreshTailscale, 20000);
}

// -------------------------------------------------------------------------- //
// Settings
// -------------------------------------------------------------------------- //
async function loadSettings() {
  const s = await api("GET", "/api/settings");
  const form = $("#settingsForm");
  for (const [k, v] of Object.entries(s)) {
    const el = form.querySelector(`[name="${k}"]`);
    if (el && typeof v !== "object") el.value = v;
  }
}

function initSettings() {
  $("#saveSettingsBtn").addEventListener("click", async () => {
    const form = $("#settingsForm");
    const body = {};
    form.querySelectorAll("[name]").forEach(el => {
      if (el.type === "password" && !el.value) return; // "(unchanged)"
      body[el.name] = el.value;
    });
    const msg = $("#settingsMsg");
    try {
      await api("POST", "/api/settings", body);
      msg.textContent = "Saved.";
      msg.className = "form-msg ok";
    } catch (e) {
      msg.textContent = `Error: ${e.message}`;
      msg.className = "form-msg err";
    }
  });
  loadSettings();
}

// -------------------------------------------------------------------------- //
// Auto-register
// -------------------------------------------------------------------------- //
function initAutoRegister() {
  $("#autoRegisterBtn").addEventListener("click", async () => {
    const btn = $("#autoRegisterBtn");
    const status = $("#autoRegisterStatus");
    btn.disabled = true;
    status.textContent = "Working... this can take a few minutes (browser automation + captcha solving).";
    try {
      const r = await api("POST", "/api/register/auto", {});
      status.innerHTML = `Registered <b>${escapeHtml(r.username)}</b> / ${escapeHtml(r.email)}` +
        (r.activated ? " - activated." : " - activation pending, check the inbox below.") +
        ` Imported into pornolab-seeder as account ${escapeHtml(r.account_id)}.`;
      loadEvents();
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });
}

// -------------------------------------------------------------------------- //
// Emails registry
// -------------------------------------------------------------------------- //
async function loadEmails() {
  const rows = await api("GET", "/api/emails");
  $("#emailsBody").innerHTML = rows.map(r => `
    <tr data-id="${r.id}">
      <td>${r.id}</td>
      <td>${escapeHtml(r.provider)}</td>
      <td>${escapeHtml(r.address)}</td>
      <td>${new Date(r.created_at * 1000).toLocaleString()}</td>
      <td>
        <button class="btn small" data-act="inbox">Check inbox</button>
        <button class="btn small primary" data-act="register">Register on PornoLab</button>
        <button class="btn small danger" data-act="delete">Delete</button>
      </td>
    </tr>
  `).join("") || `<tr><td colspan="5" class="muted">No emails yet.</td></tr>`;
}

function initEmails() {
  $("#newEmailBtn").addEventListener("click", async () => {
    const provider = $("#newEmailProvider").value;
    const btn = $("#newEmailBtn");
    btn.disabled = true;
    try {
      await api("POST", "/api/emails/new", { provider });
      await loadEmails();
    } catch (e) {
      alert(`Error: ${e.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  $("#emailsBody").addEventListener("click", async e => {
    const btn = e.target.closest("button[data-act]");
    if (!btn) return;
    const tr = btn.closest("tr");
    const id = tr.dataset.id;
    const act = btn.dataset.act;
    btn.disabled = true;
    try {
      if (act === "delete") {
        await api("DELETE", `/api/emails/${id}`);
        await loadEmails();
      } else if (act === "inbox") {
        const r = await api("GET", `/api/emails/${id}/inbox`);
        alert((r.messages || []).map(m => `${m.from}: ${m.subject}`).join("\n") || "Inbox is empty.");
      } else if (act === "register") {
        const r = await api("POST", `/api/emails/${id}/register-pornolab`);
        alert(`Registered ${r.username} (account ${r.account_id}).` + (r.activated ? " Activated." : " Activation pending."));
        await loadEmails();
        loadEvents();
      }
    } catch (e2) {
      alert(`Error: ${e2.message}`);
    } finally {
      btn.disabled = false;
    }
  });

  loadEmails();
}

// -------------------------------------------------------------------------- //
// atomicmail.io interactive hCaptcha relay
// -------------------------------------------------------------------------- //
let amSession = null;
let amClicks = [];

function initAtomicmail() {
  $("#amStartBtn").addEventListener("click", async () => {
    const status = $("#amStatus");
    status.textContent = "Starting...";
    try {
      const r = await api("POST", "/api/atomicmail/signup/start", {});
      if (r.solved_without_challenge) {
        status.textContent = `Done: ${r.email} (no challenge needed).`;
        return;
      }
      amSession = r.session_id;
      amClicks = [];
      $("#amChallenge").style.display = "block";
      $("#amImage").src = `data:image/png;base64,${r.image}`;
      $("#amPrompt").textContent = r.prompt || "Click the matching cells, then Verify.";
      status.textContent = `${r.email} / ${r.password} - solve the challenge below.`;
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  $("#amImage").addEventListener("click", async e => {
    if (!amSession) return;
    const rect = e.target.getBoundingClientRect();
    const x = (e.clientX - rect.left) / rect.width;
    const y = (e.clientY - rect.top) / rect.height;
    try { await api("POST", "/api/atomicmail/signup/click", { session_id: amSession, x, y }); }
    catch (e2) { $("#amStatus").textContent = `Error: ${e2.message}`; }
  });

  $("#amVerifyBtn").addEventListener("click", async () => {
    if (!amSession) return;
    const status = $("#amStatus");
    try {
      const r = await api("POST", "/api/atomicmail/signup/verify", { session_id: amSession });
      if (r.solved) {
        status.textContent = `Solved: ${r.email} / ${r.password}`;
        $("#amChallenge").style.display = "none";
        amSession = null;
      } else if (r.image) {
        $("#amImage").src = `data:image/png;base64,${r.image}`;
        $("#amPrompt").textContent = r.prompt || "";
        status.textContent = "Another round - keep clicking.";
      } else {
        status.textContent = r.error || "Not solved yet.";
      }
    } catch (e) {
      status.textContent = `Error: ${e.message}`;
    }
  });

  $("#amCancelBtn").addEventListener("click", async () => {
    if (amSession) await api("POST", "/api/atomicmail/signup/cancel", { session_id: amSession }).catch(() => {});
    amSession = null;
    $("#amChallenge").style.display = "none";
    $("#amStatus").textContent = "Cancelled.";
  });
}

// -------------------------------------------------------------------------- //
// Manual registration fallback
// -------------------------------------------------------------------------- //
function initManualRegister() {
  $("#fetchFormBtn").addEventListener("click", async () => {
    try {
      const r = await api("GET", "/api/register/form");
      $("#manualForm").style.display = "grid";
      $("#manualActions").style.display = "flex";
      $("#captchaImg").src = r.captcha || "";
      $("#mCaptcha").value = r.captcha_text || "";
    } catch (e) {
      alert(`Error: ${e.message}`);
    }
  });

  $("#submitRegisterBtn").addEventListener("click", async () => {
    const msg = $("#manualMsg");
    try {
      const r = await api("POST", "/api/register", {
        username: $("#mUsername").value,
        password: $("#mPassword").value,
        email: $("#mEmail").value,
        captcha: $("#mCaptcha").value,
      });
      if (r.ok) {
        msg.textContent = "Registered.";
        msg.className = "form-msg ok";
      } else {
        msg.textContent = r.error || "Failed.";
        msg.className = "form-msg err";
      }
    } catch (e) {
      msg.textContent = `Error: ${e.message}`;
      msg.className = "form-msg err";
    }
  });
}

// -------------------------------------------------------------------------- //
// Events
// -------------------------------------------------------------------------- //
async function loadEvents() {
  const rows = await api("GET", "/api/events");
  $("#eventsBody").innerHTML = rows.map(r => `
    <tr><td class="small muted">${new Date(r.at * 1000).toLocaleString()}</td>
        <td class="small">${escapeHtml(r.level)}</td>
        <td class="small">${escapeHtml(r.message)}</td></tr>
  `).join("") || `<tr><td class="muted">No events yet.</td></tr>`;
}

document.addEventListener("DOMContentLoaded", () => {
  initTailscale();
  initSettings();
  initAutoRegister();
  initEmails();
  initAtomicmail();
  initManualRegister();
  loadEvents();
  setInterval(loadEvents, 30000);
});
