// Card notice line: refused access code, Developer Mode off hint (app.js).
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
const printer = (extra = {}) => ({ online: true, developer_mode: null, last_error: '', ...extra });

test('a healthy printer has no notice', () => {
    assert.equal(app.cardNotice(printer()), null);
});

test('Developer Mode off is a quiet hint naming the print controls', () => {
    const n = app.cardNotice(printer({ developer_mode: false }));
    assert.equal(n.quiet, true);
    assert.match(n.text, /Developer Mode off/);
    assert.match(n.text, /pause/);
    assert.match(n.text, /may be refused/);
    // A fact, not a claim that every command is refused: the light works.
    assert.doesNotMatch(n.text, /refusing/i);
    // How to fix it is in the tooltip.
    assert.match(n.title, /LAN Only \+ Developer Mode/);
    assert.match(n.title, /restart/);
    assert.match(n.title, /access code/i);
});

test('Developer Mode on or unknown shows nothing', () => {
    assert.equal(app.cardNotice(printer({ developer_mode: true })), null);
    assert.equal(app.cardNotice(printer({ developer_mode: null })), null);
});

test('commands_blocked alone (the earlier field) shows nothing', () => {
    assert.equal(app.cardNotice(printer({ commands_blocked: true })), null);
});

test('a refused access code says so, and where to fix it', () => {
    const n = app.cardNotice(printer({ online: false, last_error: 'auth_rejected' }));
    assert.ok(!n.quiet);
    assert.match(n.text, /access code wrong or changed/i);
    assert.match(n.text, /Settings/);
    // Why it changed is visible too, not only in a hover tooltip.
    assert.match(n.text, /LAN Only|Developer Mode/);
});

test('a stale Developer Mode flag is not shown while the printer is offline', () => {
    assert.equal(app.cardNotice(printer({ online: false, developer_mode: false })), null);
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
