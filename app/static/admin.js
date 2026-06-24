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

    // --- per-entity translation progress poller (ai-translation-queue) ---
    // After enqueueing translation jobs, poll progress and let the caller repaint
    // as jobs land. `entityRef` null → aggregate across ALL entities of `kind`
    // (library-wide "translate all"); a slug → that entity; `epNumber` further
    // scopes to one episode. `onTick(p)` fires every poll while work is in flight;
    // `onDone(p, reason)` fires once when active===0 (reason 'complete') or the
    // poller gives up ('timeout'). Returns a stop() fn.
    window.pollTranslation = function (kind, entityRef, epNumber, opts) {
        opts = opts || {};
        const intervalMs = opts.intervalMs || 3000;
        const maxMs = opts.maxMs || 900000;  // 15 min ceiling
        const start = Date.now();
        let stopped = false;
        async function tick() {
            if (stopped) return;
            let p = null;
            try {
                const qs = new URLSearchParams({ kind: kind });
                if (entityRef != null) qs.set('entity_ref', entityRef);
                if (epNumber != null) qs.set('ep_number', epNumber);
                const r = await fetch('/admin/translations/progress?' + qs.toString(), { cache: 'no-store' });
                if (r.ok) p = await r.json();
            } catch (_) { /* transient — try again next tick */ }
            if (p) {
                try { if (opts.onTick) await opts.onTick(p); } catch (_) {}
                if (((p.queued || 0) + (p.running || 0)) === 0) {
                    stopped = true;
                    try { if (opts.onDone) await opts.onDone(p, 'complete'); } catch (_) {}
                    return;
                }
            }
            if (Date.now() - start > maxMs) {
                stopped = true;
                try { if (opts.onDone) await opts.onDone(p || {}, 'timeout'); } catch (_) {}
                return;
            }
            setTimeout(tick, intervalMs);
        }
        setTimeout(tick, intervalMs);
        return function () { stopped = true; };
    };

    // nas-source-ingest: open the shared NAS browser modal and resolve with the
    // operator's pick. opts.select = 'file' (click a file to pick) | 'dir' (drill
    // in, then "选择当前文件夹"). Resolves {type, rel, name} or null if cancelled.
    function fmtSize(n) {
        if (n == null) return '';
        const u = ['B', 'KB', 'MB', 'GB', 'TB']; let v = n, i = 0;
        while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
        return v.toFixed(i ? 1 : 0) + u[i];
    }
    const NAS_ROW = 'w-full flex items-center gap-2 px-3 py-2 rounded hover:bg-surface-container-highest text-body-md text-left';
    window.openNasBrowser = function (opts) {
        opts = opts || {};
        const select = opts.select || 'file';
        const modal = document.getElementById('nas-modal');
        if (!modal) return Promise.resolve(null);
        const titleEl = document.getElementById('nas-modal-title');
        const crumbEl = document.getElementById('nas-breadcrumb');
        const listEl = document.getElementById('nas-list');
        const hintEl = document.getElementById('nas-hint');
        const pickDirBtn = document.getElementById('nas-pick-dir');
        titleEl.textContent = opts.title || (select === 'dir' ? '选择 NAS 文件夹' : '选择 NAS 文件');
        hintEl.textContent = select === 'dir' ? '进入目标文件夹后点「选择当前文件夹」' : '点击文件即选中';
        pickDirBtn.classList.toggle('hidden', select !== 'dir');

        let curPath = opts.startPath || '';
        let resolveFn;
        const done = new Promise((res) => { resolveFn = res; });
        // Lock background page scroll while the modal is open — the wheel should
        // only move the #nas-list (which has its own overflow-auto), not the page
        // behind the overlay. Lock both <html> and <body> since which one is the
        // viewport scroll container varies by browser; restore both on close.
        const prevHtmlOverflow = document.documentElement.style.overflow;
        const prevBodyOverflow = document.body.style.overflow;
        document.documentElement.style.overflow = 'hidden';
        document.body.style.overflow = 'hidden';

        function close(result) {
            modal.classList.add('hidden');
            document.documentElement.style.overflow = prevHtmlOverflow;
            document.body.style.overflow = prevBodyOverflow;
            document.removeEventListener('keydown', onKey);
            listEl.onclick = null; pickDirBtn.onclick = null;
            resolveFn(result || null);
        }
        function onKey(e) { if (e.key === 'Escape') close(null); }

        async function load(path) {
            listEl.innerHTML = '<div class="meta p-4">加载中…</div>';
            let data;
            try {
                const r = await fetch('/admin/nas/browse?path=' + encodeURIComponent(path), { cache: 'no-store' });
                if (r.status === 503) { listEl.innerHTML = '<div class="meta p-4">NAS 源导入未启用或不可访问。</div>'; return; }
                if (!r.ok) { listEl.innerHTML = `<div class="meta p-4">读取失败 (${r.status})：${escapeHtml(await r.text())}</div>`; return; }
                data = await r.json();
            } catch (e) { listEl.innerHTML = `<div class="meta p-4">读取出错：${escapeHtml(String(e))}</div>`; return; }
            curPath = data.path || '';
            crumbEl.textContent = '/' + curPath;
            let rows = '';
            if (data.parent != null) {
                rows += `<button class="${NAS_ROW}" data-nav="${escapeHtml(data.parent)}"><span class="material-symbols-outlined text-[18px]">drive_folder_upload</span><span>..（上一级）</span></button>`;
            }
            for (const d of data.dirs) {
                rows += `<button class="${NAS_ROW}" data-nav="${escapeHtml(d.rel)}"><span class="material-symbols-outlined text-[18px] text-tertiary">folder</span><span class="break-all">${escapeHtml(d.name)}</span></button>`;
            }
            for (const f of data.files) {
                rows += `<button class="${NAS_ROW}" data-pick-file="${escapeHtml(f.rel)}" data-name="${escapeHtml(f.name)}"><span class="material-symbols-outlined text-[18px] text-status-info">movie</span><span class="break-all flex-1">${escapeHtml(f.name)}</span><span class="meta">${fmtSize(f.size)}</span></button>`;
            }
            if (!data.dirs.length && !data.files.length) rows += '<div class="meta p-4">（空文件夹 / 无视频文件）</div>';
            listEl.innerHTML = rows;
        }

        listEl.onclick = (ev) => {
            const nav = ev.target.closest('[data-nav]');
            if (nav) { load(nav.dataset.nav); return; }
            const pf = ev.target.closest('[data-pick-file]');
            if (pf && select === 'file') close({ type: 'file', rel: pf.dataset.pickFile, name: pf.dataset.name });
        };
        pickDirBtn.onclick = () => close({ type: 'dir', rel: curPath, name: curPath || '(根目录)' });
        modal.querySelectorAll('[data-nas-close]').forEach((el) => { el.onclick = () => close(null); });
        document.addEventListener('keydown', onKey);
        modal.classList.remove('hidden');
        load(curPath);
        return done;
    };
})();
