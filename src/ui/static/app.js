/* ShiftVoice dashboard: renders /api/state, listens on /api/events, speaks call audio. */
const $ = (sel) => document.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let S = null;                 // latest snapshot
let clockOffset = 0;          // sim clock minus browser clock, ms
let activeInbound = null;     // call id of the inbound call on the phone widget
let thinking = false;
let voiceOn = true;
const openDetails = new Set();
const fromMic = new Set();    // lines the presenter spoke aloud; don't read them back
let selectedShift = null;

async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || res.statusText);
  }
  return res.json();
}

/* ---------- formatting ---------- */
const fmtTime = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
const fmtDay = (iso) => new Date(iso + "T12:00:00").toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" });
const fmtDur = (s) => s == null ? "–" : s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
const money = (n) => n == null ? "–" : "$" + Math.round(n).toLocaleString();
const STATUS_WORDS = {
  queued: "Queued", calling: "Calling…", declined: "Declined", accepted: "Accepted", no_answer: "No answer",
  skipped: "Skipped", not_needed: "Not needed", open: "Open", filling: "Filling…", filled: "Filled", escalated: "Escalated",
  approved: "Approved", logged: "Logged", pending_supervisor: "Needs supervisor", alternatives_offered: "Alternatives offered",
  declined_sup: "Declined", withdrawn: "Withdrawn",
};

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.add("show");
  clearTimeout(toast._h);
  toast._h = setTimeout(() => t.classList.remove("show"), 4200);
}

/* ---------- speech out ---------- */
let voices = [];
function loadVoices() { voices = window.speechSynthesis ? speechSynthesis.getVoices().filter((v) => v.lang.startsWith("en")) : []; }
if (window.speechSynthesis) { loadVoices(); speechSynthesis.onvoiceschanged = loadVoices; }
function pickVoice(prefs, avoid) {
  for (const p of prefs) {
    const v = voices.find((x) => x.name.includes(p) && x !== avoid);
    if (v) return v;
  }
  return voices.find((x) => x !== avoid) || null;
}
function speak(line) {
  if (!voiceOn || !window.speechSynthesis) return;
  const u = new SpeechSynthesisUtterance(line.text);
  const agentVoice = pickVoice(["Aria", "Jenny", "Samantha", "Google US English", "Zira"]);
  if (line.speaker === "agent") {
    u.voice = agentVoice; u.rate = 1.05;
  } else {
    let h = 0; for (const c of line.name || "") h = (h * 31 + c.charCodeAt(0)) >>> 0;
    u.voice = pickVoice(["Google UK English Female", "Sonia", "Libby", "Karen", "Moira", "Guy", "Daniel"], agentVoice);
    u.pitch = 0.9 + (h % 5) * 0.08; u.rate = 1.0;
  }
  speechSynthesis.speak(u);
}

/* ---------- speech in (Chrome Web Speech API) ---------- */
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognizer = null;
let micTarget = null; // { input, send }
function listen(input, send, button) {
  if (!Recognition) { toast("Voice input needs Chrome or Edge. You can type instead."); return; }
  if (recognizer) { recognizer.stop(); return; }
  if (window.speechSynthesis) speechSynthesis.cancel();
  recognizer = new Recognition();
  recognizer.lang = "en-US"; recognizer.interimResults = true; recognizer.continuous = false;
  micTarget = { input, send };
  button.classList.add("listening");
  let finalText = "";
  recognizer.onresult = (e) => {
    let interim = "";
    for (let i = e.resultIndex; i < e.results.length; i++) {
      if (e.results[i].isFinal) finalText += e.results[i][0].transcript; else interim += e.results[i][0].transcript;
    }
    input.value = finalText || interim;
  };
  recognizer.onerror = (e) => { if (e.error !== "no-speech" && e.error !== "aborted") toast("Mic error: " + e.error); };
  recognizer.onend = () => {
    button.classList.remove("listening");
    recognizer = null;
    const text = (finalText || input.value).trim();
    if (text) { fromMic.add(text); micTarget.send(text); }
  };
  recognizer.start();
}

