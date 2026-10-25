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
        document: {
            getElementById: () => null,
            addEventListener: () => {},
            // Just enough for app.esc().
            createElement: () => {
                let text = '';
                return {
                    set textContent(v) { text = String(v); },
                    get innerHTML() {
                        return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
                    },
                };
            },
        },
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
    assert.match(n.text, /may be ignored/);
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

test('a quiet hint puts the fix in a details body, not only a tooltip', () => {
    const html = app.noticeHtml(app.cardNotice(printer({ developer_mode: false })));
    assert.match(html, /^<details><summary>Developer Mode off: [^<]*<\/summary>/);
    assert.match(html, /<div class="card-notice-detail">[^<]*LAN Only \+ Developer Mode[^<]*<\/div><\/details>$/);
});

test('a warning notice is plain escaped text', () => {
    assert.equal(app.noticeHtml({ text: 'a <b> & c', title: 't' }), 'a &lt;b&gt; &amp; c');
    assert.equal(app.noticeHtml(null), '');
});

function fakeNoticeEl() {
    const classes = new Set();
    let writes = 0, html = '';
    return {
        dataset: {},
        title: '',
        get innerHTML() { return html; },
        set innerHTML(v) { html = v; writes++; },
        get writes() { return writes; },
        classList: { toggle: (c, on) => (on ? classes.add(c) : classes.delete(c)), has: (c) => classes.has(c) },
    };
}

test('applyNotice switches between the quiet hint and a warning', () => {
    const el = fakeNoticeEl();
    const hint = app.cardNotice(printer({ developer_mode: false }));
    app.applyNotice(el, hint);
    assert.equal(el.classList.has('quiet'), true);
    assert.equal(el.title, '');
    assert.match(el.innerHTML, /<details>/);

    const auth = app.cardNotice(printer({ online: false, last_error: 'auth_rejected' }));
    app.applyNotice(el, auth);
    assert.equal(el.classList.has('quiet'), false);
    assert.equal(el.title, auth.title);
    assert.doesNotMatch(el.innerHTML, /<details>/);

    app.applyNotice(el, null);
    assert.equal(el.innerHTML, '');
    assert.equal(el.title, '');
});

test('applyNotice leaves an unchanged hint alone, so an open details stays open', () => {
    const el = fakeNoticeEl();
    const s = printer({ developer_mode: false });
    app.applyNotice(el, app.cardNotice(s));
    app.applyNotice(el, app.cardNotice(s));
    assert.equal(el.writes, 1);
});
