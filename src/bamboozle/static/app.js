const STATUS_PRIORITY = {
    'RUNNING': 0, 'PAUSE': 1, 'PREPARE': 2, 'FINISH': 3, 'FAILED': 4, 'IDLE': 5, 'UNKNOWN': 6,
};

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
};

class BamboozleApp {
    constructor() {
        this.ws = null;
        this.printers = {};
        this._poppedOut = new Set();
        this.grid = document.getElementById('printer-grid');

        const saved = JSON.parse(localStorage.getItem('bamboozle-sort') || '{}');
        this.sortBy = saved.sortBy || 'name';
        this.sortDir = saved.sortDir || 'asc';
        this._initSortControls();

        if (this.grid) this.connect();
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
        this.ws.onmessage = (e) => this.onMessage(JSON.parse(e.data));
        this.ws.onclose = () => setTimeout(() => this.connect(), 2000);
        this.ws.onerror = () => {};
    }

    onMessage(msg) {
        if (msg.type === 'init' || msg.type === 'update') {
            for (const [id, state] of Object.entries(msg.printers)) {
                this.printers[id] = state;
            }
            this.render();
        } else if (msg.type === 'command_result') {
            this.showToast(msg.message, msg.success ? 'success' : 'error');
        }
    }

    render() {
        if (!this.grid) return;
        const loading = document.getElementById('loading-msg');
        if (loading) loading.remove();

        const sorted = this._sortedPrinters();

        for (const [id, state] of sorted) {
            let card = this.grid.querySelector(`[data-printer-id="${id}"]`);
            if (!card) {
                card = document.createElement('article');
                card.className = 'printer-card';
                card.dataset.printerId = id;
                this.grid.appendChild(card);
                card.innerHTML = this.renderCard(id, state);
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

        const isPrinting = s.gcode_state === 'RUNNING';
        const isPaused = s.gcode_state === 'PAUSE';
        const hasJob = isPrinting || isPaused;

        // Update job info section (always exists, just update contents)
        const jobSection = card.querySelector('.job-section');
        if (jobSection) {
            if (hasJob) {
                const eta = s.remaining_minutes > 0
                    ? `${Math.floor(s.remaining_minutes / 60)}h ${s.remaining_minutes % 60}m`
                    : '--';

                jobSection.innerHTML = `
                    <div class="file-name">${this.esc(s.file_name || 'Unknown file')}</div>
                    <div class="progress-row">
                        <progress value="${s.progress}" max="100"></progress>
                        <span class="progress-pct">${s.progress}%</span>
                    </div>
                    <div class="detail-row">
                        <span class="eta-value">ETA: ${eta}</span>
                    </div>`;
            } else {
                jobSection.innerHTML = '';
            }
        }

        // Update temperatures
        const temps = card.querySelectorAll('.temp-value');
        if (temps.length >= 3) {
            temps[0].innerHTML = `${s.nozzle_temp != null ? Math.round(s.nozzle_temp) : '--'}&deg;C`;
            temps[1].innerHTML = `${s.bed_temp != null ? Math.round(s.bed_temp) : '--'}&deg;C`;
            temps[2].innerHTML = `${s.chamber_temp != null ? Math.round(s.chamber_temp) : '--'}&deg;C`;
        }

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
            speedRow.innerHTML = `<span class="temp-label">Speed</span><span>${s.print_speed}%</span>`;
        } else if (speedRow) {
            speedRow.remove();
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
            if (!camFeed) {
                camFeed = document.createElement('div');
                camFeed.className = 'camera-feed thumbnail-feed';
                camFeed.innerHTML = `<img src="/stream/${id}/thumbnail" alt="Model preview">`;
                const header = card.querySelector('header');
                header.after(camFeed);
            } else if (!camFeed.querySelector('img[src*="/thumbnail"]')) {
                camFeed.className = 'camera-feed thumbnail-feed';
                camFeed.innerHTML = `<img src="/stream/${id}/thumbnail" alt="Model preview">`;
            }
        } else if (camFeed) {
            camFeed.remove();
        }
    }

    renderCard(id, s) {
        const statusClass = this.getStatusClass(s);
        const statusLabel = this.getStatusLabel(s);

        const eta = s.remaining_minutes > 0
            ? `${Math.floor(s.remaining_minutes / 60)}h ${s.remaining_minutes % 60}m`
            : '--';

        const nozzle = s.nozzle_temp != null ? `${Math.round(s.nozzle_temp)}` : '--';
        const bed = s.bed_temp != null ? `${Math.round(s.bed_temp)}` : '--';
        const chamber = s.chamber_temp != null ? `${Math.round(s.chamber_temp)}` : '--';

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
            cameraHtml = `<div class="camera-feed thumbnail-feed"><img src="/stream/${id}/thumbnail" alt="Model preview"></div>`;
        }

        let amsHtml = '';
        if (s.ams_trays && s.ams_trays.length > 0) {
            const trays = s.ams_trays.map(t =>
                `<span class="ams-tray" style="background:${t.color || '#555'}" title="${t.filament_type || 'Empty'}"></span>`
            ).join('');
            amsHtml = `<div class="ams-row">${trays}</div>`;
        }

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

        return `
            <header>
                <span class="status-dot ${statusClass}"></span>
                <strong class="printer-name">${this.esc(s.name)}</strong>
                <span class="status-label">${statusLabel}</span>
                ${cameraToggleBtn}
                ${lightBtn}
            </header>
            ${cameraHtml}
            <div class="card-bottom">
                <div class="job-section">${hasJob ? `
                    <div class="file-name">${this.esc(s.file_name || 'Unknown file')}</div>
                    <div class="progress-row">
                        <progress value="${s.progress}" max="100"></progress>
                        <span class="progress-pct">${s.progress}%</span>
                    </div>
                    <div class="detail-row">
                        <span class="eta-value">ETA: ${eta}</span>
                    </div>
                ` : ''}</div>
                <div class="temps-row">
                    <div class="temp-item">
                        <span class="temp-label">Nozzle</span>
                        <span class="temp-value">${nozzle}&deg;C</span>
                    </div>
                    <div class="temp-item">
                        <span class="temp-label">Bed</span>
                        <span class="temp-value">${bed}&deg;C</span>
                    </div>
                    <div class="temp-item">
                        <span class="temp-label">Chamber</span>
                        <span class="temp-value">${chamber}&deg;C</span>
                    </div>
                </div>
                ${s.online && s.print_speed ? `
                    <div class="speed-row">
                        <span class="temp-label">Speed</span>
                        <span>${s.print_speed}%</span>
                    </div>
                ` : ''}
                ${amsHtml}
                <div class="actions">${actionsHtml}</div>
            </div>
        `;
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
            if (json.success) {
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
        if (this.ws && this.ws.readyState === WebSocket.OPEN) {
            this.ws.send(JSON.stringify({
                type: 'command',
                printer_id: printerId,
                action,
                params,
            }));
        }
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

    esc(str) {
        const d = document.createElement('div');
        d.textContent = str;
        return d.innerHTML;
    }
}

const app = new BamboozleApp();

document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
        document.querySelectorAll('.camera-fullwindow').forEach(el => el.classList.remove('camera-fullwindow'));
    }
});