/* ---------- actions ---------- */
async function startCall(staffId) {
  if (window.speechSynthesis) speechSynthesis.cancel();
  const { call_id } = await api("/api/calls/inbound", { body: { staff_id: Number(staffId) } });
  activeInbound = call_id;
  thinking = false;
  await refresh();
  $("#say-input").focus();
}

async function sayInbound(text) {
  if (!activeInbound || !text.trim()) return;
  $("#say-input").value = "";
  thinking = true;
  renderInbound();
  try {
    await api(`/api/calls/${activeInbound}/say`, { body: { text } });
  } catch (e) {
    toast(e.message);
  } finally {
    thinking = false;
    refresh();
  }
}

async function answerOutbound(callId, text) {
  if (!text.trim()) return;
  try { await api(`/api/calls/${callId}/say`, { body: { text } }); } catch (e) { toast(e.message); }
}

/* ---------- rendering ---------- */
function renderHeader() {
  $("#hospital").textContent = `${S.hospital} · synthetic data`;
  const pill = $("#llm-pill");
  if (S.llm.online) {
    pill.className = "pill on";
    pill.textContent = `LLM: ${S.llm.model} via Sciforium`;
    pill.title = S.llm.last_error ? `Last error (fell back to rules): ${S.llm.last_error}` : "Online";
    if (S.llm.last_error) pill.className = "pill off";
  } else {
    pill.className = "pill off";
    pill.textContent = "LLM offline · rules fallback";
    pill.title = "Set SCIFORIUM_API_KEY and SCIFORIUM_MODEL in .env";
  }
  $("#candidate-mode").value = S.settings.candidate_mode;
  $("#queue-count").textContent = S.supervisor_queue.length || "";
}

function renderMetrics() {
  const m = S.metrics;
  const items = [
    [m.callouts_today, "Call-outs today"],
    [`${m.gaps_filled}/${m.gaps_filled + m.gaps_open}`, "Gaps filled"],
    [fmtDur(m.avg_time_to_fill_s), "Avg time to fill"],
    [m.outbound_calls, "Calls placed by agent"],
    [`${m.charge_nurse_minutes_saved} min`, "Charge-nurse phone time saved"],
    [money(m.agency_spend_avoided), "Agency spend avoided"],
  ];
  $("#metrics").innerHTML = items.map(([v, l]) => `<div class="metric"><div class="v">${esc(v)}</div><div class="l">${esc(l)}</div></div>`).join("");
}

function rosterLine(r) {
  const charge = r.role === "charge" ? `<span class="badge charge">Charge</span>` : "";
  if (r.status === "called_out") return `<li><span class="out">${esc(r.name)}</span><span class="badge out">${esc(r.note || "called out")}</span></li>`;
  if (r.status === "leave") return `<li><span class="out">${esc(r.name)}</span><span class="badge">${esc(r.note || "leave")}</span></li>`;
  if (r.source === "backfill") return `<li><span class="new">${esc(r.name)} ${charge}</span><span class="badge bf">Backfill · tier ${r.tier}</span></li>`;
  const pool = r.pool !== "core" ? ` <span class="muted small">${esc(r.pool.replace("_", " "))}</span>` : "";
  return `<li><span>${esc(r.name)}${pool}</span>${charge}</li>`;
}

