// Dashboard initial load, WebSocket status marker and command fallback (app.js).
// Run from the repo root:  node --test "src/tests/js/*.test.mjs"
// Zero dependencies: app.js is loaded into a vm context with minimal stubs.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const APP_JS = readFileSync(new URL('../../bamboozle/static/app.js', import.meta.url), 'utf8');

const flush = () => new Promise((r) => setImmediate(r));

function deferred() {
    let resolve, reject;
    const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
    return { promise, resolve, reject };
}

function setup({ dashboard = true } = {}) {
    const timers = [];
    const sockets = [];
    const fetches = [];
    const toasts = [];
    const elements = {};
    let domReady = false;  // the load-time `app` instance sees no dashboard

    class FakeWebSocket {
        static CONNECTING = 0;
        static OPEN = 1;
        static CLOSING = 2;
        static CLOSED = 3;
        constructor(url) {
            this.url = url;
            this.readyState = FakeWebSocket.CONNECTING;
            this.sent = [];
            sockets.push(this);
        }
        send(data) { this.sent.push(JSON.parse(data)); }
        open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.(); }
        close() { this.readyState = FakeWebSocket.CLOSED; this.onclose?.(); }
        receive(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
    }

    if (dashboard) {
        elements['printer-grid'] = { id: 'printer-grid' };
        elements['ws-status'] = { id: 'ws-status', textContent: '' };
    }

    const ctx = vm.createContext({
        console,
        location: { protocol: 'http:', host: 'test' },
        localStorage: { getItem: () => null, setItem: () => {} },
        document: {
            getElementById: (id) => (domReady ? elements[id] ?? null : null),
            addEventListener: () => {},
        },
        WebSocket: FakeWebSocket,
        fetch: (url, opts) => { const d = deferred(); fetches.push({ url, opts, ...d }); return d.promise; },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
    });
    vm.runInContext(APP_JS, ctx);
    const BamboozleApp = vm.runInContext('BamboozleApp', ctx);
    const graceMs = vm.runInContext('WS_MARKER_GRACE_MS', ctx);

    // Card rendering needs a real DOM; these tests only care about what gets rendered.
    BamboozleApp.prototype.render = function () { this.renderCount = (this.renderCount ?? 0) + 1; };
    BamboozleApp.prototype.showToast = (message, type) => { toasts.push({ message, type }); };

    domReady = true;
    const app = new BamboozleApp();
    // Fire pending timers scheduled with exactly `ms`; leave the rest pending.
    const runTimers = (ms) => {
        const due = timers.filter((t) => t.ms === ms);
        for (const t of due) timers.splice(timers.indexOf(t), 1);
        for (const t of due) t.fn();
    };
    const marker = elements['ws-status'];
    const markerShown = () => !!marker.textContent;
    return { app, sockets, fetches, toasts, timers, runTimers, graceMs, markerShown };
}

const printer = (id, extra = {}) => ({ printer_id: id, name: id, online: true, ...extra });
const okResponse = (body) => ({ ok: true, json: async () => body });

// --- HTTP initial load ---

test('page load fetches /api/printers and renders them before the WebSocket opens', async () => {
    const { app, fetches, sockets } = setup();
    assert.equal(fetches.length, 1);
    assert.equal(fetches[0].url, '/api/printers');
    assert.equal(sockets.length, 1, 'WebSocket is still opened for live updates');

    fetches[0].resolve(okResponse({ printers: [printer('a'), printer('b', { name: 'Bee' })] }));
    await flush();

    assert.deepEqual(Object.keys(app.printers).sort(), ['a', 'b']);
    assert.equal(app.printers.b.name, 'Bee');
    assert.ok(app.renderCount >= 1);
    assert.equal(sockets[0].readyState, 0, 'rendered while WS still connecting');
});

test('a later WebSocket update overwrites the HTTP state', async () => {
    const { app, fetches, sockets } = setup();
    fetches[0].resolve(okResponse({ printers: [printer('a', { progress: 1 })] }));
    await flush();

    sockets[0].open();
    sockets[0].receive({ type: 'update', printers: { a: printer('a', { progress: 50 }) } });
    assert.equal(app.printers.a.progress, 50);
});

test('WS init replaces the printer set, dropping printers it no longer lists', async () => {
    const { app, fetches, sockets } = setup();
    fetches[0].resolve(okResponse({ printers: [printer('a'), printer('gone')] }));
    await flush();

    sockets[0].open();
    sockets[0].receive({ type: 'init', printers: { a: printer('a') } });
    assert.deepEqual(Object.keys(app.printers), ['a']);
});

test('an HTTP response arriving after WS init does not overwrite newer WS data', async () => {
    const { app, fetches, sockets } = setup();
    sockets[0].open();
    sockets[0].receive({ type: 'init', printers: { a: printer('a', { progress: 70 }) } });
    const rendersAfterInit = app.renderCount;

    fetches[0].resolve(okResponse({ printers: [printer('a', { progress: 10 }), printer('stale')] }));
    await flush();

    assert.equal(app.printers.a.progress, 70);
    assert.equal(app.printers.stale, undefined);
    assert.equal(app.renderCount, rendersAfterInit);
});

test('HTTP still applies after a socket opened and closed without sending init', async () => {
    const { app, fetches, sockets } = setup();
    sockets[0].open();
    sockets[0].close();

    fetches[0].resolve(okResponse({ printers: [printer('a')] }));
    await flush();
    assert.deepEqual(Object.keys(app.printers), ['a']);
});

