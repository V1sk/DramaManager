/* HLS admin shared JS helpers. Loaded by every page extending templates/_base.html.
 *
 * Exports as window globals (no module bundler).
 *   fetchJSON(url, opts={})  — wraps fetch with JSON body + error throw
 *   escapeHtml(s)            — HTML-escape a string for safe innerHTML
 *   confirmDanger(message)   — confirm() wrapper with destructive phrasing
 *   flash(message, kind)     — render a flash message strip
 *   fmtDuration(ms)          — "M:SS"
 *   fmtTime(iso)             — locale time string
 */

(function () {
    'use strict';

    function escapeHtml(s) {
        return String(s ?? '').replace(/[&<>"']/g, function (ch) {
            return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch];
        });
    }

    async function fetchJSON(url, opts) {
        opts = opts || {};
        const init = { method: opts.method || 'GET', headers: opts.headers || {} };
        if (opts.body !== undefined) {
            if (opts.body instanceof FormData) {
                init.body = opts.body;
            } else {
                init.headers['Content-Type'] = 'application/json';
                init.body = JSON.stringify(opts.body);
            }
        }
        if (opts.redirect) init.redirect = opts.redirect;
        const res = await fetch(url, init);
        let body = null;
        const ct = res.headers.get('content-type') || '';
        if (ct.indexOf('application/json') >= 0) {
            try { body = await res.json(); } catch (_) { body = null; }
        } else {
            try { body = await res.text(); } catch (_) { body = ''; }
        }
        if (!res.ok && res.status !== 302) {
            const err = new Error('HTTP ' + res.status);
            err.status = res.status;
            err.body = body;
            throw err;
        }
        return { status: res.status, body: body };
    }

    function confirmDanger(message) {
        return window.confirm(message);
    }

    function flash(message, kind) {
        const zone = document.getElementById('flash-zone');
        if (!zone) { alert(message); return; }
        const div = document.createElement('div');
        div.className = 'flash ' + (kind || 'info');
        div.style.display = 'flex';
        div.style.alignItems = 'flex-start';
        div.style.justifyContent = 'space-between';
        div.style.gap = '8px';
        const span = document.createElement('span');
        span.textContent = message;
        span.style.flex = '1';
        const btn = document.createElement('button');
        btn.innerHTML = '&times;';
        btn.setAttribute('aria-label', 'close');
        btn.style.cssText = 'background:transparent;border:0;color:inherit;cursor:pointer;font-size:18px;line-height:1;padding:0 4px;opacity:0.7;';
        btn.onmouseover = () => (btn.style.opacity = '1');
        btn.onmouseout = () => (btn.style.opacity = '0.7');
        const dismiss = () => { div.style.opacity = '0'; setTimeout(() => div.remove(), 200); };
        btn.onclick = dismiss;
        div.appendChild(span);
        div.appendChild(btn);
        div.style.transition = 'opacity 0.2s ease';
        zone.appendChild(div);
        // Auto-dismiss after 6s for info/warn; keep errors until manual close.
        if (kind !== 'error') setTimeout(dismiss, 6000);
    }

    function fmtDuration(ms) {
        if (ms == null) return '—';
        const s = Math.round(ms / 1000);
        const m = Math.floor(s / 60);
        const r = s - m * 60;
        return m + ':' + String(r).padStart(2, '0');
    }

    function fmtTime(iso) {
        if (!iso) return '—';
        const d = new Date(iso);
        if (isNaN(d)) return iso;
        return d.toLocaleString();
    }

    window.escapeHtml = escapeHtml;
    window.fetchJSON = fetchJSON;
    window.confirmDanger = confirmDanger;
    window.flash = flash;
    window.fmtDuration = fmtDuration;
    window.fmtTime = fmtTime;

    // --- nav-bar sync zone polling ---
    // Polls /admin/sync/summary every 5s, renders "需同步: N" → /admin/sync.
    // Hidden entirely when sync is disabled (response carries `enabled: false`).
    async function refreshSyncZone() {
        const zone = document.getElementById('sync-zone');
        if (!zone) return;
        try {
            const r = await fetch('/admin/sync/summary', {cache: 'no-store'});
            if (!r.ok) return;
            const j = await r.json();
            if (!j.enabled) {
                zone.innerHTML = '';
                return;
            }
            const n = j.non_clean_count || 0;
            if (n === 0) {
                zone.innerHTML = '<a href="/admin/sync" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-status-ready hover:bg-surface-container-highest transition-colors" title="已同步"><span class="material-symbols-outlined text-[16px]">cloud_done</span><span>已同步</span></a>';
            } else {
                zone.innerHTML = '<a href="/admin/sync" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md text-status-warn hover:bg-surface-container-highest transition-colors" title="需同步: ' + n + '"><span class="material-symbols-outlined text-[16px]">cloud_sync</span><span>需同步 <strong>' + n + '</strong></span></a>';
            }
        } catch (_) {
            // network / parse error — ignore, try again next tick
        }
    }
    refreshSyncZone();
    setInterval(refreshSyncZone, 5000);

    // --- nav-bar translate zone polling (ai-translation-queue) ---
    // Polls /admin/translations/summary every 5s, renders "翻译 N" → /admin/translations.
    // Hidden when AI translation is disabled.
    async function refreshTranslateZone() {
        const zone = document.getElementById('translate-zone');
        if (!zone) return;
        try {
            const r = await fetch('/admin/translations/summary', {cache: 'no-store'});
            if (!r.ok) return;
            const j = await r.json();
            if (!j.enabled) { zone.innerHTML = ''; return; }
            const n = j.outstanding || 0;
            if (n === 0) {
                zone.innerHTML = '';
                return;
            }
            const active = (j.queued || 0) + (j.running || 0);
            const cls = (j.failed && active === 0) ? 'text-status-danger' : 'text-status-warn';
            const icon = (active > 0) ? 'translate' : 'error';
            const title = '翻译任务 queued ' + (j.queued||0) + ' / running ' + (j.running||0) + ' / failed ' + (j.failed||0);
            zone.innerHTML = '<a href="/admin/translations" class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-md ' + cls + ' hover:bg-surface-container-highest transition-colors" title="' + title + '"><span class="material-symbols-outlined text-[16px]">' + icon + '</span><span>翻译 <strong>' + n + '</strong></span></a>';
        } catch (_) {
            // network / parse error — ignore, try again next tick
        }
    }
    refreshTranslateZone();
    setInterval(refreshTranslateZone, 5000);
})();
