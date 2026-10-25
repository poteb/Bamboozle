// Why a running job has no preview picture (app.js thumbnailNote).
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
const job = (extra = {}) => ({
    online: true, gcode_state: 'RUNNING', camera_enabled: false, has_thumbnail: false,
    thumbnail_unavailable: 'internal_storage', ...extra,
});

test('a job on internal storage says why there is no preview, and how to get one', () => {
    const text = app.thumbnailNote(job());
    assert.match(text, /internal storage/i);
    assert.match(text, /Store sent files on external storage/);
});

test('no note without a reason, e.g. from an older server', () => {
    assert.equal(app.thumbnailNote(job({ thumbnail_unavailable: '' })), '');
    assert.equal(app.thumbnailNote({ online: true, gcode_state: 'RUNNING' }), '');
});

test('no note while the camera is shown instead', () => {
    assert.equal(app.thumbnailNote(job({ camera_enabled: true })), '');
});

test('no note when a preview is shown anyway', () => {
    assert.equal(app.thumbnailNote(job({ has_thumbnail: true })), '');
});
