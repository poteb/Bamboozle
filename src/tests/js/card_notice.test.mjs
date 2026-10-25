// Card notice line: refused access code, printer refusing commands (app.js).
// Run from the repo root:  node --test "src/tests/js/*.test.mjs"
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const APP_JS = readFileSync(new URL('../../bamboozle/static/app.js', import.meta.url), 'utf8');

function loadApp() {
    const ctx = vm.createContext({
        console,
        location: { protocol: 'http:', host: 'test' },
        localStorage: { getItem: () => null, setItem: () => {} },
        document: { getElementById: () => null, addEventListener: () => {} },
        WebSocket: class {},
        fetch: () => new Promise(() => {}),
        setTimeout: () => 0,
        clearTimeout: () => {},
        setInterval: () => 0,
        clearInterval: () => {},
    });
    vm.runInContext(APP_JS, ctx);
    return vm.runInContext('app', ctx);
}

const app = loadApp();
const printer = (extra = {}) => ({ online: true, commands_blocked: false, last_error: '', ...extra });

test('a healthy printer has no notice', () => {
    assert.equal(app.cardNotice(printer()), null);
});

test('a printer refusing commands says how to fix it', () => {
    const n = app.cardNotice(printer({ commands_blocked: true }));
    // The whole fix is visible, not only in a hover tooltip.
    assert.match(n.text, /refus/i);
    assert.match(n.text, /LAN Only \+ Developer Mode/);
    assert.match(n.text, /restart/);
});

test('a refused access code says so, and where to fix it', () => {
    const n = app.cardNotice(printer({ online: false, last_error: 'auth_rejected' }));
    assert.match(n.text, /access code wrong or changed/i);
    assert.match(n.text, /Settings/);
    // Why it changed is visible too, not only in a hover tooltip.
    assert.match(n.text, /LAN Only|Developer Mode/);
});

test('a stale commands_blocked flag is not shown while the printer is offline', () => {
    assert.equal(app.cardNotice(printer({ online: false, commands_blocked: true })), null);
});

test('a printer from an older server without the new fields has no notice', () => {
    assert.equal(app.cardNotice({ online: true }), null);
});

test('a session that never got a report points at the serial', () => {
    const n = app.cardNotice(printer({ online: false, last_error: 'no_reports' }));
    assert.match(n.text, /serial/i);
    assert.match(n.text, /Settings/);
});

test('a session whose reports stopped says it is reconnecting', () => {
    const n = app.cardNotice(printer({ online: false, last_error: 'stale' }));
    assert.match(n.text, /no data|no report/i);
    assert.match(n.text, /reconnect/i);
});

test('a silence reason left over on an online printer is not shown', () => {
    assert.equal(app.cardNotice(printer({ last_error: 'stale' })), null);
});
