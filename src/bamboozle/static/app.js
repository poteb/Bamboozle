// The reconnecting marker stays hidden this long after page load so a normal
// (fast) WS connect doesn't flash it.
const WS_MARKER_GRACE_MS = 1000;

const STATUS_PRIORITY = {
    'RUNNING': 0, 'PAUSE': 1, 'PREPARE': 2, 'FINISH': 3, 'FAILED': 4, 'IDLE': 5, 'UNKNOWN': 6,
};

// HMS / print error levels (hms.py): fatal stops the job, serious pauses it.
const ERROR_SEVERITY_LABELS = {
    fatal: 'Error', serious: 'Warning', common: 'Notice', info: 'Info',
};

// Bambu Lab filament color catalog — hex -> human-readable name.
// PLA Basic and PLA Matte sourced from Bambu Lab's official color PDFs.
// PETG HF / ABS / Silks etc. are best-effort from BambuStudio profiles and may
// drift from firmware-reported hex. Keys are uppercase 6-digit hex without alpha.
const BAMBU_COLOR_NAMES = {
    // PLA Basic (official)
    'FFFFFF': 'Jade White',
    'EC008C': 'Magenta',
    'E4BD68': 'Gold',
    '3F8E43': 'Mistletoe Green',
    'C12E1F': 'Red',
    '5E43B7': 'Purple',
    'F7E6DE': 'Beige',
    'F55A74': 'Pink',
    'FEC600': 'Sunflower Yellow',
    '847D48': 'Bronze',
    '00B1B7': 'Turquoise',
    '482960': 'Indigo Purple',
    'D1D3D5': 'Light Gray',
    'F5547C': 'Hot Pink',
    'F4EE2A': 'Yellow',
    '6F5034': 'Cocoa Brown',
    '0086D6': 'Cyan',
    '5B6579': 'Blue Grey',
    'A6A9AA': 'Silver',
    'FF6A13': 'Orange',
    'BECF00': 'Bright Green',
    '9D432C': 'Brown',
    '0A2989': 'Blue',
    '545454': 'Dark Gray',
    '8E9089': 'Gray',
    'FF9016': 'Pumpkin Orange',
    '00AE42': 'Bambu Green',
    '9D2235': 'Maroon Red',
    '0056B8': 'Cobalt Blue',
    '000000': 'Black',

    // PLA Matte (official)
    // Note: Matte's "Ivory White" (FFFFFF) and "Charcoal" (000000) collide with
    // Basic's "Jade White" / "Black"; Basic wins since the table is hex-keyed.
    'CBC6B8': 'Bone White',
    'E8DBB7': 'Desert Tan',
    'D3B7A7': 'Latte Brown',
    'AE835B': 'Caramel',
    'B15533': 'Terracotta',
    '7D6556': 'Dark Brown',
    '4D3324': 'Dark Chocolate',
    'AE96D4': 'Lilac Purple',
    'E8AFCF': 'Sakura Pink',
    'F99963': 'Mandarin Orange',
    'F7D959': 'Lemon Yellow',
    '950051': 'Plum',
    'DE4343': 'Scarlet Red',
    'BB3D43': 'Dark Red',
    '68724D': 'Dark Green',
    '61C680': 'Grass Green',
    'C2E189': 'Apple Green',
    'A3D8E1': 'Ice Blue',
    '56B7E6': 'Sky Blue',
    '0078BF': 'Marine Blue',
    '042F56': 'Dark Blue',
    '9B9EA0': 'Ash Gray',
    '757575': 'Nardo Gray',

    // PETG HF / Translucent
    'F2EFE6': 'Cream',
    'D5C9A8': 'Beige',
    'C6B7A0': 'Khaki',
    'E94B3C': 'Coral Red',
    'E37947': 'Terracotta',
    'F8B17E': 'Peach',
    '5BCEFA': 'Cyan',
    '2E96E1': 'Solid Blue',
    '69BFD3': 'Lake Blue',

    // ABS
    'F5F5DC': 'Bone White',
    '273B5F': 'Navy Blue',

    // Silks / Galaxy / Sparkle
    'C9A862': 'Silk Gold',
    'B8B8B8': 'Silk Silver',
    'C76B27': 'Silk Copper',
    '88C9F2': 'Galaxy Blue',
    '4D4DA0': 'Galaxy Purple',
    '5C9D52': 'Galaxy Green',
    '2A1A35': 'Galaxy Black',
    '2A2C30': 'Sparkle Black',
};

function normalizeHex(color) {
    if (!color || typeof color !== 'string') return null;
    let s = color.trim();
    if (s.startsWith('#')) s = s.slice(1);
    // Drop alpha — accept #RRGGBB, #RRGGBBAA. Anything else: fallback.
    if (s.length === 8) s = s.slice(0, 6);
    if (s.length !== 6) return null;
    if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
    return s.toUpperCase();
}

function colorNameFromHex(color) {
    const hex = normalizeHex(color);
    if (!hex) return null;
    return BAMBU_COLOR_NAMES[hex] || null;
}

const ICO = {
    sortAsc:    () => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 5v14"/><polyline points="18 13 12 19 6 13"/></svg>`,
    sortDesc:   () => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5"/><polyline points="6 11 12 5 18 11"/></svg>`,
    camera:     (c='currentColor') => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M23 19a2 2 0 0 1-2 2H3a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4l2-3h6l2 3h4a2 2 0 0 1 2 2z"/><circle cx="12" cy="13" r="4"/></svg>`,
    light:      (c='currentColor') => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/><circle cx="12" cy="12" r="5"/></svg>`,
    expand:     () => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>`,
    fullscreen: () => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M8 3H5a2 2 0 0 0-2 2v3m18 0V5a2 2 0 0 0-2-2h-3m0 18h3a2 2 0 0 0 2-2v-3M3 16v3a2 2 0 0 0 2 2h3"/></svg>`,
    popout:     () => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>`,
    close:      () => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>`,
    pause:      () => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>`,
    play:       () => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>`,
    stop:       () => `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/></svg>`,
    refresh:    (c='currentColor') => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="${c}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>`,
};

