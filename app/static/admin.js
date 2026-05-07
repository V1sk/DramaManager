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
        div.textContent = message;
        zone.appendChild(div);
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
                zone.innerHTML = '<a href="/admin/sync">已同步 ✓</a>';
            } else {
                zone.innerHTML = '<a href="/admin/sync">需同步: <span class="sync-count">' + n + '</span></a>';
            }
        } catch (_) {
            // network / parse error — ignore, try again next tick
        }
    }
    refreshSyncZone();
    setInterval(refreshSyncZone, 5000);
})();
