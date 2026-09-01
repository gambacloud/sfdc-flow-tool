(function () {
  'use strict';

  // Self-contained "Contact us" widget. Rendered in a Shadow DOM so it never
  // collides with each host page's own CSS (Tailwind, Angular Material, etc).
  // Posts same-origin to /api/contact - no third-party form service involved.

  var host = document.createElement('div');
  host.id = 'dt-contact-widget-host';
  host.style.all = 'initial';
  document.addEventListener('DOMContentLoaded', mount);
  if (document.readyState === 'complete' || document.readyState === 'interactive') mount();

  var html2canvasPromise = null;
  function loadHtml2Canvas() {
    if (window.html2canvas) return Promise.resolve(window.html2canvas);
    if (!html2canvasPromise) {
      html2canvasPromise = new Promise(function (resolve, reject) {
        var s = document.createElement('script');
        s.src = 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js';
        s.onload = function () { resolve(window.html2canvas); };
        s.onerror = reject;
        document.head.appendChild(s);
      });
    }
    return html2canvasPromise;
  }

  // Opt-in only (checkbox in the form) - never runs unless the user asks for
  // it, since the page can contain sensitive org data (session tokens, etc).
  function captureScreenshot() {
    return loadHtml2Canvas()
      .then(function (html2canvas) {
        host.style.display = 'none';
        return html2canvas(document.body, { logging: false, useCORS: true })
          .then(function (canvas) {
            host.style.display = '';
            return canvas.toDataURL('image/png').split(',')[1] || '';
          });
      })
      .catch(function () {
        host.style.display = '';
        return '';
      });
  }

  function mount() {
    if (document.getElementById('dt-contact-widget-host')) return;
    document.body.appendChild(host);
    var root = host.attachShadow({ mode: 'open' });

    root.innerHTML =
      '<style>' +
      ':host{all:initial}' +
      '*{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}' +
      '.wrap{position:fixed;right:20px;bottom:20px;z-index:2147483000}' +
      '.btn{background:#2563eb;color:#fff;' +
      'border:none;border-radius:999px;padding:12px 18px;font-size:14px;font-weight:600;cursor:pointer;' +
      'box-shadow:0 4px 14px rgba(0,0,0,.25);display:flex;align-items:center;gap:8px}' +
      '.btn:hover{background:#1d4ed8}' +
      '.dismiss{position:absolute;top:-8px;right:-8px;width:20px;height:20px;border-radius:50%;' +
      'background:#fff;color:#6b7280;border:1px solid #d1d5db;font-size:13px;line-height:1;cursor:pointer;' +
      'display:flex;align-items:center;justify-content:center;padding:0;box-shadow:0 1px 4px rgba(0,0,0,.2)}' +
      '.dismiss:hover{color:#dc2626;border-color:#dc2626}' +
      '.panel{position:fixed;right:20px;bottom:80px;z-index:2147483000;width:300px;max-width:calc(100vw - 40px);' +
      'background:#fff;color:#111827;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.3);padding:16px;display:none}' +
      '.panel.open{display:block}' +
      '.panel h3{margin:0 0 4px;font-size:15px}' +
      '.panel p.hint{margin:0 0 10px;font-size:12px;color:#6b7280}' +
      'textarea,input{width:100%;border:1px solid #d1d5db;border-radius:8px;padding:8px;font-size:13px;margin-bottom:8px;resize:vertical}' +
      'textarea{min-height:70px}' +
      '.row{display:flex;gap:8px;align-items:center}' +
      '.send{flex:1;background:#2563eb;color:#fff;border:none;border-radius:8px;padding:8px 12px;font-size:13px;font-weight:600;cursor:pointer}' +
      '.send:disabled{opacity:.6;cursor:default}' +
      '.close{background:transparent;border:none;color:#6b7280;cursor:pointer;font-size:18px;line-height:1;padding:4px}' +
      '.msg{font-size:12px;margin-top:8px}' +
      '.msg.ok{color:#16a34a}' +
      '.msg.err{color:#dc2626}' +
      '.hp{position:absolute;left:-9999px;top:-9999px}' +
      '.shot{display:flex;align-items:center;gap:6px;font-size:12px;color:#374151;margin-bottom:8px}' +
      '.shot input{width:auto;margin:0}' +
      '</style>' +
      '<div class="wrap">' +
      '  <button class="btn" type="button" aria-label="Contact us">✉️ Contact us</button>' +
      '  <button class="dismiss" type="button" aria-label="Hide contact button">×</button>' +
      '</div>' +
      '<div class="panel">' +
      '  <div class="row" style="justify-content:space-between">' +
      '    <h3>Contact us</h3>' +
      '    <button class="close" type="button" aria-label="Close">×</button>' +
      '  </div>' +
      '  <p class="hint">Goes straight to us by email. Nothing is stored or shared.</p>' +
      '  <form>' +
      '    <input class="hp" type="text" name="website" tabindex="-1" autocomplete="off" aria-hidden="true">' +
      '    <textarea name="message" placeholder="What\'s up?" required maxlength="5000"></textarea>' +
      '    <input type="email" name="email" placeholder="Your email (optional, so we can reply)" maxlength="200">' +
      '    <label class="shot"><input type="checkbox" name="attachScreenshot">Attach a screenshot of this page</label>' +
      '    <div class="row"><button class="send" type="submit">Send</button></div>' +
      '    <div class="msg" role="status"></div>' +
      '  </form>' +
      '</div>';

    var wrap = root.querySelector('.wrap');
    var btn = root.querySelector('.btn');
    var dismissBtn = root.querySelector('.dismiss');
    var panel = root.querySelector('.panel');
    var closeBtn = root.querySelector('.close');
    var form = root.querySelector('form');
    var msgEl = root.querySelector('.msg');
    var sendBtn = root.querySelector('.send');

    btn.addEventListener('click', function () {
      panel.classList.toggle('open');
      if (panel.classList.contains('open')) form.querySelector('textarea').focus();
    });
    closeBtn.addEventListener('click', function () { panel.classList.remove('open'); });
    dismissBtn.addEventListener('click', function () {
      wrap.style.display = 'none';
      panel.classList.remove('open');
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      msgEl.textContent = '';
      msgEl.className = 'msg';
      var message = form.message.value.trim();
      if (!message) return;

      sendBtn.disabled = true;
      var wantsScreenshot = form.attachScreenshot.checked;
      sendBtn.textContent = wantsScreenshot ? 'Capturing...' : 'Sending...';

      (wantsScreenshot ? captureScreenshot() : Promise.resolve(''))
        .then(function (screenshot) {
          sendBtn.textContent = 'Sending...';
          return fetch('/api/contact', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              message: message,
              email: form.email.value.trim(),
              honeypot: form.website.value,
              page: location.pathname,
              screenshot: screenshot
            })
          });
        })
        .then(function (res) {
          if (!res.ok) throw new Error('request failed');
          return res.json();
        })
        .then(function () {
          msgEl.textContent = 'Sent, thanks!';
          msgEl.className = 'msg ok';
          form.reset();
          setTimeout(function () { panel.classList.remove('open'); msgEl.textContent = ''; }, 1800);
        })
        .catch(function () {
          msgEl.textContent = 'Could not send, please try again later.';
          msgEl.className = 'msg err';
        })
        .finally(function () {
          sendBtn.disabled = false;
          sendBtn.textContent = 'Send';
        });
    });
  }
})();
