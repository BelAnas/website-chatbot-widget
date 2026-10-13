/*!
 * Website chat widget. One file, no dependencies, no build step.
 *
 *   <script src="https://YOUR-SERVICE/widget.js" data-title="Assistant" data-color="#1f5f4a" defer></script>
 *
 * Options (all optional, as data- attributes on the script tag):
 *   data-api       address of the chat service (default: where this file was loaded from)
 *   data-title     name shown in the header
 *   data-color     brand colour of the button, header and visitor bubbles
 *   data-position  "right" (default) or "left"
 *   data-open      "true" opens the panel on page load
 */
(function () {
  "use strict";
  var script = document.currentScript;
  if (!script || window.__siteChatWidget) return;   // never load twice on one page
  window.__siteChatWidget = true;

  var api = (script.dataset.api || new URL(script.src).origin).replace(/\/$/, "");
  var color = /^#[0-9a-f]{3,8}$/i.test(script.dataset.color || "") ? script.dataset.color : "#1f5f4a";
  var side = script.dataset.position === "left" ? "left" : "right";
  var title = script.dataset.title || "Assistant";
  var KEY = "site-chat:" + api;                       // one saved conversation per chat service

  // ---- saved state: the session id and the visible transcript survive a page change ----
  var state = { session: "", log: [], open: false, leadDone: false };
  try { state = Object.assign(state, JSON.parse(localStorage.getItem(KEY) || "{}")); } catch (e) { /* private mode */ }
  if (!state.session) {
    state.session = (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : "s-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
  }
  function save() {
    state.log = state.log.slice(-40);
    try { localStorage.setItem(KEY, JSON.stringify(state)); } catch (e) { /* storage full or blocked */ }
  }

  // ---- DOM. A shadow root keeps the host page's CSS out and ours in. ----
  var host = document.createElement("div");
  host.id = "site-chat-widget";
  var root = host.attachShadow({ mode: "open" });

  // All text goes in through textContent, never innerHTML: an answer or a page title that
  // contains HTML is shown as text and cannot run in the visitor's browser.
  function el(tag, cls, text, attrs) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text) node.textContent = text;
    for (var k in attrs || {}) node.setAttribute(k, attrs[k]);
    return node;
  }

  var style = el("style");
  style.textContent = [
    ":host{all:initial;position:fixed;bottom:20px;" + side + ":20px;z-index:2147483000;font:15px/1.45 system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;color:#1c2321;--c:" + color + "}",
    "*{box-sizing:border-box;font-family:inherit}",
    ".launch{width:58px;height:58px;border-radius:50%;border:0;background:var(--c);color:#fff;cursor:pointer;box-shadow:0 6px 20px rgba(0,0,0,.25);display:grid;place-items:center;float:" + side + "}",
    ".launch:focus-visible,button:focus-visible,input:focus-visible,textarea:focus-visible,a:focus-visible{outline:3px solid #f5a524;outline-offset:2px}",
    ".launch svg{width:26px;height:26px}",
    ".panel{display:none;flex-direction:column;width:372px;height:560px;max-height:calc(100vh - 110px);background:#fff;border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.28);overflow:hidden;margin-bottom:12px}",
    ".open .panel{display:flex}",
    "header{background:var(--c);color:#fff;padding:12px 14px;display:flex;align-items:center;gap:10px}",
    "header b{flex:1;font-size:16px}header small{display:block;font-weight:400;opacity:.85;font-size:12px}",
    "header button{background:transparent;border:0;color:#fff;cursor:pointer;font-size:13px;padding:4px 6px;border-radius:6px}header button:hover{background:rgba(255,255,255,.18)}",
    ".log{flex:1;overflow-y:auto;padding:14px;display:flex;flex-direction:column;gap:10px;background:#f6f7f5}",
    ".msg{max-width:86%;padding:9px 12px;border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere}",
    ".bot{background:#fff;border:1px solid #e2e6e3;align-self:flex-start;border-bottom-left-radius:4px}",
    ".user{background:var(--c);color:#fff;align-self:flex-end;border-bottom-right-radius:4px}",
    ".err{background:#fdecea;border-color:#f5c2bd;color:#8a1f17}",
    ".typing::after{content:'\\25CF\\25CF\\25CF';letter-spacing:3px;color:#9aa5a0;animation:blink 1s infinite}@keyframes blink{50%{opacity:.3}}",
    ".src{display:flex;flex-direction:column;gap:3px;margin-top:8px;padding-top:7px;border-top:1px solid #e9ecea;font-size:12.5px}",
    ".src span{color:#6b7772}.src a{color:var(--c);text-decoration:underline}",
    ".chips{display:flex;flex-wrap:wrap;gap:6px}.chips button{border:1px solid var(--c);color:var(--c);background:#fff;border-radius:16px;padding:6px 11px;font-size:13px;cursor:pointer}.chips button:hover{background:var(--c);color:#fff}",
    "form.lead{background:#fff;border:1px solid #e2e6e3;border-radius:12px;padding:12px;display:flex;flex-direction:column;gap:7px;align-self:stretch}",
    "form.lead p{margin:0 0 2px;font-weight:600}form.lead label{font-size:12.5px;color:#4d5955;display:flex;flex-direction:column;gap:2px}",
    "form.lead input[type=text],form.lead input[type=email],form.lead input[type=tel],form.lead textarea{border:1px solid #cdd4d0;border-radius:8px;padding:7px 9px;font-size:14px;width:100%}",
    "form.lead .consent{flex-direction:row;align-items:flex-start;gap:7px}form.lead .hp{position:absolute;left:-9999px}",
    "form.lead .bad{color:#b3261e;font-size:12px}form.lead .row{display:flex;gap:8px}form.lead .row button{flex:1}",
    "form.lead .two{display:grid;grid-template-columns:1fr 1fr;gap:8px}form.lead .two label{min-width:0}",
    ".btn{background:var(--c);color:#fff;border:0;border-radius:8px;padding:9px 12px;font-size:14px;cursor:pointer}.btn.ghost{background:#fff;color:#4d5955;border:1px solid #cdd4d0}.btn:disabled{opacity:.55;cursor:default}",
    ".ask{display:flex;gap:8px;padding:10px;border-top:1px solid #e2e6e3;background:#fff}",
    ".ask textarea{flex:1;resize:none;border:1px solid #cdd4d0;border-radius:10px;padding:9px 10px;font-size:15px;height:42px;max-height:96px}",
    ".foot{font-size:11px;color:#8b9691;text-align:center;padding:0 10px 8px;background:#fff}",
    "@media (max-width:480px){:host{bottom:12px;" + side + ":12px}.panel{position:fixed;inset:0;width:100vw;height:100dvh;max-height:none;border-radius:0;margin:0}.open .launch{display:none}}",
    "@media (prefers-reduced-motion:reduce){.typing::after{animation:none}}"
  ].join("\n");

  var wrap = el("div", "wrap");
  var panel = el("section", "panel", "", { role: "dialog", "aria-label": title + " chat" });
  var header = el("header");
  var heading = el("b", "", title);
  heading.appendChild(el("small", "", "Answers from this website"));
  var resetBtn = el("button", "", "New chat", { type: "button", title: "Start a new conversation" });
  var closeBtn = el("button", "", "✕", { type: "button", "aria-label": "Close chat" });
  header.append(heading, resetBtn, closeBtn);
  var log = el("div", "log", "", { role: "log", "aria-live": "polite" });
  var ask = el("form", "ask");
  var input = el("textarea", "", "", { rows: "1", placeholder: "Type your question…", "aria-label": "Your question", maxlength: "500" });
  var sendBtn = el("button", "btn", "Send", { type: "submit" });
  ask.append(input, sendBtn);
  var foot = el("div", "foot", "Automated assistant. Answers come from this website’s pages.");
  panel.append(header, log, ask, foot);
  var launch = el("button", "launch", "", { type: "button", "aria-label": "Open chat", "aria-expanded": "false" });
  launch.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12Z"/></svg>';   // static markup, no visitor data
  wrap.append(panel, launch);
  root.append(style, wrap);

  var config = { greeting: "Hi! How can I help?", suggestions: [], max_message_chars: 500 };
  var busy = false;

  function scrollDown() { log.scrollTop = log.scrollHeight; }

  function addSources(bubble, sources) {
    if (!sources || !sources.length) return;
    var box = el("div", "src");
    box.appendChild(el("span", "", "Read more on the website:"));
    sources.forEach(function (s) {
      // Relative links belong to the chat service's demo site; absolute ones go to the real page.
      var href = /^https?:\/\//.test(s.url) ? s.url : api + s.url;
      box.appendChild(el("a", "", s.label, { href: href, target: "_blank", rel: "noopener" }));
    });
    bubble.appendChild(box);
  }

  function addMessage(role, text, sources) {
    var bubble = el("div", "msg " + role);
    bubble.appendChild(el("span", "text", text));
    addSources(bubble, sources);
    log.appendChild(bubble);
    scrollDown();
    return bubble;
  }

  function showSuggestions() {
    if (!config.suggestions.length) return;
    var chips = el("div", "chips");
    config.suggestions.forEach(function (q) {
      var b = el("button", "", q, { type: "button" });
      b.addEventListener("click", function () { chips.remove(); send(q); });
      chips.appendChild(b);
    });
    log.appendChild(chips);
  }

  function render() {
    log.textContent = "";
    addMessage("bot", config.greeting);
    state.log.forEach(function (m) { addMessage(m.role, m.text, m.sources); });
    if (!state.log.length) showSuggestions();
  }

  function setOpen(open) {
    state.open = open;
    wrap.classList.toggle("open", open);
    launch.setAttribute("aria-expanded", String(open));
    launch.setAttribute("aria-label", open ? "Close chat" : "Open chat");
    save();
    if (open) { scrollDown(); input.focus(); }
  }

  // ---- lead form, shown inside the conversation ----
  function showLeadForm(promptText) {
    if (state.leadDone || root.querySelector("form.lead")) return;
    var form = el("form", "lead", "", { novalidate: "" });
    form.appendChild(el("p", "", promptText));
    var fields = {};
    var pair = el("div", "two");     // the two optional fields share a line to keep the form short
    [["name", "Name", "text", "name"], ["email", "Work email", "email", "email"],
     ["phone", "Phone (optional)", "tel", "tel"], ["company", "Company (optional)", "text", "organization"]].forEach(function (f, i) {
      var label = el("label", "", f[1]);
      var bad = el("span", "bad", "", { "data-for": f[0] });
      fields[f[0]] = el("input", "", "", { type: f[2], name: f[0], autocomplete: f[3] });
      fields[f[0]].addEventListener("input", function () { bad.textContent = ""; });
      label.append(fields[f[0]], bad);
      (i < 2 ? form : pair).appendChild(label);
    });
    form.appendChild(pair);
    var trap = el("label", "hp", "Website", { "aria-hidden": "true" });   // honeypot: people never see it
    fields.website = el("input", "", "", { type: "text", name: "website", tabindex: "-1", autocomplete: "off" });
    trap.appendChild(fields.website);
    var consent = el("label", "consent");
    fields.consent = el("input", "", "", { type: "checkbox", name: "consent" });
    consent.append(fields.consent, el("span", "", "I agree to be contacted about my enquiry."));
    var consentBad = el("span", "bad", "", { "data-for": "consent" });
    fields.consent.addEventListener("change", function () { consentBad.textContent = ""; });
    var rowBox = el("div", "row");
    var submit = el("button", "btn", "Send my details", { type: "submit" });
    var skip = el("button", "btn ghost", "No thanks", { type: "button" });
    rowBox.append(submit, skip);
    form.append(trap, consent, consentBad, rowBox);
    skip.addEventListener("click", function () { form.remove(); input.focus(); });
    form.addEventListener("submit", function (ev) {
      ev.preventDefault();
      submit.disabled = true;
      form.querySelectorAll(".bad").forEach(function (b) { b.textContent = ""; });
      fetch(api + "/api/leads", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.session, name: fields.name.value, email: fields.email.value, phone: fields.phone.value,
          company: fields.company.value, consent: fields.consent.checked, website: fields.website.value,
          page_url: location.href.slice(0, 500)
        })
      }).then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
        .then(function (res) {
          if (res.body.ok) {
            form.remove();
            state.leadDone = true;
            var thanks = "Thank you, " + fields.name.value.trim().split(" ")[0] + ". The team has your details and this conversation, and will contact you.";
            addMessage("bot", thanks);
            state.log.push({ role: "bot", text: thanks });
            save();
            return;
          }
          var errors = res.body.errors || { email: res.body.detail || "Something went wrong. Please try again." };
          Object.keys(errors).forEach(function (k) {
            var slot = form.querySelector('[data-for="' + k + '"]');
            if (slot) slot.textContent = errors[k];
          });
          submit.disabled = false;
        })
        .catch(function () {
          form.querySelector('[data-for="email"]').textContent = "Could not send. Please check your connection and try again.";
          submit.disabled = false;
        });
    });
    log.appendChild(form);
    scrollDown();
    fields.name.focus();
  }

  // ---- sending a question and reading the streamed reply ----
  function send(text) {
    text = (text || "").trim();
    if (!text || busy) return;
    busy = true; sendBtn.disabled = true;
    var chips = root.querySelector(".chips");
    if (chips) chips.remove();
    addMessage("user", text);
    state.log.push({ role: "user", text: text });
    var bubble = addMessage("bot", "");
    var span = bubble.querySelector(".text");
    span.classList.add("typing");
    var answer = "", sources = [], leadPrompt = null;

    function handle(event, data) {
      if (event === "token") { span.classList.remove("typing"); answer += data.text; span.textContent = answer; scrollDown(); }
      else if (event === "sources") { sources = data.sources; }
      else if (event === "lead_prompt") { leadPrompt = data.text; }
      else if (event === "lead_saved") { state.leadDone = true; }
      else if (event === "error") { fail(data.message); }
    }
    function fail(message) {
      span.classList.remove("typing");
      bubble.classList.add("err");
      answer = message || "Sorry, something went wrong. Please try again.";
      span.textContent = answer;
    }
    function finish() {
      span.classList.remove("typing");
      if (!bubble.classList.contains("err")) {
        addSources(bubble, sources);
        state.log.push({ role: "bot", text: answer, sources: sources });
      } else { state.log.pop(); }     // a failed question is not kept, so a reload does not show it unanswered
      save();
      if (leadPrompt) showLeadForm(leadPrompt);
      busy = false; sendBtn.disabled = false;
      scrollDown();
      if (!leadPrompt) input.focus();
    }

    // EventSource only does GET, and a question belongs in a POST body, so the stream is
    // read by hand: bytes -> text -> events separated by a blank line.
    fetch(api + "/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ session_id: state.session, message: text, page_url: location.href.slice(0, 500) })
    }).then(function (response) {
      if (!response.ok || !response.body) {
        return response.json().catch(function () { return {}; }).then(function (j) {
          fail(typeof j.detail === "string" ? j.detail : null);
        });
      }
      var reader = response.body.getReader(), decoder = new TextDecoder(), buffer = "";
      function pump() {
        return reader.read().then(function (chunk) {
          buffer += decoder.decode(chunk.value || new Uint8Array(), { stream: !chunk.done });
          var parts = buffer.split("\n\n");
          buffer = parts.pop();
          parts.forEach(function (block) {
            var name = (block.match(/^event: (.+)$/m) || [])[1];
            var data = (block.match(/^data: (.+)$/m) || [])[1];
            if (name && data) { try { handle(name, JSON.parse(data)); } catch (e) { /* skip a broken event */ } }
          });
          if (!chunk.done) return pump();
        });
      }
      return pump();
    }).catch(function () { fail("I could not reach the server. Please check your connection and try again."); })
      .then(finish);
  }

  ask.addEventListener("submit", function (ev) { ev.preventDefault(); var t = input.value; input.value = ""; send(t); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); ask.requestSubmit(); }
  });
  launch.addEventListener("click", function () { setOpen(!state.open); });
  closeBtn.addEventListener("click", function () { setOpen(false); launch.focus(); });
  root.addEventListener("keydown", function (ev) { if (ev.key === "Escape" && state.open) { setOpen(false); launch.focus(); } });
  resetBtn.addEventListener("click", function () {
    state.session = ""; state.log = []; state.leadDone = false;
    state.session = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : "s-" + Date.now().toString(36) + Math.random().toString(36).slice(2, 12);
    save(); render(); input.focus();
  });

  function start() {
    document.body.appendChild(host);
    render();
    if (state.open || script.dataset.open === "true") setOpen(true);
    fetch(api + "/api/widget-config").then(function (r) { return r.ok ? r.json() : null; }).then(function (c) {
      if (!c) return;
      config = c;
      input.setAttribute("maxlength", String(c.max_message_chars));
      render();
    }).catch(function () { /* the defaults above are used */ });
  }
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
})();
