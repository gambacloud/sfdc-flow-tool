(function () {
  'use strict';

  // Self-contained star-rating widget. Rendered in a Shadow DOM so it never
  // collides with the host page's own CSS. Posts same-origin to /api/rating -
  // one click, no message required, so it stays inviting rather than naggy.
  // Mirrors salesforce-debugtool's static/rating-widget.js; keep them in sync.

  var RATED_KEY = 'ft_rating_done_at';
  var DISMISSED_KEY = 'ft_rating_dismissed_at';
  var SNOOZE_DAYS = 30; // after rating or dismissing, stay quiet for this long

  function daysSince(ts) {
    if (!ts) return Infinity;
    return (Date.now() - Number(ts)) / 86400000;
  }

  function alreadyHandled() {
    try {
      if (daysSince(localStorage.getItem(RATED_KEY)) < SNOOZE_DAYS) return true;
      if (daysSince(localStorage.getItem(DISMISSED_KEY)) < SNOOZE_DAYS) return true;
    } catch (e) { /* localStorage unavailable, e.g. private mode */ }
    return false;
  }

  var host = document.createElement('div');
  host.id = 'ft-rating-widget-host';
  host.style.all = 'initial';
  document.addEventListener('DOMContentLoaded', mount);
  if (document.readyState === 'complete' || document.readyState === 'interactive') mount();

  function mount() {
    if (alreadyHandled()) return;
    if (document.getElementById('ft-rating-widget-host')) return;
    document.body.appendChild(host);
    var root = host.attachShadow({ mode: 'open' });

    root.innerHTML =
      '<style>' +
      ':host{all:initial}' +
      '*{box-sizing:border-box;font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif}' +
      '.wrap{position:fixed;left:20px;bottom:20px;z-index:2147483000}' +
      '.btn{background:#fff;color:#4b5563;border:1px solid #e5e7eb;border-radius:999px;' +
      'padding:9px 14px;font-size:13px;font-weight:500;cursor:pointer;' +
      'box-shadow:0 2px 10px rgba(0,0,0,.12);display:flex;align-items:center;gap:6px}' +
      '.btn:hover{border-color:#f59e0b;color:#111827}' +
      '.btn .star{color:#f59e0b;font-size:15px}' +
      '.dismiss{position:absolute;top:-8px;right:-8px;width:20px;height:20px;border-radius:50%;' +
      'background:#fff;color:#6b7280;border:1px solid #d1d5db;font-size:13px;line-height:1;cursor:pointer;' +
      'display:flex;align-items:center;justify-content:center;padding:0;box-shadow:0 1px 4px rgba(0,0,0,.2)}' +
      '.dismiss:hover{color:#dc2626;border-color:#dc2626}' +
      '.panel{position:fixed;left:20px;bottom:78px;z-index:2147483000;width:230px;max-width:calc(100vw - 40px);' +
      'background:#fff;color:#111827;border-radius:12px;box-shadow:0 10px 40px rgba(0,0,0,.3);padding:16px;display:none;text-align:center}' +
      '.panel.open{display:block}' +
      '.panel h3{margin:0 0 4px;font-size:14px}' +
      '.panel p.hint{margin:0 0 10px;font-size:12px;color:#6b7280}' +
      '.stars{display:flex;justify-content:center;gap:4px;margin-bottom:2px}' +
      '.stars button{background:none;border:none;cursor:pointer;font-size:26px;color:#d1d5db;padding:2px;line-height:1}' +
      '.stars button.hover,.stars button.on{color:#f59e0b}' +
      '.close{position:absolute;top:8px;right:10px;background:transparent;border:none;color:#9ca3af;cursor:pointer;font-size:16px;line-height:1;padding:4px}' +
      '.msg{font-size:12px;margin-top:8px;min-height:14px}' +
      '.msg.ok{color:#16a34a}' +
      '.msg.err{color:#dc2626}' +
      '</style>' +
      '<div class="wrap">' +
      '  <button class="btn" type="button" aria-label="Rate this tool"><span class="star">★</span>Rate this tool</button>' +
      '  <button class="dismiss" type="button" aria-label="Hide rating button">×</button>' +
      '</div>' +
      '<div class="panel">' +
      '  <button class="close" type="button" aria-label="Close">×</button>' +
      '  <h3>How\'s it going?</h3>' +
      '  <p class="hint">One click, no strings attached.</p>' +
      '  <div class="stars">' +
      '    <button type="button" data-v="1" aria-label="1 star">★</button>' +
      '    <button type="button" data-v="2" aria-label="2 stars">★</button>' +
      '    <button type="button" data-v="3" aria-label="3 stars">★</button>' +
      '    <button type="button" data-v="4" aria-label="4 stars">★</button>' +
      '    <button type="button" data-v="5" aria-label="5 stars">★</button>' +
      '  </div>' +
      '  <div class="msg" role="status"></div>' +
      '</div>';

    var wrap = root.querySelector('.wrap');
    var btn = root.querySelector('.btn');
    var dismissBtn = root.querySelector('.dismiss');
    var panel = root.querySelector('.panel');
    var closeBtn = root.querySelector('.close');
    var starBtns = Array.prototype.slice.call(root.querySelectorAll('.stars button'));
    var msgEl = root.querySelector('.msg');
    var sent = false;

    function paint(upTo, cls) {
      starBtns.forEach(function (b) {
        b.classList.toggle(cls, Number(b.dataset.v) <= upTo);
      });
    }

    btn.addEventListener('click', function () {
      panel.classList.toggle('open');
    });
    closeBtn.addEventListener('click', function () { panel.classList.remove('open'); });
    dismissBtn.addEventListener('click', function () {
      try { localStorage.setItem(DISMISSED_KEY, String(Date.now())); } catch (e) { /* ignore */ }
      wrap.style.display = 'none';
      panel.classList.remove('open');
    });

    starBtns.forEach(function (b) {
      b.addEventListener('mouseenter', function () { if (!sent) paint(Number(b.dataset.v), 'hover'); });
      b.addEventListener('mouseleave', function () { if (!sent) paint(0, 'hover'); });
      b.addEventListener('click', function () {
        if (sent) return;
        sent = true;
        var stars = Number(b.dataset.v);
        paint(stars, 'on');
        starBtns.forEach(function (sb) { sb.disabled = true; });
        msgEl.textContent = 'Sending...';
        msgEl.className = 'msg';

        fetch('/api/rating', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            stars: stars,
            page: location.pathname
          })
        })
          .then(function (res) {
            if (!res.ok) throw new Error('request failed');
            return res.json();
          })
          .then(function () {
            try { localStorage.setItem(RATED_KEY, String(Date.now())); } catch (e) { /* ignore */ }
            msgEl.textContent = 'Thanks for the feedback!';
            msgEl.className = 'msg ok';
            setTimeout(function () {
              wrap.style.display = 'none';
              panel.classList.remove('open');
            }, 1600);
          })
          .catch(function () {
            msgEl.textContent = 'Could not send, please try again later.';
            msgEl.className = 'msg err';
            sent = false;
            starBtns.forEach(function (sb) { sb.disabled = false; });
          });
      });
    });
  }
})();