function shiftCard(c) {
  const segs = [];
  const active = c.roster.filter((r) => r.status === "scheduled");
  active.forEach((r) => segs.push(r.source === "backfill" ? "backfill" : "filled"));
  for (let i = active.length; i < c.required; i++) segs.push("missing");
  const statusWord = { ok: "Covered", at_min: "At minimum", short: "Short" }[c.status];
  const hours = c.kind === "day" ? "7a–7p" : "7p–7a";
  return `<div class="card shift-card ${c.status}">
    <div class="shift-top">
      <div><div class="shift-name">${esc(c.unit)} · ${c.kind === "day" ? "Day" : "Night"} <span class="muted">${hours}</span></div>
      <div class="shift-meta">Census ${c.census} · ratio ${esc(c.ratio)} → ${c.required} RNs required</div></div>
      <div style="text-align:right"><div class="count-big">${c.staffed}<small>/${c.required}</small></div><span class="status-tag ${c.status}">${statusWord}</span></div>
    </div>
    <div class="bar">${segs.map((s) => `<i class="${s}"></i>`).join("")}</div>
    <div class="checks">
      <span class="chk ${c.has_charge ? "" : "bad"}">${c.has_charge ? "✓" : "✗"} Charge RN</span>
      ${c.unit_id === "icu" ? `<span class="chk ${c.cert_ok ? "" : "bad"}">${c.cert_ok ? "✓" : "✗"} ICU-certified</span>` : ""}
      <span class="chk ${c.margin < 0 ? "bad" : ""}">${c.margin >= 0 ? "✓" : "✗"} Ratio</span>
    </div>
    <ul class="roster">${c.roster.map(rosterLine).join("")}</ul>
  </div>`;
}

function renderCoverage() { $("#coverage").innerHTML = S.today.map(shiftCard).join(""); }

function transcriptHTML(lines) {
  return lines.map((l) => `<div class="line ${l.speaker}"><div class="who">${esc(l.name)} · ${fmtTime(l.ts)}</div>${esc(l.text)}</div>`).join("");
}

function renderInbound() {
  // The call this browser started, else the latest inbound call (e.g. one that came in by phone).
  const call = !S ? null : activeInbound ? S.calls.find((c) => c.id === activeInbound)
    : S.calls.find((c) => c.direction === "inbound");
  const box = $("#inbound-transcript");
  const live = call && call.status === "active";
  if (call) {
    box.innerHTML = transcriptHTML(call.transcript)
      + (thinking ? `<div class="thinking">ShiftVoice is thinking</div>` : "")
      + (call.status === "ended" ? `<div class="line system">Call ended</div>` : "");
    box.scrollTop = box.scrollHeight;
  }
  $("#say-input").disabled = !live;
  $("#send-btn").disabled = !live;
  $("#mic-btn").disabled = !live;
  $("#call-btn").classList.toggle("hidden", !!live);
  $("#hangup-btn").classList.toggle("hidden", !live);
  $("#caller").disabled = !!live;
}

function renderCallerSelect() {
  const sel = $("#caller");
  if (sel.options.length) return;
  const label = (p) => `${p.name} · ${p.pool === "core" ? (p.home_unit === "icu" ? "ICU" : "Med-Surg") : p.pool.replace("_", " ")}`;
  sel.innerHTML = S.staff.map((p) => `<option value="${p.id}">${esc(label(p))}</option>`).join("");
  renderChips();
}

function renderChips() {
  if (!S) return;
  const caller = Number($("#caller").value);
  const scripts = S.scripts.filter((s) => s.caller === caller);
  $("#chips").innerHTML = scripts.map((s, i) => `<button class="chip" data-i="${S.scripts.indexOf(s)}" title="${esc(s.text)}">${esc(s.label)}</button>`).join("")
    || `<span class="muted small">Try the scripted callers: Maria Chen (sick call) or Devon Wright (time off).</span>`;
}

function keepInputs(container, fn) {
  const saved = {};
  let focused = null;
  container.querySelectorAll("input[data-keep]").forEach((el) => {
    saved[el.dataset.keep] = el.value;
    if (document.activeElement === el) focused = el.dataset.keep;
  });
  fn();
  container.querySelectorAll("input[data-keep]").forEach((el) => {
    if (saved[el.dataset.keep] != null) el.value = saved[el.dataset.keep];
    if (focused === el.dataset.keep) el.focus();
  });
}