class BamboozleApp {
    constructor() {
        this.ws = null;
        this.printers = {};
        this._poppedOut = new Set();
        this._expandedFilaments = new Set();
        this.grid = document.getElementById('printer-grid');

        const saved = JSON.parse(localStorage.getItem('bamboozle-sort') || '{}');
        this.sortBy = saved.sortBy || 'name';
        this.sortDir = saved.sortDir || 'asc';
        this._initSortControls();
        this._updateAllCamsToggle();

        // Set once any WS init/update has been applied; the HTTP initial load
        // must not overwrite newer WS data after that.
        this._wsStateReceived = false;
        // Armed after WS_MARKER_GRACE_MS; see _updateWsMarker.
        this._wsMarkerArmed = false;

        if (this.grid) {
            this._loadInitial();
            this.connect();
            setTimeout(() => {
                this._wsMarkerArmed = true;
                this._updateWsMarker();
            }, WS_MARKER_GRACE_MS);
        }
    }

    // Plain HTTP isn't subject to the browser's WebSocket reconnect throttling
    // (Firefox can delay a new WS by up to ~60s), so render from it first.
    async _loadInitial() {
        try {
            const res = await fetch('/api/printers');
            if (!res.ok) return;
            const data = await res.json();
            this._applyInitialPrinters(data.printers);
        } catch {
            // Stay silent; the WS init will fill the dashboard.
        }
    }

    _applyInitialPrinters(list) {
        if (this._wsStateReceived || !Array.isArray(list)) return;
        for (const state of list) {
            if (state && state.printer_id) this.printers[state.printer_id] = state;
        }
        this.render();
    }

    _updateWsMarker() {
        const marker = document.getElementById('ws-status');
        if (!marker) return;
        // Empty = invisible (see .ws-status in style.css); setting the text
        // lets the role="status" live region announce it.
        marker.textContent = this._wsMarkerArmed && !this._wsOpen() ? 'Reconnecting…' : '';
    }

    _wsOpen() {
        return !!this.ws && this.ws.readyState === WebSocket.OPEN;
    }

    _initSortControls() {
        const select = document.getElementById('sort-by');
        const btn = document.getElementById('sort-dir-btn');
        if (select) select.value = this.sortBy;
        if (btn) btn.innerHTML = this.sortDir === 'asc' ? ICO.sortAsc() : ICO.sortDesc();
    }

    _saveSortPref() {
        localStorage.setItem('bamboozle-sort', JSON.stringify({ sortBy: this.sortBy, sortDir: this.sortDir }));
    }

    setSortBy(value) {
        this.sortBy = value;
        this._saveSortPref();
        this.render();
    }

    toggleSortDir() {
        this.sortDir = this.sortDir === 'asc' ? 'desc' : 'asc';
        document.getElementById('sort-dir-btn').innerHTML =
            this.sortDir === 'asc' ? ICO.sortAsc() : ICO.sortDesc();
        this._saveSortPref();
        this.render();
    }

    _sortedPrinters() {
        const entries = Object.entries(this.printers);
        const dir = this.sortDir === 'asc' ? 1 : -1;

        entries.sort(([, a], [, b]) => {
            let cmp = 0;
            switch (this.sortBy) {
                case 'sort':
                    cmp = (a.sort ?? 0) - (b.sort ?? 0);
                    if (cmp === 0) cmp = a.name.localeCompare(b.name);
                    break;
                case 'name':
                    cmp = a.name.localeCompare(b.name);
                    break;
                case 'status': {
                    const ap = a.online ? (STATUS_PRIORITY[a.gcode_state] ?? 6) : 7;
                    const bp = b.online ? (STATUS_PRIORITY[b.gcode_state] ?? 6) : 7;
                    cmp = ap - bp;
                    break;
                }
                case 'connection':
                    cmp = (b.online ? 1 : 0) - (a.online ? 1 : 0);
                    break;
                case 'eta': {
                    const aActive = a.gcode_state === 'RUNNING' || a.gcode_state === 'PAUSE';
                    const bActive = b.gcode_state === 'RUNNING' || b.gcode_state === 'PAUSE';
                    if (aActive !== bActive) {
                        cmp = aActive ? -1 : 1;
                    } else if (aActive) {
                        cmp = (a.remaining_minutes || 0) - (b.remaining_minutes || 0);
                    }
                    break;
                }
            }
            return cmp * dir;
        });

        return entries;
    }

    connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${proto}//${location.host}/ws`);
        this.ws.onopen = () => this._updateWsMarker();
        this.ws.onmessage = (e) => this.onMessage(JSON.parse(e.data));
        this.ws.onclose = () => {
            this._updateWsMarker();
            setTimeout(() => this.connect(), 2000);
        };
        this.ws.onerror = () => {};
    }

    onMessage(msg) {
        if (msg.type === 'init' || msg.type === 'update') {
            this._wsStateReceived = true;
            // init is the full set: drop printers removed while we weren't listening.
            if (msg.type === 'init') this.printers = {};
            for (const [id, state] of Object.entries(msg.printers)) {
                this.printers[id] = state;
            }
            this.render();
        } else if (msg.type === 'command_result') {
            this.showToast(msg.message, this.commandToastType(msg));
        } else if (msg.type === 'notice') {
            this.showToast(msg.message, msg.level || 'info');
        }
    }

    render() {
        if (!this.grid) return;
        const loading = document.getElementById('loading-msg');
        if (loading) loading.remove();

        this._updateAllCamsToggle();

        const sorted = this._sortedPrinters();

        for (const [id, state] of sorted) {
            let card = this.grid.querySelector(`[data-printer-id="${id}"]`);
            if (!card) {
                card = document.createElement('article');
                card.className = 'printer-card';
                card.dataset.printerId = id;
                this.grid.appendChild(card);
                card.innerHTML = this.renderCard(id, state);
                this.applyErrors(card.querySelector('.card-errors'), state);
            } else {
                this.updateCard(card, id, state);
            }
            // Reorder: appendChild moves existing nodes without recreating them
            this.grid.appendChild(card);
        }

        // Remove cards for printers no longer present
        for (const card of this.grid.querySelectorAll('.printer-card')) {
            if (!this.printers[card.dataset.printerId]) {
                card.remove();
            }
        }
    }

    updateCard(card, id, s) {
        const statusClass = this.getStatusClass(s);
        const statusLabel = this.getStatusLabel(s);

        // Update status dot
        const dot = card.querySelector('.status-dot');
        if (dot) dot.className = `status-dot ${statusClass}`;

        // Update status label
        const label = card.querySelector('.status-label');
        if (label) label.textContent = statusLabel;

        // Update camera feed — only add/remove, never replace an active stream
        this.updateCamera(card, id, s);

        // Update notice line (refused access code / Developer Mode off ...)
        const noticeEl = card.querySelector('.card-notice');
        if (noticeEl) this.applyNotice(noticeEl, this.cardNotice(s));

        // Update HMS messages / print error
        const errorsEl = card.querySelector('.card-errors');
        if (errorsEl) this.applyErrors(errorsEl, s);

        const isPrinting = s.gcode_state === 'RUNNING';
        const isPaused = s.gcode_state === 'PAUSE';

        // Update job info section (always exists, just update contents)
        const jobSection = card.querySelector('.job-section');
        if (jobSection) jobSection.innerHTML = this.jobSectionHtml(s);

        // Update temperatures (tiles come and go: second nozzle, chamber)
        const tempsRow = card.querySelector('.temps-row');
        if (tempsRow) tempsRow.innerHTML = this.tempsRowHtml(s);

        // Update speed
        let speedRow = card.querySelector('.speed-row');
        if (s.online && s.print_speed) {
            if (!speedRow) {
                speedRow = document.createElement('div');
                speedRow.className = 'speed-row';
                const bottom = card.querySelector('.card-bottom');
                const tempsRow = bottom?.querySelector('.temps-row');
                if (tempsRow) tempsRow.after(speedRow);
            }
            speedRow.innerHTML = `<span class="temp-label">Speed</span><span>${this.esc(this.speedText(s))}</span>`;
        } else if (speedRow) {
            speedRow.remove();
        }

        // Update filaments row (AMS + external spool)
        let filamentsRow = card.querySelector('.filaments-row');
        const filamentsHtml = this._renderFilaments(s, id);
        if (filamentsHtml) {
            if (!filamentsRow) {
                const bottom = card.querySelector('.card-bottom');
                // Before Details (or .actions), to match the initial render order.
                const nextEl = bottom?.querySelector('.card-details') || bottom?.querySelector('.actions');
                if (bottom && nextEl) {
                    bottom.insertAdjacentHTML('beforeend', filamentsHtml);
                    filamentsRow = card.querySelector('.filaments-row');
                    if (filamentsRow) bottom.insertBefore(filamentsRow, nextEl);
                }
            } else {
                // Replace contents in place to update colors/tooltips, and refresh class to reflect expanded state.
                const tmp = document.createElement('div');
                tmp.innerHTML = filamentsHtml;
                const fresh = tmp.firstElementChild;
                filamentsRow.className = fresh.className;
                filamentsRow.innerHTML = fresh.innerHTML;
            }
        } else if (filamentsRow) {
            filamentsRow.remove();
        }

        // Update action buttons (div always exists, just update contents)
        const actions = card.querySelector('.actions');
        if (actions) {
            if (isPrinting) {
                actions.innerHTML = `
                    <button class="outline small icon-btn" onclick="app.sendCommand('${id}','pause')" title="Pause">${ICO.pause()}</button>
                    <button class="outline small icon-btn danger-btn" onclick="app.sendCommand('${id}','stop')" title="Stop">${ICO.stop()}</button>`;
            } else if (isPaused) {
                actions.innerHTML = `
                    <button class="outline small icon-btn" onclick="app.sendCommand('${id}','resume')" title="Resume">${ICO.play()}</button>
                    <button class="outline small icon-btn danger-btn" onclick="app.sendCommand('${id}','stop')" title="Stop">${ICO.stop()}</button>`;
            } else {
                actions.innerHTML = '';
            }
        }

        // Update camera toggle button
        const camBtn = card.querySelector('.cam-toggle-btn');
        if (camBtn) {
            camBtn.style.display = s.online ? '' : 'none';
            camBtn.innerHTML = ICO.camera(s.camera_enabled ? '#4caf50' : '#888');
            camBtn.title = s.camera_enabled ? 'Disable camera' : 'Enable camera';
            camBtn.setAttribute('onclick', `app.toggleCamera('${id}', ${!s.camera_enabled})`);
        }

        // Update light button
        const lightBtn = card.querySelector('.light-btn');
        if (lightBtn) {
            lightBtn.style.display = s.online ? '' : 'none';
            lightBtn.innerHTML = ICO.light(s.light_on ? '#ffc107' : '#888');
            lightBtn.setAttribute('onclick', `app.sendCommand('${id}','${s.light_on ? 'light_off' : 'light_on'}')`);
        }

        const refreshBtn = card.querySelector('.refresh-btn');
        if (refreshBtn) refreshBtn.style.display = s.online ? '' : 'none';

        // Update the Details disclosure; only its body, so an open one stays open.
        const details = card.querySelector('.card-details');
        if (details) {
            const items = this.detailItems(s);
            details.hidden = items.length === 0;
            const body = details.querySelector('.card-details-body');
            const html = this.detailsBodyHtml(items);
            if (body && body.innerHTML !== html) body.innerHTML = html;
        }
    }

    _cameraStreamHtml(id) {
        return `<img src="/stream/${id}" alt="Camera">` +
            `<div class="cam-overlay">` +
            `<button onclick="app.fullwindowCamera('${id}')" title="Full window">${ICO.expand()}</button>` +
            `<button onclick="app.fullscreenCamera('${id}')" title="Fullscreen">${ICO.fullscreen()}</button>` +
            `<button onclick="app.popoutCamera('${id}')" title="Pop out">${ICO.popout()}</button>` +
            `</div>`;
    }

    fullwindowCamera(id) {
        const card = this.grid.querySelector(`[data-printer-id="${id}"]`);
        const feed = card?.querySelector('.camera-feed');
        if (!feed) return;

        if (feed.classList.contains('camera-fullwindow')) {
            feed.classList.remove('camera-fullwindow');
        } else {
            // Close any other fullwindow first
            document.querySelectorAll('.camera-fullwindow').forEach(el => el.classList.remove('camera-fullwindow'));
            feed.classList.add('camera-fullwindow');
        }
    }

    fullscreenCamera(id) {
        const card = this.grid.querySelector(`[data-printer-id="${id}"]`);
        const feed = card?.querySelector('.camera-feed');
        if (!feed) return;

        if (document.fullscreenElement) {
            document.exitFullscreen();
        } else {
            feed.requestFullscreen();
        }
    }

    popoutCamera(id) {
        this._poppedOut.add(id);
        // Kill the main window's stream first and wait for browser to release the connection
        this._clearCameraFeed(id);
        setTimeout(() => {
            const win = window.open(`/camera/${id}`, `camera_${id}`, 'width=960,height=600');
            if (!win) {
                this._poppedOut.delete(id);
                this.render();
                return;
            }
            const check = setInterval(() => {
                if (win.closed) {
                    clearInterval(check);
                    this._poppedOut.delete(id);
                    this.render();
                }
            }, 1000);
        }, 500);
    }

    _clearCameraFeed(id) {
        const card = this.grid?.querySelector(`[data-printer-id="${id}"]`);
        const feed = card?.querySelector('.camera-feed');
        if (feed) {
            const img = feed.querySelector('img');
            if (img) {
                img.removeAttribute('src');
                img.remove();
            }
            feed.className = 'camera-feed camera-offline';
            feed.innerHTML = '<span>Popped out</span>';
        }
    }

    updateCamera(card, id, s) {
        if (this._poppedOut.has(id)) return;

        let camFeed = card.querySelector('.camera-feed');
        const wantStream = s.camera_enabled && s.camera_available;
        const wantConnecting = s.camera_enabled && s.online && !s.camera_available;
        const wantThumbnail = !s.camera_enabled && s.has_thumbnail;

        if (wantStream) {
            if (!camFeed) {
                camFeed = document.createElement('div');
                camFeed.className = 'camera-feed';
                camFeed.innerHTML = this._cameraStreamHtml(id);
                const header = card.querySelector('header');
                header.after(camFeed);
            } else if (!camFeed.querySelector('img[src*="/stream/"]')) {
                camFeed.className = 'camera-feed';
                camFeed.innerHTML = this._cameraStreamHtml(id);
            }
        } else if (wantConnecting) {
            if (!camFeed) {
                camFeed = document.createElement('div');
                camFeed.className = 'camera-feed camera-offline';
                camFeed.innerHTML = '<span>Camera connecting...</span>';
                const header = card.querySelector('header');
                header.after(camFeed);
            } else if (camFeed.querySelector('img[src*="/stream/"]')) {
                camFeed.className = 'camera-feed camera-offline';
                camFeed.innerHTML = '<span>Camera connecting...</span>';
            }
        } else if (wantThumbnail) {
            const thumbUrl = `/stream/${id}/thumbnail?v=${encodeURIComponent(s.thumbnail_key || '')}`;
            if (!camFeed) {
                camFeed = document.createElement('div');
                camFeed.className = 'camera-feed thumbnail-feed';
                camFeed.innerHTML = `<img src="${thumbUrl}" alt="Model preview">`;
                const header = card.querySelector('header');
                header.after(camFeed);
            } else {
                const existing = camFeed.querySelector('img[src*="/thumbnail"]');
                if (!existing) {
                    camFeed.className = 'camera-feed thumbnail-feed';
                    camFeed.innerHTML = `<img src="${thumbUrl}" alt="Model preview">`;
                } else if (existing.getAttribute('src') !== thumbUrl) {
                    existing.setAttribute('src', thumbUrl);
                }
            }
        } else if (camFeed) {
            camFeed.remove();
        }
    }

    _filamentTooltip(f, s = {}) {
        const parts = [];
        if (f.color) {
            const colorLabel = colorNameFromHex(f.color) || f.color;
            parts.push(colorLabel);
        }
        const name = f.name || f.filament_type;
        if (name) parts.push(name);
        if (f.source === 'external') {
            parts.push('External spool');
        } else {
            parts.push(`${this.amsName(s, f.ams_index ?? 0)} · Slot ${(f.tray_index ?? 0) + 1}`);
        }
        return parts.join(' · ');
    }

    _filamentLabelExpanded(f) {
        // Expanded view: just color name + filament type. Location is shown in the group header.
        const parts = [];
        if (f.color) {
            const colorLabel = colorNameFromHex(f.color) || f.color;
            parts.push(colorLabel);
        }
        const name = f.name || f.filament_type;
        if (name) parts.push(name);
        return parts.join(' · ');
    }

    _renderFilaments(s, id) {
        if (!s.filaments || s.filaments.length === 0) return '';
        const expanded = this._expandedFilaments.has(id);

        // Bucket filaments by group: AMS units (numeric ascending on ams_index) then external last.
        const amsBuckets = new Map(); // ams_index -> filament[]
        const externalBucket = [];
        for (const f of s.filaments) {
            if (f.source === 'external') {
                externalBucket.push(f);
            } else {
                const idx = (typeof f.ams_index === 'number') ? f.ams_index : 0;
                if (!amsBuckets.has(idx)) amsBuckets.set(idx, []);
                amsBuckets.get(idx).push(f);
            }
        }
        const orderedAmsKeys = Array.from(amsBuckets.keys()).sort((a, b) => a - b);
        const groups = orderedAmsKeys.map(k => ({ kind: 'ams', amsIndex: k, items: amsBuckets.get(k) }));
        if (externalBucket.length > 0) groups.push({ kind: 'external', items: externalBucket });

        const renderGroup = (group) => {
            const items = group.items.map(f => {
                const cls = f.source === 'external' ? 'filament-square external' : 'filament-square';
                const bg = f.color || 'transparent';
                const tip = this.esc(this._filamentTooltip(f, s));
                const square = `<span class="${cls}" style="background:${bg}" title="${tip}"></span>`;
                if (expanded) {
                    const label = this.esc(this._filamentLabelExpanded(f));
                    return `<div class="filament-row-item">${square}<span class="filament-label">${label}</span></div>`;
                }
                return square;
            }).join('');
            let header = '';
            if (expanded) {
                const headerText = group.kind === 'external' ? 'External' : this.amsHeader(s, group.amsIndex);
                header = `<div class="filament-group-header">${this.esc(headerText)}</div>`;
            }
            return `<div class="filament-group">${header}${items}</div>`;
        };

        const groupsHtml = groups.map(renderGroup).join('');
        const rowClass = expanded ? 'filaments-row expanded' : 'filaments-row';
        return `<div class="${rowClass}" onclick="app.toggleFilamentsExpanded('${id}')">${groupsHtml}</div>`;
    }

    toggleFilamentsExpanded(id) {
        if (this._expandedFilaments.has(id)) {
            this._expandedFilaments.delete(id);
        } else {
            this._expandedFilaments.add(id);
        }
        const card = this.grid?.querySelector(`[data-printer-id="${id}"]`);
        if (!card) return;
        const s = this.printers[id];
        if (!s) return;
        const filamentsRow = card.querySelector('.filaments-row');
        const filamentsHtml = this._renderFilaments(s, id);
        if (filamentsRow && filamentsHtml) {
            const tmp = document.createElement('div');
            tmp.innerHTML = filamentsHtml;
            const fresh = tmp.firstElementChild;
            filamentsRow.className = fresh.className;
            filamentsRow.innerHTML = fresh.innerHTML;
        }
    }

    renderCard(id, s) {
        const statusClass = this.getStatusClass(s);
        const statusLabel = this.getStatusLabel(s);

        const isPrinting = s.gcode_state === 'RUNNING';
        const isPaused = s.gcode_state === 'PAUSE';
        const hasJob = isPrinting || isPaused;

        const cameraToggleBtn = `<button class="outline small icon-btn cam-toggle-btn" onclick="app.toggleCamera('${id}', ${!s.camera_enabled})" title="${s.camera_enabled ? 'Disable camera' : 'Enable camera'}" ${s.online ? '' : 'style="display:none"'}>${ICO.camera(s.camera_enabled ? '#4caf50' : '#888')}</button>`;

        let cameraHtml = '';
        if (s.camera_enabled && s.camera_available) {
            cameraHtml = `<div class="camera-feed">${this._cameraStreamHtml(id)}</div>`;
        } else if (s.camera_enabled && s.online) {
            cameraHtml = `<div class="camera-feed camera-offline"><span>Camera connecting...</span></div>`;
        } else if (!s.camera_enabled && s.has_thumbnail) {
            cameraHtml = `<div class="camera-feed thumbnail-feed"><img src="/stream/${id}/thumbnail?v=${encodeURIComponent(s.thumbnail_key || '')}" alt="Model preview"></div>`;
        }

        const filamentsHtml = this._renderFilaments(s, id);
        const notice = this.cardNotice(s);
        const details = this.detailItems(s);

        let actionsHtml = '';
        if (hasJob) {
            if (isPrinting) {
                actionsHtml = `
                    <button class="outline small icon-btn" onclick="app.sendCommand('${id}','pause')" title="Pause">${ICO.pause()}</button>
                    <button class="outline small icon-btn danger-btn" onclick="app.sendCommand('${id}','stop')" title="Stop">${ICO.stop()}</button>`;
            } else if (isPaused) {
                actionsHtml = `
                    <button class="outline small icon-btn" onclick="app.sendCommand('${id}','resume')" title="Resume">${ICO.play()}</button>
                    <button class="outline small icon-btn danger-btn" onclick="app.sendCommand('${id}','stop')" title="Stop">${ICO.stop()}</button>`;
            }
        }

        const lightBtn = `<button class="outline small icon-btn light-btn" onclick="app.sendCommand('${id}','${s.light_on ? 'light_off' : 'light_on'}')" title="Toggle light" ${s.online ? '' : 'style="display:none"'}>${ICO.light(s.light_on ? '#ffc107' : '#888')}</button>`;
        const refreshBtn = `<button class="outline small icon-btn refresh-btn" onclick="app.sendCommand('${id}','refresh')" title="Refresh: ask the printer for a full status report" aria-label="Refresh" ${s.online ? '' : 'style="display:none"'}>${ICO.refresh('#888')}</button>`;

        return `
            <header>
                <span class="status-dot ${statusClass}"></span>
                <strong class="printer-name">${this.esc(s.name)}</strong>
                <span class="status-label">${statusLabel}</span>
                ${cameraToggleBtn}
                ${lightBtn}
                ${refreshBtn}
            </header>
            ${cameraHtml}
            <div class="card-bottom">
                <div class="card-notice${notice && notice.quiet ? ' quiet' : ''}" role="status" title="${notice && !notice.quiet ? this.esc(notice.title) : ''}" data-key="${this.esc(this._noticeKey(notice))}">${this.noticeHtml(notice)}</div>
                <div class="card-errors" role="status"></div>
                <div class="job-section">${this.jobSectionHtml(s)}</div>
                <div class="temps-row">${this.tempsRowHtml(s)}</div>
                ${s.online && s.print_speed ? `
                    <div class="speed-row">
                        <span class="temp-label">Speed</span>
                        <span>${this.esc(this.speedText(s))}</span>
                    </div>
                ` : ''}
                ${filamentsHtml}
                <details class="card-details"${details.length ? '' : ' hidden'}>
                    <summary>Details</summary>
                    <div class="card-details-body">${this.detailsBodyHtml(details)}</div>
                </details>
                <div class="actions">${actionsHtml}</div>
            </div>
        `;
    }

    // Inner HTML of the job section: the stage (also while preparing), then
    // file, progress and ETA during a job.
    jobSectionHtml(s) {
        const stage = this.stageText(s);
        const stageHtml = stage ? `<div class="stage-line">${this.esc(stage)}</div>` : '';
        if (s.gcode_state !== 'RUNNING' && s.gcode_state !== 'PAUSE') return stageHtml;

        const eta = s.remaining_minutes > 0
            ? `${Math.floor(s.remaining_minutes / 60)}h ${s.remaining_minutes % 60}m`
            : '--';
        const etaClass = s.remaining_minutes > 0 && s.remaining_minutes < 10 ? 'eta-green'
            : s.remaining_minutes > 0 && s.remaining_minutes < 30 ? 'eta-yellow' : '';
        return `
                    <div class="file-name">${this.esc(s.file_name || 'Unknown file')}</div>
                    ${stageHtml}
                    ${this._thumbnailNoteHtml(s)}
                    <div class="progress-row">
                        <progress value="${s.progress}" max="100"></progress>
                        <span class="progress-pct">${s.progress}%</span>
                    </div>
                    <div class="detail-row">
                        <span class="eta-value ${etaClass}">ETA: ${eta}</span>
                    </div>`;
    }

    // What the printer is doing, when that says more than the status label:
    // '' for plain printing (stage 0) and when idle.
    stageText(s) {
        if (!s.online || s.stage == null || s.stage === 0) return '';
        return s.stage_name || '';
    }

    // "220 / 220°C"; the target only while one is set.
    tempHtml(temp, target) {
        const now = temp != null ? Math.round(temp) : '--';
        const tgt = target > 0 ? `<span class="temp-target"> / ${Math.round(target)}</span>` : '';
        return `${now}${tgt}&deg;C`;
    }

    // Temperature tiles: each nozzle (left, right on the H2C / H2D), the bed,
    // and the chamber where the printer has a sensor.
    tempItems(s) {
        const sideLabel = { left: 'Nozzle L', right: 'Nozzle R' };
        const nozzles = Array.isArray(s.nozzles) && s.nozzles.length
            ? s.nozzles
            : [{ side: '', temp: s.nozzle_temp, target: s.nozzle_target }];
        const items = nozzles.map((n) => ({
            label: sideLabel[n.side] || 'Nozzle',
            html: this.tempHtml(n.temp, n.target),
        }));
        items.push({ label: 'Bed', html: this.tempHtml(s.bed_temp, s.bed_target) });
        if (s.chamber_temp != null) {
            items.push({ label: 'Chamber', html: this.tempHtml(s.chamber_temp, s.chamber_target) });
        }
        return items;
    }

    tempsRowHtml(s) {
        return this.tempItems(s).map((i) => `
                    <div class="temp-item">
                        <span class="temp-label">${this.esc(i.label)}</span>
                        <span class="temp-value">${i.html}</span>
                    </div>`).join('');
    }

    speedText(s) {
        return s.speed_name ? `${s.speed_name} · ${s.print_speed}%` : `${s.print_speed}%`;
    }

    amsName(s, index) {
        const unit = (s.ams_units || []).find((u) => u.index === index);
        if (unit && unit.name) return unit.name;
        return index >= 128 ? `AMS HT ${index - 127}` : `AMS ${index + 1}`;
    }

    // Expanded filament group header: "AMS 1 · 35% RH · 28°C".
    amsHeader(s, index) {
        const unit = (s.ams_units || []).find((u) => u.index === index) || {};
        const parts = [this.amsName(s, index)];
        if (unit.humidity != null) parts.push(`${unit.humidity}% RH`);
        if (unit.temp != null) parts.push(`${Math.round(unit.temp)}°C`);
        return parts.join(' · ');
    }

    // [label, value] rows for the Details disclosure; unknown fields are left out.
    detailItems(s) {
        if (!s.online) return [];
        const items = [];
        const f = s.fans || {};
        const fans = [['Part', f.part], ['Aux', f.aux], ['Chamber', f.chamber], ['Heatbreak', f.heatbreak]]
            .filter(([, v]) => v != null)
            .map(([name, v]) => `${name} ${v}%`);
        if (fans.length) items.push(['Fans', fans.join(' · ')]);

        const nozzles = (s.nozzles || []).filter((n) => n.type || n.diameter != null);
        const describe = (n) => [n.type, n.diameter != null ? `${n.diameter} mm` : ''].filter(Boolean);
        if (nozzles.length === 1 && !nozzles[0].side) {
            items.push(['Nozzle', describe(nozzles[0]).join(' · ')]);
        } else if (nozzles.length) {
            const side = { left: 'L', right: 'R' };
            items.push(['Nozzles', nozzles
                .map((n) => [side[n.side] || '', ...describe(n)].filter(Boolean).join(' '))
                .join(' · ')]);
        }
        if (s.wifi_dbm != null) items.push(['Wi-Fi', `${s.wifi_dbm} dBm`]);
        if (s.door_open != null) items.push(['Door', s.door_open ? 'Open' : 'Closed']);
        if (s.sdcard != null) items.push(['SD card', s.sdcard ? 'Inserted' : 'None']);
        if (s.firmware) items.push(['Firmware', s.firmware]);
        return items;
    }

    detailsBodyHtml(items) {
        return items.map(([label, value]) =>
            `<span class="detail-label">${this.esc(label)}</span><span>${this.esc(value)}</span>`).join('');
    }

    // Why the job card has no preview picture, or ''.
    thumbnailNote(s) {
        if (s.camera_enabled || s.has_thumbnail) return '';
        if (s.thumbnail_unavailable === 'internal_storage') {
            return 'No preview: the print file is on the printer\'s internal storage. '
                + 'To get previews, insert an SD card or USB drive and enable '
                + '"Store sent files on external storage" in Bambu Studio';
        }
        return '';
    }

    _thumbnailNoteHtml(s) {
        const note = this.thumbnailNote(s);
        return note ? `<div class="thumbnail-note">${this.esc(note)}</div>` : '';
    }

    // One line of "why isn't this working" on the card, or null.
    // `quiet` marks an informational hint rather than a problem.
    cardNotice(s) {
        if (!s.online && s.last_error === 'auth_rejected') {
            return {
                text: 'Access code wrong or changed (toggling LAN Only or Developer Mode '
                    + 'changes it): update it in Settings',
                title: 'The printer refused the login.',
            };
        }
        if (!s.online && s.last_error === 'no_reports') {
            return {
                text: 'Connected, but the printer sends no data: check the serial number '
                    + 'in Settings (it is case-sensitive)',
                title: 'The login worked, but no report arrived on the serial\'s topic.',
            };
        }
        if (!s.online && s.last_error === 'stale') {
            const text = 'No data from the printer for over a minute: reconnecting';
            return { text, title: text };
        }
        if (s.online && s.developer_mode === false) {
            // A fact, not a claim: the light works with it off on the H2C and
            // P2S; which print controls the firmware drops is unconfirmed.
            return {
                text: 'Developer Mode off: print controls (pause, resume, stop, speed) '
                    + 'may be ignored',
                title: 'The printer reports Developer Mode off. Its firmware may silently '
                    + 'ignore print controls from LAN apps (the light works on some models). '
                    + 'To allow them, enable LAN Only + Developer Mode on the printer, then '
                    + 'restart it. This changes the access code: update it in Settings.',
                quiet: true,
            };
        }
        return null;
    }

    // Inner HTML of the notice line. A quiet hint is a <details> whose body
    // holds the fix, so it's reachable by keyboard and touch, not only hover.
    noticeHtml(notice) {
        if (!notice) return '';
        if (!notice.quiet) return this.esc(notice.text);
        return `<details><summary>${this.esc(notice.text)}</summary>`
            // aria-live off: opening it isn't a status change to announce.
            + `<div class="card-notice-detail" aria-live="off">${this.esc(notice.title)}</div></details>`;
    }

    // One HMS message / print error. With a description: "Warning · <text>", then the module
    // and linked code as small secondary text. The text itself is left out
    // (an empty .error-text) and set with textContent by applyErrors: it's
    // Bambu's catalogue text, downloaded at runtime. Without one, the line is
    // "Warning · AMS 2 · <code>".
    errorLineHtml(e) {
        const label = ERROR_SEVERITY_LABELS[e.severity] || '';
        const code = this.esc(e.code);
        const codeHtml = typeof e.url === 'string' && e.url.startsWith('https://')
            ? `<a href="${this.esc(e.url)}" target="_blank" rel="noopener noreferrer" `
                + `title="Look up this code on Bambu Lab's wiki">${code}</a>`
            : code;
        const where = e.module ? `${this.esc(e.module)} · ` : '';
        const sev = label ? `${this.esc(label)} · ` : '';
        if (!e.description) return `<div class="error-line">${sev}${where}${codeHtml}</div>`;
        return `<div class="error-line${e.undocumented ? ' undocumented' : ''}">`
            + `<div class="error-desc">${sev}<span class="error-text"></span></div>`
            + `<div class="error-meta">${where}${codeHtml}</div></div>`;
    }

    // The errors in display order: active, then lingering (server order within).
    _errorGroups(s) {
        if (!s.online || !Array.isArray(s.errors)) return { active: [], lingering: [] };
        return { active: s.errors.filter((e) => e.active), lingering: s.errors.filter((e) => !e.active) };
    }

    // Inner HTML of the card's error area. Active errors (why the job is
    // paused or failed) are a prominent block; the rest are lingering messages
    // the printer keeps until they're acknowledged on it, so they sit in a
    // quiet collapsed line instead of looking like a current fault.
    errorsHtml(s) {
        const { active, lingering } = this._errorGroups(s);
        let html = '';
        if (active.length) {
            html += `<div class="card-errors-active">${active.map((e) => this.errorLineHtml(e)).join('')}</div>`;
        }
        if (lingering.length) {
            const n = lingering.length;
            const summary = `${n}${active.length ? ' more' : ''} printer message${n === 1 ? '' : 's'}`;
            // aria-live off: only active errors are worth announcing.
            html += `<details class="card-errors-lingering" aria-live="off"><summary>${summary}</summary>`
                + `${lingering.map((e) => this.errorLineHtml(e)).join('')}</details>`;
        }
        return html;
    }

    _errorsKey(s) {
        if (!s.online || !Array.isArray(s.errors)) return '';
        return s.errors.map((e) => `${e.active ? 'a' : 'l'}:${e.code}:${e.severity}:${e.module}:${e.undocumented ? 'u' : ''}:${e.description}`).join('|');
    }

    // Update the error area in place, only when the errors change, so an open
    // list of lingering messages stays open across updates. Descriptions go
    // in with textContent, never as HTML (see errorLineHtml).
    applyErrors(el, s) {
        const key = this._errorsKey(s);
        if (el.dataset.key !== key) {
            el.dataset.key = key;
            el.innerHTML = this.errorsHtml(s);
            const { active, lingering } = this._errorGroups(s);
            const texts = [...active, ...lingering].filter((e) => e.description).map((e) => String(e.description));
            el.querySelectorAll('.error-text').forEach((span, i) => { span.textContent = texts[i] || ''; });
        }
    }

    _noticeKey(notice) {
        return notice ? `${notice.quiet ? 'q' : 'w'}:${notice.text}` : '';
    }

    // Update the notice line in place. The content (a live region, maybe an
    // open <details>) is only replaced when the notice itself changes.
    applyNotice(el, notice) {
        const key = this._noticeKey(notice);
        if (el.dataset.key !== key) {
            el.dataset.key = key;
            el.innerHTML = this.noticeHtml(notice);
        }
        el.title = notice && !notice.quiet ? notice.title : '';
        el.classList.toggle('quiet', !!(notice && notice.quiet));
    }

    getStatusClass(s) {
        if (!s.online) return 'status-offline';
        switch (s.gcode_state) {
            case 'RUNNING': return 'status-printing';
            case 'PREPARE': return 'status-printing';
            case 'PAUSE': return 'status-paused';
            case 'FINISH': return 'status-idle';
            case 'FAILED': return 'status-error';
            case 'IDLE': return 'status-idle';
            default: return 'status-idle';
        }
    }

    getStatusLabel(s) {
        if (!s.online) return 'Offline';
        switch (s.gcode_state) {
            case 'RUNNING': return 'Printing';
            case 'PREPARE': return 'Preparing';
            case 'PAUSE': return 'Paused';
            case 'FINISH': return 'Finished';
            case 'FAILED': return 'Failed';
            case 'IDLE': return 'Idle';
            default: return s.gcode_state;
        }
    }

    _updateAllCamsToggle() {
        const btn = document.getElementById('all-cams-toggle');
        if (!btn) return;
        const onlineIds = Object.keys(this.printers).filter(id => this.printers[id].online);
        const allOn = onlineIds.length > 0 && onlineIds.every(id => this.printers[id].camera_enabled === true);
        const color = allOn ? '#4caf50' : '#888';
        btn.innerHTML = `<span class="all-cams-label">All</span>${ICO.camera(color)}`;
        btn.title = allOn ? 'Disable all cameras' : 'Enable all cameras';
        btn.setAttribute('onclick', `app.toggleAllCameras(${!allOn})`);
    }

    async toggleAllCameras(enabled) {
        const ids = Object.keys(this.printers).filter(id => this.printers[id].online);
        await Promise.all(ids.map(id => this.toggleCamera(id, enabled)));
    }

    async toggleCamera(printerId, enabled) {
        try {
            const res = await fetch(`/api/printers/${printerId}/camera`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({enabled}),
            });
            const json = await res.json();
            if (json.success && this.printers[printerId]) {
                this.printers[printerId].camera_enabled = json.camera_enabled;
                if (!json.camera_enabled) {
                    this.printers[printerId].camera_available = false;
                }
                this.render();
                this.showToast(`Camera ${json.camera_enabled ? 'enabled' : 'disabled'}`, 'success');
            }
        } catch (err) {
            this.showToast('Failed to toggle camera', 'error');
        }
    }

    sendCommand(printerId, action, params = {}) {
        if (this._wsOpen()) {
            this.ws.send(JSON.stringify({
                type: 'command',
                printer_id: printerId,
                action,
                params,
            }));
            return;
        }
        // Cards can render from HTTP long before the WS opens, so don't drop
        // the click: use the matching REST endpoint instead.
        return this._sendCommandHttp(printerId, action, params);
    }

    async _sendCommandHttp(printerId, action, params) {
        const route = {
            pause: ['pause'],
            resume: ['resume'],
            stop: ['stop'],
            light_on: ['light', { on: true }],
            light_off: ['light', { on: false }],
            set_speed: ['speed', { level: params.level }],
            refresh: ['refresh'],
        }[action];
        if (!route) {
            this.showToast('Not connected — reconnecting', 'error');
            return;
        }
        const [path, body] = route;
        const opts = { method: 'POST' };
        if (body) {
            opts.headers = { 'Content-Type': 'application/json' };
            opts.body = JSON.stringify(body);
        }
        try {
            const res = await fetch(`/api/printers/${encodeURIComponent(printerId)}/${path}`, opts);
            if (!res.ok) {
                this.showToast(`Command failed (${res.status})`, 'error');
                return;
            }
            const json = await res.json();
            this.showToast(json.message || (json.success ? 'Done' : 'Command failed'),
                this.commandToastType(json));
        } catch {
            this.showToast('Command failed: server unreachable', 'error');
        }
    }

    // A command the printer took (success), one it was sent but didn't show
    // within the wait (info: "sent, but not confirmed"), or one that failed.
    commandToastType(result) {
        if (!result.success) return 'error';
        return result.confirmed === false ? 'info' : 'success';
    }

    showToast(message, type = 'info') {
        const container = document.getElementById('toast-container');
        if (!container) return;
        const toast = document.createElement('div');
        toast.className = `toast toast-${type}`;
        toast.textContent = message;
        container.appendChild(toast);
        setTimeout(() => toast.remove(), 4000);
    }

    // Safe in text and in double-quoted attributes.
    esc(str) {
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML.replace(/"/g, '&quot;');
    }
}

const app = new BamboozleApp();

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.camera-fullwindow').forEach(el => el.classList.remove('camera-fullwindow'));
    }
});