test('a failed HTTP fetch is silent and falls back to the WebSocket', async () => {
    const { app, fetches, sockets, toasts } = setup();
    fetches[0].reject(new TypeError('network down'));
    await flush();
    assert.deepEqual(Object.keys(app.printers), []);  // cross-realm object: compare keys
    assert.deepEqual(toasts, []);

    sockets[0].open();
    sockets[0].receive({ type: 'init', printers: { a: printer('a') } });
    assert.deepEqual(Object.keys(app.printers), ['a']);
});

test('a non-OK HTTP response is silent and ignored', async () => {
    const { app, fetches, toasts } = setup();
    fetches[0].resolve({ ok: false, status: 500, json: async () => ({ detail: 'boom' }) });
    await flush();
    assert.deepEqual(Object.keys(app.printers), []);  // cross-realm object: compare keys
    assert.deepEqual(toasts, []);
    assert.equal(app.renderCount ?? 0, 0);
});

// --- Reconnecting marker ---

test('grace period is about one second', () => {
    const { graceMs, timers } = setup();
    assert.ok(graceMs >= 500 && graceMs <= 2000, `grace ${graceMs}ms`);
    assert.ok(timers.some((t) => t.ms === graceMs), 'grace timer is armed on load');
});

test('marker stays hidden during the grace period even if the WS has already failed', () => {
    const { markerShown, sockets, runTimers, graceMs } = setup();
    assert.equal(markerShown(), false);
    sockets[0].close();
    assert.equal(markerShown(), false, 'hidden inside the grace period');
    runTimers(graceMs);
    assert.equal(markerShown(), true, 'shown once the grace period ends');
});

test('marker shows after the grace period while the WS is still connecting', () => {
    const { markerShown, runTimers, graceMs } = setup();
    runTimers(graceMs);
    assert.equal(markerShown(), true);
});

test('no marker on a normal load where the WS opens within the grace period', () => {
    const { markerShown, sockets, runTimers, graceMs } = setup();
    sockets[0].open();
    runTimers(graceMs);
    assert.equal(markerShown(), false);
});

test('marker shows when the WS drops and hides when it reconnects', () => {
    const { markerShown, sockets, runTimers, graceMs } = setup();
    sockets[0].open();
    runTimers(graceMs);
    assert.equal(markerShown(), false);

    sockets[0].close();
    assert.equal(markerShown(), true);

    runTimers(2000);  // existing 2s client reconnect timer
    assert.equal(sockets.length, 2);
    assert.equal(markerShown(), true, 'stays visible while the new socket is connecting');

    sockets[1].open();
    assert.equal(markerShown(), false);
});

test('pages without the printer grid make no fetch and no WebSocket', () => {
    const { fetches, sockets } = setup({ dashboard: false });
    assert.equal(fetches.length, 0);
    assert.equal(sockets.length, 0);
});

// --- Commands while the WS is down ---

test('commands go over the WS when it is open', () => {
    const { app, sockets, fetches } = setup();
    sockets[0].open();
    app.sendCommand('p1', 'pause');
    assert.equal(sockets[0].sent.length, 1);
    assert.equal(sockets[0].sent[0].action, 'pause');
    assert.equal(fetches.length, 1, 'only the initial /api/printers fetch');
});

for (const [action, params, url, body] of [
    ['pause', {}, '/api/printers/p1/pause', undefined],
    ['resume', {}, '/api/printers/p1/resume', undefined],
    ['stop', {}, '/api/printers/p1/stop', undefined],
    ['light_on', {}, '/api/printers/p1/light', { on: true }],
    ['light_off', {}, '/api/printers/p1/light', { on: false }],
    ['set_speed', { level: 3 }, '/api/printers/p1/speed', { level: 3 }],
]) {
    test(`${action} falls back to REST while the WS is not open and toasts the result`, async () => {
        const { app, fetches, toasts } = setup();
        const done = app.sendCommand('p1', action, params);
        const call = fetches[1];
        assert.equal(call.url, url);
        assert.equal(call.opts.method, 'POST');
        assert.deepEqual(call.opts.body === undefined ? undefined : JSON.parse(call.opts.body), body);

        call.resolve(okResponse({ success: true, message: 'ok!' }));
        await done;
        assert.deepEqual(toasts.map((t) => ({ ...t })), [{ message: 'ok!', type: 'success' }]);
    });
}

test('a failed REST command result toasts as an error', async () => {
    const { app, fetches, toasts } = setup();
    const done = app.sendCommand('p1', 'stop');
    fetches[1].resolve(okResponse({ success: false, message: 'Printer not connected' }));
    await done;
    assert.equal(toasts[0].type, 'error');
    assert.equal(toasts[0].message, 'Printer not connected');
});

test('a REST command that cannot reach the server toasts an error', async () => {
    const { app, fetches, toasts } = setup();
    const done = app.sendCommand('p1', 'pause');
    fetches[1].reject(new TypeError('network down'));
    await done;
    assert.equal(toasts.length, 1);
    assert.equal(toasts[0].type, 'error');
});

test('a command with no REST equivalent toasts that the dashboard is reconnecting', async () => {
    const { app, fetches, toasts } = setup();
    await app.sendCommand('p1', 'something_else');
    assert.equal(fetches.length, 1, 'no REST call');
    assert.equal(toasts.length, 1);
    assert.match(toasts[0].message, /reconnecting/i);
});