function renderOutbound() {
  const calls = S.calls.filter((c) => c.direction === "outbound").slice(0, 4);
  const el = $("#outbound");
  keepInputs(el, () => {
    if (!calls.length) { el.innerHTML = `<div class="card empty">When a shift opens up, the agent calls candidates here, one at a time.</div>`; return; }
    el.innerHTML = calls.map((c, i) => {
      const live = c.status === "active";
      const answer = live && S.settings.candidate_mode === "live" ? `
        <div class="composer">
          <button class="mic" data-mic="${c.id}" title="Answer by voice"><svg viewBox="0 0 24 24" width="20" height="20"><path d="M12 15a3 3 0 0 0 3-3V6a3 3 0 1 0-6 0v6a3 3 0 0 0 3 3Zm5-3a5 5 0 0 1-10 0H5a7 7 0 0 0 6 6.9V21h2v-2.1A7 7 0 0 0 19 12h-2Z" fill="currentColor"/></svg></button>
          <input data-keep="ans-${c.id}" data-answer="${c.id}" placeholder="Answer as ${esc(c.name)}…">
          <button class="btn" data-send="${c.id}">Send</button>
        </div>` : "";
      const collapsed = !live && i > 0;
      return `<div class="card call-card">
        <div class="call-head">
          <div><b>${live ? `<span class="live-dot"></span>` : ""}→ ${esc(c.name)}</b> <span class="muted small">${esc(c.purpose)} · ${fmtTime(c.started_at)}</span></div>
          <span class="st ${live ? "calling" : ""}">${live ? "On the call" : "Ended"}</span>
        </div>
        ${collapsed ? `<details data-key="call-${c.id}" ${openDetails.has("call-" + c.id) ? "open" : ""}><summary>Transcript (${c.transcript.length} lines)</summary><div class="transcript">${transcriptHTML(c.transcript)}</div></details>`
                    : `<div class="transcript">${transcriptHTML(c.transcript)}</div>`}
        ${answer}
      </div>`;
    }).join("");
  });
  el.querySelectorAll(".call-card > .transcript").forEach((t) => (t.scrollTop = t.scrollHeight));
}

function gapCard(g) {
  const pill = g.status === "filled" ? `<span class="st filled">Filled in ${fmtDur(g.time_to_fill_s)}</span>` : `<span class="st ${g.status}">${STATUS_WORDS[g.status] || g.status}</span>`;
  const cands = g.candidates.map((c) => `
    <li class="cand ${c.status}">
      <span class="rank">${c.rank}</span>
      <div><div class="nm"><span class="tier t${c.tier}">T${c.tier}</span>${esc(c.name)}</div>
        <div class="why">${c.reasons.map(esc).join(" · ")}</div></div>
      <div><span class="st ${c.status}">${STATUS_WORDS[c.status] || c.status}</span><div class="cost">${money(c.est_cost)}</div></div>
    </li>`).join("");
  const byReason = {};
  g.excluded.forEach((e) => { const r = e.reasons[0]; (byReason[r] = byReason[r] || []).push(e.name); });
  const excluded = g.excluded.length ? `
    <details data-key="ex-${g.id}" ${openDetails.has("ex-" + g.id) ? "open" : ""}>
      <summary>Ruled out by policy (${g.excluded.length})</summary>
      <ul class="excluded">${g.excluded.map((e) => `<li><span>${esc(e.name)}</span><span>${e.reasons.map(esc).join("; ")}</span></li>`).join("")}</ul>
    </details>` : "";
  const start = g.status === "open" ? `<button class="btn sm primary" data-backfill="${g.id}">Start outreach</button>` : "";
  const foot = g.status === "filled"
    ? `<span><b>${esc(g.filled_by)}</b> after ${g.calls_made} call${g.calls_made === 1 ? "" : "s"}</span><span>Cost ${money(g.fill_cost)} vs agency <b>${money(g.agency_cost)}</b></span>`
    : `<span>${g.calls_made} call${g.calls_made === 1 ? "" : "s"} so far</span><span>Agency fallback ${money(g.agency_cost)}</span>`;
  return `<div class="card">
    <div class="gap-head">
      <div><div class="gap-title">${esc(g.label)}</div>
      <div class="gap-sub">${g.vacated ? esc(g.vacated) + " out · " : ""}opened ${fmtTime(g.created_at)}${g.needs_charge ? " · needs charge-certified RN" : ""}</div></div>
      <div>${pill} ${start}</div>
    </div>
    ${g.candidates.length ? `<ul class="cands">${cands}</ul>` : ""}
    ${excluded}
    <div class="gap-foot">${foot}</div>
  </div>`;
}

