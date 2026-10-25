// HMS messages and print errors on the card (app.js).
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
const plain = (html) => html.replace(/<[^>]+>/g, '').replace(/\s+/g, ' ').trim();

const REFUSED = {
    kind: 'hms', code: '0500_0500_0001_0007', severity: 'fatal', module: 'Mainboard',
    description: 'Refused a LAN command it couldn\'t verify (Developer Mode off).',
    url: 'https://e.bambulab.com/index.php?e=0500050000010007&s=device_hms&lang=en', active: false,
};
const H2C_OTHER = {
    kind: 'hms', code: '0500_0600_0002_0006', severity: 'serious', module: 'Mainboard',
    description: '', url: 'https://e.bambulab.com/index.php?e=0500060000020006&s=device_hms&lang=en',
    active: false,
};
const RUNOUT = {
    kind: 'hms', code: '0701_2000_0002_0001', severity: 'serious', module: 'AMS 2',
    description: '', url: 'https://e.bambulab.com/index.php?e=0701200000020001&s=device_hms&lang=en',
    active: true,
};
const printer = (errors, extra = {}) => ({ online: true, errors, ...extra });

test('no errors, or an older server without the field, renders nothing', () => {
    assert.equal(app.errorsHtml(printer([])), '');
    assert.equal(app.errorsHtml({ online: true }), '');
});

test('errors of an offline printer are not shown (they may be stale)', () => {
    assert.equal(app.errorsHtml(printer([RUNOUT], { online: false })), '');
});

test('an active error is a prominent line: severity, module, linked code', () => {
    const html = app.errorsHtml(printer([RUNOUT]));
    assert.match(html, /class="card-errors-active"/);
    assert.doesNotMatch(html, /<details/);
    assert.equal(plain(html), 'Warning · AMS 2 · 0701_2000_0002_0001 ↗');
    assert.match(html, /<a href="https:\/\/e\.bambulab\.com\/index\.php\?e=0701200000020001&amp;s=device_hms&amp;lang=en" target="_blank" rel="noopener noreferrer"[^>]*>0701_2000_0002_0001/);
});

test('lingering messages are a quiet collapsed line, not an active fault', () => {
    const html = app.errorsHtml(printer([REFUSED, H2C_OTHER]));
    assert.doesNotMatch(html, /card-errors-active/);
    assert.match(html, /^<details class="card-errors-lingering"><summary>2 printer messages<\/summary>/);
    // Each still links to the wiki; ours-described codes carry the description.
    assert.match(html, /0500_0500_0001_0007/);
    assert.match(html, /Developer Mode off/);
    assert.match(html, /e=0500060000020006/);
});

test('one lingering message is singular', () => {
    assert.match(app.errorsHtml(printer([H2C_OTHER])), /<summary>1 printer message<\/summary>/);
});

test('active errors come first, lingering ones stay collapsed below', () => {
    const html = app.errorsHtml(printer([RUNOUT, REFUSED]));
    const active = html.indexOf('card-errors-active');
    const lingering = html.indexOf('card-errors-lingering');
    assert.ok(active >= 0 && lingering > active);
    assert.match(html, /<summary>1 more printer message<\/summary>/);
});

test('severity labels', () => {
    const label = (severity) => plain(app.errorLineHtml({ ...RUNOUT, severity, module: '' }));
    assert.equal(label('fatal'), 'Error · 0701_2000_0002_0001 ↗');
    assert.equal(label('serious'), 'Warning · 0701_2000_0002_0001 ↗');
    assert.equal(label('common'), 'Notice · 0701_2000_0002_0001 ↗');
    assert.equal(label('info'), 'Info · 0701_2000_0002_0001 ↗');
    assert.equal(label(''), '0701_2000_0002_0001 ↗');
});

test('a code without an https link is plain text', () => {
    const html = app.errorLineHtml({ ...RUNOUT, url: 'javascript:alert(1)' });
    assert.doesNotMatch(html, /<a /);
    assert.match(html, /0701_2000_0002_0001/);
});

test('error fields are escaped', () => {
    const html = app.errorLineHtml({ ...RUNOUT, module: '<b>', description: '"x" & y' });
    assert.match(html, /&lt;b&gt;/);
    assert.match(html, /&quot;x&quot; &amp; y/);
});

function fakeEl() {
    let writes = 0, html = '';
    return {
        dataset: {},
        get innerHTML() { return html; },
        set innerHTML(v) { html = v; writes++; },
        get writes() { return writes; },
    };
}

test('applyErrors only rewrites when the errors change, so an open list stays open', () => {
    const el = fakeEl();
    app.applyErrors(el, printer([REFUSED]));
    app.applyErrors(el, printer([{ ...REFUSED }]));
    assert.equal(el.writes, 1);
    app.applyErrors(el, printer([{ ...REFUSED, active: true }]));
    assert.equal(el.writes, 2);
    app.applyErrors(el, printer([]));
    assert.equal(el.innerHTML, '');
});
