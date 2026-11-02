// An MJPEG <img> must be released (src cleared) whenever it leaves the page, or
// Firefox keeps its connection open: at 6 per server the whole dashboard hangs.
// Run from the repo root:  node --test "src/tests/js/*.test.mjs"
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

const APP_JS = readFileSync(new URL('../../bamboozle/static/app.js', import.meta.url), 'utf8');

const ctx = vm.createContext({
    console,
    location: { protocol: 'http:', host: 'test' },
    localStorage: { getItem: () => null, setItem: () => {} },
    document: { getElementById: () => null, addEventListener: () => {} },
    setTimeout: () => 0,
    clearTimeout: () => {},
    setInterval: () => 0,
    clearInterval: () => {},
});
vm.runInContext(APP_JS, ctx);
const BamboozleApp = vm.runInContext('BamboozleApp', ctx);

class FakeImg {
    constructor(src) { this.src = src; }
    getAttribute(name) { return name === 'src' ? this.src ?? null : null; }
    setAttribute(name, value) { if (name === 'src') this.src = value; }
    removeAttribute(name) { if (name === 'src') this.src = undefined; }
    matches(selector) {
        const part = /src\*="([^"]+)"/.exec(selector)?.[1];
        return this.src !== undefined && this.src.includes(part);
    }
    remove() {}
}

// A .camera-feed: the <img> it holds is parsed out of innerHTML, as a browser would.
class FakeFeed {
    constructor() { this.className = 'camera-feed'; this.imgs = []; this._html = ''; this.removed = false; }
    get innerHTML() { return this._html; }
    set innerHTML(html) {
        this._html = html;
        this.imgs = [...html.matchAll(/<img src="([^"]+)"/g)].map((m) => new FakeImg(m[1]));
    }
    querySelector(selector) {
        return selector === 'img'
            ? this.imgs[0] ?? null
            : this.imgs.find((img) => img.matches(selector)) ?? null;
    }
    querySelectorAll(selector) {
        return selector === 'img' ? this.imgs : this.imgs.filter((img) => img.matches(selector));
    }
    remove() { this.removed = true; }
}

const fakeCard = (feed, id = 'p1') => ({
    dataset: { printerId: id },
    querySelector: (selector) => (selector === '.camera-feed' ? feed : null),
    querySelectorAll: (selector) => feed.querySelectorAll(selector),
    remove() { this.removed = true; },
});

function streaming(id = 'p1') {
    const app = Object.create(BamboozleApp.prototype);
    app._poppedOut = new Set();
    const feed = new FakeFeed();
    const card = fakeCard(feed, id);
    app.grid = { querySelector: () => card, querySelectorAll: () => [card], appendChild() {} };
    app.updateCamera(card, id, { camera_enabled: true, camera_available: true, online: true });
    const stream = feed.imgs[0];
    assert.equal(stream.src, `/stream/${id}`);
    return { app, card, feed, stream };
}

test('a stream going back to "connecting" releases its connection', () => {
    const { app, card, feed, stream } = streaming();
    app.updateCamera(card, 'p1', { camera_enabled: true, camera_available: false, online: true });
    assert.match(feed.innerHTML, /Camera connecting/);
    assert.equal(stream.src, undefined);
});

test('a stream replaced by the job thumbnail releases its connection', () => {
    const { app, card, feed, stream } = streaming();
    app.updateCamera(card, 'p1', { camera_enabled: false, online: true, has_thumbnail: true, thumbnail_key: 'k' });
    assert.match(feed.innerHTML, /thumbnail\?v=k/);
    assert.equal(stream.src, undefined);
});

test('a stream removed with its feed releases its connection', () => {
    const { app, card, feed, stream } = streaming();
    app.updateCamera(card, 'p1', { camera_enabled: false, online: true });
    assert.equal(feed.removed, true);
    assert.equal(stream.src, undefined);
});

test('popping a camera out releases the card\'s stream', () => {
    const { app, stream } = streaming();
    app._clearCameraFeed('p1');
    assert.equal(stream.src, undefined);
});

test('removing a printer\'s card releases its stream', () => {
    const { app, card, stream } = streaming();
    app.printers = {};
    app.render();
    assert.equal(card.removed, true);
    assert.equal(stream.src, undefined);
});

test('an update that keeps streaming leaves the stream alone', () => {
    const { app, card, feed, stream } = streaming();
    app.updateCamera(card, 'p1', { camera_enabled: true, camera_available: true, online: true });
    assert.equal(feed.imgs[0], stream);
    assert.equal(stream.src, '/stream/p1');
});