function renderGaps() {
  $("#gaps").innerHTML = S.gaps.length ? S.gaps.map(gapCard).join("")
    : `<div class="card empty">No open gaps. When someone calls out, the ranked call list appears here.</div>`;
}

function renderNotifications() {
  $("#notifications").innerHTML = S.notifications.length
    ? S.notifications.slice(0, 6).map((n) => `<div class="card note ${n.kind}"><div class="meta">${fmtTime(n.ts)} · to ${esc(n.recipient)}</div>${esc(n.message)}</div>`).join("")
    : `<div class="card empty">Nothing yet. The charge nurse hears from ShiftVoice only when something changes.</div>`;
}

function rulesList(decision) {
  if (!decision.checks || !decision.checks.length) {
    return decision.policy ? `<div class="muted small" style="margin-top:6px">${esc(decision.policy)}</div>` : "";
  }
  const icon = { pass: "✓", warn: "!", fail: "✗" };
  return `<ul class="rules">${decision.checks.map((c) => `<li><span class="r-${c.result}">${icon[c.result]}</span><span>${esc(c.rule)}</span><span class="muted">${esc(c.detail)}</span></li>`).join("")}</ul>`;
}

function requestCard(l, withActions) {
  const d = l.decision || {};
  const dates = l.start_date === l.end_date ? fmtDay(l.start_date) : `${fmtDay(l.start_date)} – ${fmtDay(l.end_date)}`;
  const status = l.status === "declined" ? "declined" : l.status;
  let alts = "";
  if (l.status === "alternatives_offered" && (d.alternatives || []).length) {
    alts = `<div class="alts"><span class="muted">Auto-approvable instead:</span>${d.alternatives.map((a, i) => `<button class="btn sm" data-alt="${l.id}:${i}">${esc(a.label)}</button>`).join("")}</div>`;
    if ((d.swap_partners || []).length) alts += `<div class="alts"><span class="muted">Could swap with:</span> ${d.swap_partners.map((p) => esc(p.name)).join(", ")}</div>`;
  }
  const actions = withActions ? `<div class="actions"><button class="btn sm primary" data-decide="${l.id}:true">Approve</button><button class="btn sm" data-decide="${l.id}:false">Decline</button></div>` : "";
  const who = l.decided_by ? ` · decided by ${esc(l.decided_by)}` : "";
  return `<div class="card">
    <div class="req-head">
      <div><div class="req-title">${esc(l.name)} · ${esc(l.kind)} · ${dates}</div>
      <div class="muted small">${l.category === "unplanned" ? "Unplanned call-out" : "Planned leave"} · via ${esc(l.channel || "–")} · ${fmtTime(l.requested_at)}${who}</div></div>
      <span class="st ${status}">${STATUS_WORDS[status] || status}</span>
    </div>
    ${rulesList(d)}${alts}${actions}
  </div>`;
}

function renderLeave() {
  $("#queue").innerHTML = S.supervisor_queue.length ? S.supervisor_queue.map((l) => requestCard(l, true)).join("")
    : `<div class="card empty">Nothing waiting. Safe requests are approved automatically; only borderline ones land here.</div>`;
  $("#requests").innerHTML = S.leave.map((l) => requestCard(l, false)).join("");
}

