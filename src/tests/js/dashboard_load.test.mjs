// Dashboard initial load + WebSocket status marker (app.js).
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
            sockets.push(this);
        }
        open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.(); }
        close() { this.readyState = FakeWebSocket.CLOSED; this.onclose?.(); }
        receive(msg) { this.onmessage?.({ data: JSON.stringify(msg) }); }
    }

    if (dashboard) {
        elements['printer-grid'] = { id: 'printer-grid' };
        elements['ws-status'] = { id: 'ws-status', hidden: true };
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
        fetch: (url) => { const d = deferred(); fetches.push({ url, ...d }); return d.promise; },
        setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
    });
    vm.runInContext(APP_JS, ctx);
    const BamboozleApp = vm.runInContext('BamboozleApp', ctx);

    // Card rendering needs a real DOM; these tests only care about what gets rendered.
    BamboozleApp.prototype.render = function () { this.renderCount = (this.renderCount ?? 0) + 1; };
    BamboozleApp.prototype.showToast = (message, type) => { toasts.push({ message, type }); };

    domReady = true;
    const app = new BamboozleApp();
    const runTimers = (ms) => {
        for (const t of timers.splice(0).filter((t) => t.ms === ms)) t.fn();
    };
    return { app, sockets, fetches, toasts, timers, runTimers, marker: elements['ws-status'] };
}

const printer = (id, extra = {}) => ({ printer_id: id, name: id, online: true, ...extra });
const okResponse = (body) => ({ ok: true, json: async () => body });

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

test('reconnecting marker stays hidden during the first second after load', () => {
    const { marker, timers } = setup();
    assert.equal(marker.hidden, true);
    assert.ok(timers.some((t) => t.ms === 1000), 'a 1s grace timer is armed');
});

test('reconnecting marker shows after the grace period while the WS is not open', () => {
    const { marker, runTimers } = setup();
    runTimers(1000);
    assert.equal(marker.hidden, false);
});

test('no marker on a normal load where the WS opens within the grace period', () => {
    const { marker, sockets, runTimers } = setup();
    sockets[0].open();
    runTimers(1000);
    assert.equal(marker.hidden, true);
});

test('marker shows when the WS drops and hides when it reconnects', () => {
    const { marker, sockets, runTimers } = setup();
    sockets[0].open();
    runTimers(1000);
    assert.equal(marker.hidden, true);

    sockets[0].close();
    assert.equal(marker.hidden, false);

    runTimers(2000);  // existing 2s client reconnect timer
    assert.equal(sockets.length, 2);
    assert.equal(marker.hidden, false, 'stays visible while the new socket is connecting');

    sockets[1].open();
    assert.equal(marker.hidden, true);
});

test('pages without the printer grid make no fetch and no WebSocket', () => {
    const { fetches, sockets } = setup({ dashboard: false });
    assert.equal(fetches.length, 0);
    assert.equal(sockets.length, 0);
});
