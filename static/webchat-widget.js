/*
 * CronoCRM admissions assistant - embeddable webchat widget.
 *
 * A launcher bubble and a popup panel, dropped into any page with one tag:
 *
 *   <script src="/static/webchat-widget.js"
 *           data-agent="https://agent.example.com"
 *           data-channel="1"
 *           data-institution="3"><\/script>
 *
 * (the closing tag is escaped above only so this file survives being INLINED
 * into a page - an unescaped one would end the surrounding script element)
 *
 * THE INSTITUTION IS SETTLED BEFORE THE CONVERSATION. A group serving several
 * colleges cannot answer anything useful until it knows which one, so the
 * panel opens on a PICKER, not on a text box: the visitor chooses from a
 * dropdown built out of live data, and only then does the greeting appear -
 * naming that college and offering what that college actually has. The chosen
 * id rides on every message, so the assistant is told which college it serves
 * and never spends a turn asking.
 *
 * A page that already knows the college (the inquiry page's own login-time
 * dropdown) passes data-institution and the picker never appears. A single-
 * institution school gets no picker either - the server returns an empty list,
 * which is the signal that there is nothing to choose.
 */
(function () {
  'use strict';

  var script = document.currentScript;
  if (!script) { return; }

  var CFG = {
    agent: (script.getAttribute('data-agent') || '').replace(/\/+$/, ''),
    channel: script.getAttribute('data-channel') || '1',
    org: script.getAttribute('data-org') || '',
    institution: script.getAttribute('data-institution') || '',
    title: script.getAttribute('data-title') || 'Admissions assistant',
    subtitle: script.getAttribute('data-subtitle') || '',
    accent: script.getAttribute('data-accent') || '#0c6b58',
    pollMs: parseInt(script.getAttribute('data-poll-ms') || '1500', 10),
    open: script.getAttribute('data-open') === 'true'
  };

  // SaaS Phase 1: org-prefixed routes select the tenant; without an org the
  // classic unprefixed routes serve the default tenant.
  var BASE = CFG.agent + '/webhooks' + (CFG.org ? '/' + encodeURIComponent(CFG.org) : '') +
             '/webchat/' + encodeURIComponent(CFG.channel);

  var STORE_KEY = 'crono_wc_' + (CFG.org || 'default') + '_' + CFG.channel;

  // ── persisted conversation ────────────────────────────────────────────────
  // Session AND institution travel together: a reload must not drop the
  // college and put the visitor back in front of the picker mid-conversation.
  function loadState() {
    try {
      var raw = window.localStorage.getItem(STORE_KEY);
      return raw ? JSON.parse(raw) : {};
    } catch (e) { return {}; }
  }

  function saveState(state) {
    try { window.localStorage.setItem(STORE_KEY, JSON.stringify(state)); } catch (e) { /* private mode */ }
  }

  var state = loadState();
  if (CFG.institution) { state.institutionId = CFG.institution; }   // the page's own pick wins
  var afterId = 0;
  var polling = false;

  // ── shell ─────────────────────────────────────────────────────────────────
  var host = document.createElement('div');
  host.setAttribute('data-crono-webchat', '');
  var root = host.attachShadow ? host.attachShadow({ mode: 'open' }) : host;

  root.appendChild(styleEl());
  var wrap = document.createElement('div');
  wrap.className = 'wrap';
  wrap.innerHTML = markup();
  root.appendChild(wrap);
  (document.body || document.documentElement).appendChild(host);

  var el = {
    launcher: root.querySelector('.launcher'),
    panel: root.querySelector('.panel'),
    close: root.querySelector('.close'),
    restart: root.querySelector('.restart'),
    title: root.querySelector('.title'),
    subtitle: root.querySelector('.subtitle'),
    log: root.querySelector('.log'),
    typing: root.querySelector('.typing'),
    picker: root.querySelector('.picker'),
    select: root.querySelector('.picker select'),
    pickerGo: root.querySelector('.picker button'),
    pickerNote: root.querySelector('.picker .note'),
    form: root.querySelector('form'),
    input: root.querySelector('form input'),
    send: root.querySelector('form button')
  };

  el.title.textContent = CFG.title;
  if (CFG.subtitle) { el.subtitle.textContent = CFG.subtitle; }
  else { el.subtitle.remove(); }

  el.launcher.addEventListener('click', function () { open(); });
  el.close.addEventListener('click', function () { wrap.classList.remove('is-open'); });
  el.restart.addEventListener('click', function () { restart(); });

  // ── opening a conversation ────────────────────────────────────────────────
  var starting = false;

  function open() {
    wrap.classList.add('is-open');
    if (!state.sessionId && !starting) { start(); }
    else if (!isPicking()) { el.input.focus(); }
    if (!polling) { polling = true; poll(); }
  }

  function isPicking() { return el.picker.hasAttribute('data-active'); }

  function start() {
    starting = true;
    setComposerEnabled(false);
    var url = BASE + '/session';
    if (state.institutionId) { url += '?institution_id=' + encodeURIComponent(state.institutionId); }

    fetch(url, { method: 'POST' })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.sessionId = data.session_id;
        saveState(state);
        if (data.institution_required) {
          // the panel opens on the choice, not on a text box
          showPicker(data.institutions || [], data.introduction);
        } else {
          if (data.institution_id) { state.institutionId = data.institution_id; saveState(state); }
          say('ai', data.introduction);
          setComposerEnabled(true);
          el.input.focus();
        }
      })
      .catch(function () {
        say('sys', 'The assistant is unavailable right now. Please try again in a moment.');
      })
      .then(function () { starting = false; });
  }

  function showPicker(institutions, prompt) {
    if (prompt) { say('ai', prompt); }
    el.select.innerHTML = '';
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select a campus...';
    placeholder.disabled = true;
    placeholder.selected = true;
    el.select.appendChild(placeholder);

    institutions.forEach(function (inst) {
      var opt = document.createElement('option');
      opt.value = String(inst.id);
      opt.textContent = inst.name || ('Institution ' + inst.id);
      el.select.appendChild(opt);
    });

    el.picker.setAttribute('data-active', '');
    el.pickerNote.textContent = '';
    el.pickerGo.disabled = true;
    el.select.onchange = function () { el.pickerGo.disabled = !el.select.value; };
    el.pickerGo.onclick = function () { choose(el.select.value, el.select.selectedOptions[0].textContent); };
    el.select.focus();
  }

  function choose(institutionId, label) {
    if (!institutionId) { return; }
    el.pickerGo.disabled = true;
    el.pickerNote.textContent = 'Loading ' + label + '...';

    fetch(BASE + '/introduction?institution_id=' + encodeURIComponent(institutionId))
      .then(function (r) { return r.json(); })
      .then(function (data) {
        state.institutionId = institutionId;
        saveState(state);
        el.picker.removeAttribute('data-active');
        say('ai', data.introduction);
        setComposerEnabled(true);
        el.input.focus();
      })
      .catch(function () {
        el.pickerGo.disabled = false;
        el.pickerNote.textContent = 'Could not load that campus - please try again.';
      });
  }

  function restart() {
    state = {};
    if (CFG.institution) { state.institutionId = CFG.institution; }
    saveState(state);
    afterId = 0;
    el.log.innerHTML = '';
    el.picker.removeAttribute('data-active');
    start();
  }

  // ── sending ───────────────────────────────────────────────────────────────
  el.form.addEventListener('submit', function (e) {
    e.preventDefault();
    var text = el.input.value.trim();
    if (!text || !state.sessionId || isPicking()) { return; }
    el.input.value = '';
    say('me', text);

    var body = { session_id: state.sessionId, text: text };
    // the college goes with EVERY message, so a chat cannot drift off its
    // institution even if the panel is reloaded mid-conversation
    if (state.institutionId) { body.institution_id = Number(state.institutionId); }

    fetch(BASE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    }).catch(function () {
      say('sys', 'Message not delivered. Please check your connection and try again.');
    });
  });

  // ── receiving ─────────────────────────────────────────────────────────────
  function poll() {
    if (!state.sessionId) { window.setTimeout(poll, CFG.pollMs); return; }
    fetch(BASE + '/messages?session_id=' + encodeURIComponent(state.sessionId) + '&after_id=' + afterId)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        (data.messages || []).forEach(function (m) {
          say('ai', m.text);
          if (m.id > afterId) { afterId = m.id; }
        });
        el.typing.style.display = data.typing ? 'block' : 'none';
      })
      .catch(function () { /* agent down - keep polling */ })
      .then(function () { window.setTimeout(poll, CFG.pollMs); });
  }

  // ── view helpers ──────────────────────────────────────────────────────────
  function say(kind, text) {
    if (!text) { return; }
    var d = document.createElement('div');
    d.className = 'msg ' + kind;
    d.textContent = text;                       // never innerHTML - replies are untrusted text
    el.log.appendChild(d);
    el.log.scrollTop = el.log.scrollHeight;
  }

  function setComposerEnabled(on) {
    el.input.disabled = !on;
    el.send.disabled = !on;
    el.input.placeholder = on ? 'Type a message...' : 'Choose your campus to begin';
  }

  function markup() {
    return [
      '<button class="launcher" type="button" aria-label="Open the admissions assistant">',
      '  <svg viewBox="0 0 24 24" width="26" height="26" aria-hidden="true">',
      '    <path fill="currentColor" d="M12 3c5 0 9 3.4 9 7.6 0 4.2-4 7.6-9 7.6-.9 0-1.8-.1-2.6-.3L4 21l1.3-3.6C3.9 16 3 13.9 3 11.6 3 7.4 7 3 12 3z"/>',
      '  </svg>',
      '</button>',
      '<section class="panel" role="dialog" aria-label="Admissions assistant">',
      '  <header>',
      '    <div class="head-text"><span class="title"></span><span class="subtitle"></span></div>',
      '    <button class="restart" type="button" title="Start a new conversation" aria-label="Start a new conversation">&#8635;</button>',
      '    <button class="close" type="button" title="Close" aria-label="Close">&#215;</button>',
      '  </header>',
      '  <div class="log"></div>',
      '  <div class="typing">typing...</div>',
      '  <div class="picker">',
      '    <select aria-label="Choose a campus"></select>',
      '    <button type="button">Continue</button>',
      '    <div class="note"></div>',
      '  </div>',
      '  <form>',
      '    <input type="text" autocomplete="off" disabled placeholder="Choose your campus to begin">',
      '    <button type="submit" disabled>Send</button>',
      '  </form>',
      '</section>'
    ].join('\n');
  }

  function styleEl() {
    var s = document.createElement('style');
    s.textContent = [
      ':host, .wrap { all: initial; }',
      '.wrap { position: fixed; right: 20px; bottom: 20px; z-index: 2147483000;',
      '        font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1a2622;',
      '        --accent: ' + CFG.accent + '; --line: #e3e1da; --bg: #f4f5f4; --muted: #5d6b66; }',
      '.wrap * { box-sizing: border-box; font: inherit; }',

      '.launcher { width: 56px; height: 56px; border: 0; border-radius: 50%; cursor: pointer;',
      '            background: var(--accent); color: #fff; display: flex; align-items: center;',
      '            justify-content: center; box-shadow: 0 6px 20px rgba(0,0,0,.22); margin-left: auto; }',
      '.launcher:hover { filter: brightness(1.08); }',

      '.panel { display: none; flex-direction: column; width: min(380px, calc(100vw - 40px));',
      '         height: min(600px, calc(100vh - 120px)); background: #fff; border: 1px solid var(--line);',
      '         border-radius: 14px; overflow: hidden; box-shadow: 0 16px 44px rgba(0,0,0,.20); margin-bottom: 12px; }',
      '.wrap.is-open .panel { display: flex; }',
      '.wrap.is-open .launcher { display: none; }',

      'header { display: flex; align-items: center; gap: 8px; padding: 12px 14px;',
      '         background: var(--accent); color: #fff; }',
      '.head-text { display: flex; flex-direction: column; flex: 1; min-width: 0; }',
      '.title { font-weight: 600; }',
      '.subtitle { font-size: 12px; opacity: .85; }',
      'header button { background: transparent; border: 0; color: #fff; font-size: 20px;',
      '                line-height: 1; cursor: pointer; padding: 2px 6px; border-radius: 6px; opacity: .85; }',
      'header button:hover { opacity: 1; background: rgba(255,255,255,.15); }',
      '.restart { font-size: 17px; }',

      '.log { flex: 1; overflow-y: auto; padding: 14px; display: flex; flex-direction: column; gap: 8px; }',
      '.msg { max-width: 82%; padding: 8px 12px; border-radius: 12px; white-space: pre-wrap;',
      '       overflow-wrap: anywhere; }',
      '.msg.me { align-self: flex-end; background: var(--accent); color: #fff; border-bottom-right-radius: 3px; }',
      '.msg.ai { align-self: flex-start; background: var(--bg); border: 1px solid var(--line); border-bottom-left-radius: 3px; }',
      '.msg.sys { align-self: center; color: var(--muted); font-size: 12.5px; text-align: center; }',
      '.typing { display: none; color: var(--muted); font-size: 13px; padding: 0 14px 6px; }',

      '.picker { display: none; gap: 8px; padding: 12px 14px; border-top: 1px solid var(--line);',
      '          background: var(--bg); flex-wrap: wrap; }',
      '.picker[data-active] { display: flex; }',
      '.picker select { flex: 1; min-width: 150px; padding: 9px 10px; border: 1px solid var(--line);',
      '                 border-radius: 8px; background: #fff; }',
      '.picker button { padding: 9px 14px; border: 0; border-radius: 8px; background: var(--accent);',
      '                 color: #fff; cursor: pointer; }',
      '.picker button:disabled { opacity: .5; cursor: default; }',
      '.picker .note { flex-basis: 100%; font-size: 12.5px; color: var(--muted); }',

      'form { display: flex; gap: 8px; padding: 12px; border-top: 1px solid var(--line); }',
      'form input { flex: 1; min-width: 0; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px; }',
      'form input:disabled { background: var(--bg); color: var(--muted); }',
      'form button { padding: 10px 16px; border: 0; border-radius: 8px; background: var(--accent);',
      '              color: #fff; cursor: pointer; }',
      'form button:disabled { opacity: .5; cursor: default; }',

      '@media (max-width: 480px) {',
      '  .wrap { right: 12px; bottom: 12px; left: 12px; }',
      '  .panel { width: 100%; height: min(70vh, calc(100vh - 90px)); }',
      '}'
    ].join('\n');
    return s;
  }

  // a host page that wants to drive it: CronoWebchat.open() / .setInstitution(3)
  window.CronoWebchat = {
    open: open,
    close: function () { wrap.classList.remove('is-open'); },
    restart: restart,
    setInstitution: function (id) {
      state.institutionId = String(id);
      saveState(state);
    }
  };

  if (CFG.open) { open(); }
})();