function renderAudit() {
  $("#audit").innerHTML = S.events.map((e) => `<tr><td>${fmtTime(e.ts)}</td><td><span class="kind">${esc(e.kind)}</span></td><td>${esc(e.message)}</td></tr>`).join("");
}

async function renderSchedule() {
  const g = await api("/api/schedule?days=28");
  const head = g.dates.map((d) => {
    const dt = new Date(d + "T12:00:00");
    const wk = dt.getDay() === 0 || dt.getDay() === 6;
    return `<th class="${wk ? "wknd" : ""}">${dt.toLocaleDateString([], { weekday: "narrow" })}<br>${dt.getDate()}</th>`;
  }).join("");
  const rows = g.rows.map((r) => `<tr><th class="rowh">${esc(r.unit)} ${r.kind}</th>${r.cells.map((c) =>
    `<td class="cell ${c.status} ${c.blackout ? "blk" : ""} ${selectedShift === c.shift_id ? "sel" : ""}" data-shift="${c.shift_id}"
      title="${fmtDay(c.date)}: ${c.staffed}/${c.required} RNs, census ${c.census}${c.blackout ? " · " + c.blackout : ""}">${c.staffed}/${c.required}</td>`).join("")}</tr>`).join("");
  $("#grid").innerHTML = `<table class="grid"><thead><tr><th></th>${head}</tr></thead><tbody>${rows}</tbody></table>`;
  if (selectedShift) showShift(selectedShift);
}

async function showShift(id) {
  selectedShift = id;
  const c = await api(`/api/shifts/${id}`);
  $("#shift-detail").innerHTML = `<h2>${esc(c.label)}</h2>` + shiftCard({ ...c, roster: c.roster });
  document.querySelectorAll(".cell.sel").forEach((x) => x.classList.remove("sel"));
  const cell = document.querySelector(`.cell[data-shift="${id}"]`);
  if (cell) cell.classList.add("sel");
}

function render() {
  renderHeader();
  renderMetrics();
  renderCallerSelect();
  renderCoverage();
  renderInbound();
  renderOutbound();
  renderGaps();
  renderNotifications();
  renderLeave();
  renderAudit();
}

let refreshTimer = null;
async function refresh() {
  const snap = await api("/api/state");
  S = snap;
  clockOffset = new Date(S.now).getTime() - Date.now();
  if (activeInbound && !S.calls.some((c) => c.id === activeInbound)) activeInbound = null;
  render();
  tick();
  if ($("#tab-schedule").classList.contains("active")) renderSchedule();
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 120);
}

/* ---------- live events ---------- */
function connect() {
  const es = new EventSource("/api/events");
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    const d = ev.data || {};
    if (ev.kind === "call.line") {
      if (d.speaker === "agent" && d.call_id === activeInbound) thinking = false;
      const skip = d.speaker === "staff" && fromMic.has(d.text);
      if (skip) fromMic.delete(d.text); else speak(d);
    }
    if (ev.kind === "gap.opened") toast("Coverage gap: " + d.message);
    if (ev.kind === "gap.filled") toast("Filled: " + d.message);
    if (ev.kind === "leave.decided") toast(d.message);
    if (ev.kind === "demo.reset") { activeInbound = null; if (window.speechSynthesis) speechSynthesis.cancel(); }
    scheduleRefresh();
  };
  es.onerror = () => { /* EventSource reconnects on its own */ };
}

