// Chat UI: talks only to the FastAPI endpoints (/chat, /scores, /health).
const $ = (s) => document.querySelector(s);
const log = $("#log"), form = $("#composer"), input = $("#msg"), sendBtn = $("#send");

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode: fine */ } },
};
const newId = () => "s-" + Math.random().toString(36).slice(2, 10);
let sessionId = store.get("wra-session") || newId();
store.set("wra-session", sessionId);

// ---------- health ----------
fetch("/health").then((r) => r.json()).then((h) => {
  const el = $("#status");
  el.textContent = `${h.hubs_with_weather}/${h.hubs_configured} hubs cached · ${h.model}`;
  el.className = "status " + (h.status === "ok" ? "ok" : "bad");
}).catch(() => { $("#status").textContent = "API unreachable"; $("#status").className = "status bad"; });

// ---------- network board ----------
async function loadBoard(hazard) {
  const r = await fetch(`/scores?hazard=${hazard}`);
  const d = await r.json();
  $("#board").innerHTML = d.ranking.map((x) => `
    <li data-city="${esc(x.city)}" title="Ask why ${esc(x.city)} scores ${x.score}">
      <span class="rk">${x.rank}</span>
      <span class="nm">${esc(x.city)} <small>${esc(x.hub_id)}${x.close_call_with_next ? ' · <span class="cc">≈</span> next' : ""}</small>
        <span class="bar"><i class="${x.level}" style="width:${x.score}%"></i></span></span>
      <span class="sc">${x.score.toFixed(1)}</span>
    </li>`).join("");
}
$("#tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  loadBoard(b.dataset.h);
});
$("#board").addEventListener("click", (e) => {
  const li = e.target.closest("li"); if (!li) return;
  const hz = document.querySelector("#tabs .on").dataset.h;
  ask(`Why does ${li.dataset.city} score where it does on ${hz === "composite" ? "overall weather disruption risk" : hz + " risk"}?`);
});
loadBoard("composite");

// ---------- chat ----------
function addUser(text) {
  const n = $("#tpl-user").content.cloneNode(true);
  n.querySelector("p").textContent = text;
  log.appendChild(n);
}

function renderAgent(a) {
  const m = a.meta || {};
  const el = document.createElement("div");
  el.className = "msg agent" + (m.error ? " err" : a.in_scope ? "" : " oos");
  const hubs = (a.hubs || []).length ? `
    <div class="table-wrap"><table class="hubs"><thead><tr><th>Hub</th><th>${esc(a.focus_hazard)} score</th><th></th><th>Level</th><th>Network rank</th></tr></thead><tbody>
    ${a.hubs.map((h) => `<tr><td>${esc(h.city)} <span class="num" style="color:var(--ink-3)">${esc(h.hub_id)}</span></td>
      <td class="num">${h.score.toFixed(1)}</td><td><div class="bar"><i class="${h.level}" style="width:${h.score}%"></i></div></td>
      <td><span class="lvl ${h.level}">${h.level}</span></td><td class="num">${h.rank} / ${h.of}</td></tr>`).join("")}
    </tbody></table></div>` : "";
  const list = (xs) => (xs || []).map((x) => `<li>${esc(x)}</li>`).join("");
  const tools = (m.tools_called || []).map((t) => `<li><code>${esc(t.name)}(${esc(JSON.stringify(t.args))})</code>${t.ok ? "" : ` <span class="warn">${esc(t.error)}</span>`}</li>`).join("");
  const warns = (m.grounding_warnings || []).map((w) => `<li class="warn">${esc(w)}</li>`).join("");
  el.innerHTML = `
    ${a.in_scope ? `<span class="tag unc-${a.uncertainty.level}">uncertainty: ${a.uncertainty.level}</span>` : `<span class="tag">out of scope</span>`}
    <p class="answer">${esc(a.answer)}</p>
    ${hubs}
    ${a.in_scope && !m.error ? `<details><summary>Reasoning, data &amp; assumptions</summary>
      <h4>Reasoning</h4><ol>${list(a.reasoning_steps)}</ol>
      <h4>Data used</h4><ul>${(a.data_used || []).map((d) => `<li>${esc(d.source)} — ${esc(d.period)}</li>`).join("")}</ul>
      <h4>Assumptions</h4><ul>${list(a.assumptions)}</ul>
      <h4>Uncertainty</h4><ul><li>${esc(a.uncertainty.note)}</li></ul>
      <h4>Tools called</h4><ul>${tools || "<li>none</li>"}</ul>
      ${warns ? `<h4>Grounding warnings</h4><ul>${warns}</ul>` : ""}
      <h4>Run</h4><ul><li class="num">${esc(m.model)} · ${m.steps} steps · ${m.validation_retries} schema retries · ${(m.latency_ms / 1000).toFixed(1)} s</li></ul>
    </details>` : ""}
    ${(a.follow_up_suggestions || []).length ? `<div class="chips">${a.follow_up_suggestions.map((s) => `<button class="chip">${esc(s)}</button>`).join("")}</div>` : ""}`;
  log.appendChild(el);
  el.scrollIntoView({ behavior: "smooth", block: "start" });
  if ($("#speak").checked && "speechSynthesis" in window) {
    speechSynthesis.cancel();
    speechSynthesis.speak(new SpeechSynthesisUtterance(a.answer));
  }
}

async function ask(text) {
  text = text.trim(); if (!text) return;
  log.querySelector(".intro")?.remove();
  addUser(text);
  input.value = ""; sendBtn.disabled = true;
  const wait = document.createElement("div");
  wait.className = "thinking"; wait.textContent = "checking the data";
  log.appendChild(wait); wait.scrollIntoView({ behavior: "smooth" });
  try {
    const send = () => fetch("/chat", { method: "POST",
      headers: { "Content-Type": "application/json", "X-Access-Code": store.get("wra-code") || "" },
      body: JSON.stringify({ session_id: sessionId, message: text }) });
    let r = await send();
    if (r.status === 401) {  // public deploy: ask for the access code once, then retry
      const code = window.prompt("This demo is protected. Enter the access code:");
      if (code) { store.set("wra-code", code.trim()); r = await send(); }
    }
    if (r.status === 429) throw new Error((await r.json()).detail);
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    renderAgent(await r.json());
  } catch (e) {
    renderAgent({ in_scope: true, answer: `The API request failed (${e.message}).`, hubs: [], meta: { error: e.message },
      uncertainty: { level: "high", note: "" }, follow_up_suggestions: [] });
  } finally {
    wait.remove(); sendBtn.disabled = false; input.focus();
  }
}

form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
log.addEventListener("click", (e) => { const c = e.target.closest(".chip"); if (c) ask(c.textContent); });
$("#newSession").addEventListener("click", () => {
  sessionId = newId(); store.set("wra-session", sessionId);
  log.innerHTML = '<div class="intro"><p class="kicker">New session started. Earlier questions are forgotten.</p></div>';
});

// ---------- voice (bonus): browser speech-to-text ----------
const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
const mic = $("#mic");
if (!SR) { mic.disabled = true; mic.title = "Voice input is not supported in this browser (try Chrome)"; }
else {
  const rec = new SR(); rec.lang = "en-US"; rec.interimResults = true;
  let live = false;
  rec.onresult = (e) => {
    const t = Array.from(e.results).map((r) => r[0].transcript).join("");
    input.value = t;
    if (e.results[e.results.length - 1].isFinal) ask(t);
  };
  rec.onend = () => { live = false; mic.classList.remove("live"); };
  mic.addEventListener("click", () => {
    if (live) { rec.stop(); return; }
    live = true; mic.classList.add("live"); rec.start();
  });
}