/* ---------- wiring ---------- */
$("#call-btn").onclick = () => startCall($("#caller").value).catch((e) => toast(e.message));
$("#hangup-btn").onclick = async () => { if (activeInbound) { await api(`/api/calls/${activeInbound}/hangup`, { method: "POST" }); refresh(); } };
$("#send-btn").onclick = () => sayInbound($("#say-input").value);
$("#say-input").onkeydown = (e) => { if (e.key === "Enter") sayInbound(e.target.value); };
$("#mic-btn").onclick = (e) => listen($("#say-input"), sayInbound, e.currentTarget);
$("#caller").onchange = renderChips;
$("#chips").onclick = async (e) => {
  const b = e.target.closest(".chip");
  if (!b) return;
  const script = S.scripts[Number(b.dataset.i)];
  const live = S.calls.find((c) => c.id === activeInbound && c.status === "active");
  if (!live) {
    $("#caller").value = script.caller;
    await startCall(script.caller);
    $("#say-input").value = script.text;
  } else {
    sayInbound(script.text);
  }
};
$("#candidate-mode").onchange = (e) => api("/api/settings", { body: { candidate_mode: e.target.value } }).then(refresh);
$("#voice-toggle").onclick = (e) => {
  voiceOn = !voiceOn;
  e.currentTarget.textContent = voiceOn ? "🔊 Voice on" : "🔇 Voice off";
  e.currentTarget.setAttribute("aria-pressed", voiceOn);
  if (!voiceOn && window.speechSynthesis) speechSynthesis.cancel();
};
$("#reset").onclick = async () => {
  if (!confirm("Reset the demo? This reseeds the synthetic hospital.")) return;
  if (window.speechSynthesis) speechSynthesis.cancel();
  activeInbound = null;
  await api("/api/reset", { method: "POST" });
  refresh();
  toast("Demo reset");
};
function showTab(name) {
  const t = document.querySelector(`.tab[data-tab="${name}"]`);
  if (!t) return;
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x === t));
  document.querySelectorAll(".tabpanel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
  history.replaceState(null, "", "#" + name);
  if (name === "schedule" && S) renderSchedule();
}
document.querySelectorAll(".tab").forEach((t) => (t.onclick = () => showTab(t.dataset.tab)));
if (location.hash) showTab(location.hash.slice(1));
document.addEventListener("toggle", (e) => {
  const key = e.target.dataset && e.target.dataset.key;
  if (!key) return;
  if (e.target.open) openDetails.add(key); else openDetails.delete(key);
}, true);
document.addEventListener("click", async (e) => {
  const t = e.target.closest("[data-decide],[data-alt],[data-backfill],[data-send],[data-mic],[data-shift]");
  if (!t) return;
  try {
    if (t.dataset.decide) {
      const [id, ok] = t.dataset.decide.split(":");
      await api(`/api/leave/${id}/decide?approve=${ok}`, { method: "POST" });
    } else if (t.dataset.alt) {
      const [id, i] = t.dataset.alt.split(":");
      await api(`/api/leave/${id}/alternative/${i}`, { method: "POST" });
    } else if (t.dataset.backfill) {
      await api(`/api/gaps/${t.dataset.backfill}/backfill`, { method: "POST" });
    } else if (t.dataset.send) {
      const input = document.querySelector(`[data-answer="${t.dataset.send}"]`);
      const text = input.value; input.value = "";
      await answerOutbound(t.dataset.send, text);
    } else if (t.dataset.mic) {
      const id = t.dataset.mic;
      const input = document.querySelector(`[data-answer="${id}"]`);
      listen(input, (text) => { input.value = ""; answerOutbound(id, text); }, t);
    } else if (t.dataset.shift) {
      showShift(Number(t.dataset.shift));
    }
  } catch (err) { toast(err.message); }
  scheduleRefresh();
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && e.target.dataset && e.target.dataset.answer) {
    const id = e.target.dataset.answer; const text = e.target.value; e.target.value = "";
    answerOutbound(id, text);
  }
});
function tick() {
  if (!S) return;
  const now = new Date(Date.now() + clockOffset);
  $("#clock").textContent = now.toLocaleDateString([], { weekday: "short", month: "short", day: "numeric" }) + " · " +
    now.toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });
}
setInterval(tick, 1000);

refresh().then(connect).catch((e) => toast("Could not load: " + e.message));
