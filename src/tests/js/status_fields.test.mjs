// Temperatures, stage, speed, AMS headers and the Details list (app.js).
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
const plain = (html) => html.replace(/<[^>]+>/g, '').replace(/&deg;/g, '°');
// Arrays from the vm context have another realm's prototype: compare as JSON.
const j = (v) => JSON.parse(JSON.stringify(v));
const labels = (items) => j(items.map((i) => i.label));

test('a temperature shows its target only while one is set', () => {
    assert.equal(plain(app.tempHtml(219.9, 220)), '220 / 220°C');
    assert.equal(plain(app.tempHtml(28.9, 0)), '29°C');
    assert.equal(plain(app.tempHtml(28.9, null)), '29°C');
    assert.equal(plain(app.tempHtml(null, null)), '--°C');
});

test('a single-nozzle printer without a chamber sensor shows nozzle and bed', () => {
    const items = app.tempItems({
        nozzles: [{ side: '', temp: 220, target: 220 }],
        bed_temp: 65, bed_target: 65, chamber_temp: null,
    });
    assert.deepEqual(labels(items), ['Nozzle', 'Bed']);
    assert.equal(plain(items[1].html), '65 / 65°C');
});

test('the H2C shows both nozzles, left first, and the chamber', () => {
    const items = app.tempItems({
        nozzles: [
            { side: 'left', temp: 50, target: 25 },
            { side: 'right', temp: 220, target: 220 },
        ],
        bed_temp: 55, bed_target: 55, chamber_temp: 30, chamber_target: null,
    });
    assert.deepEqual(labels(items), ['Nozzle L', 'Nozzle R', 'Bed', 'Chamber']);
    assert.equal(plain(items[0].html), '50 / 25°C');
    assert.equal(plain(items[3].html), '30°C');
});

test('a state without the nozzle list (offline, older server) still has a nozzle tile', () => {
    const items = app.tempItems({ nozzle_temp: 30, bed_temp: null });
    assert.deepEqual(labels(items), ['Nozzle', 'Bed']);
    assert.equal(plain(items[0].html), '30°C');
});

test('the stage line names what the printer is doing, not plain printing', () => {
    assert.equal(app.stageText({ online: true, stage: 7, stage_name: 'Heating nozzle' }), 'Heating nozzle');
    assert.equal(app.stageText({ online: true, stage: 0, stage_name: 'Printing' }), '');
    assert.equal(app.stageText({ online: true, stage: null, stage_name: '' }), '');
    assert.equal(app.stageText({ online: false, stage: 7, stage_name: 'Heating nozzle' }), '');
    assert.equal(app.stageText({ online: true }), '');
});

test('the job section shows the stage while preparing, before any job info', () => {
    const html = app.jobSectionHtml({
        online: true, gcode_state: 'PREPARE', stage: 2, stage_name: 'Preheating bed',
    });
    assert.match(html, /class="stage-line"[^>]*>Preheating bed</);
    assert.doesNotMatch(html, /progress/);
    assert.equal(app.jobSectionHtml({ online: true, gcode_state: 'IDLE' }), '');
});

test('the job section of a running print carries the stage line too', () => {
    const html = app.jobSectionHtml({
        online: true, gcode_state: 'RUNNING', stage: 14, stage_name: 'Cleaning nozzle tip',
        file_name: 'cube', progress: 3, remaining_minutes: 61,
    });
    assert.match(html, /cube/);
    assert.match(html, /Cleaning nozzle tip/);
    assert.match(html, /<progress value="3"/);
});

test('the progress row shows the layer count once the printer reports a total', () => {
    const job = { online: true, gcode_state: 'RUNNING', progress: 12 };
    assert.match(
        app.jobSectionHtml({ ...job, current_layer: 15, total_layers: 99 }),
        /class="progress-layers"[^>]*>15\/99</,
    );
    assert.match(
        app.jobSectionHtml({ ...job, current_layer: 0, total_layers: 99 }),
        /class="progress-layers"[^>]*>0\/99</,
    );
    assert.doesNotMatch(
        app.jobSectionHtml({ ...job, current_layer: 0, total_layers: 0 }),
        /progress-layers/,
    );
});

test('speed shows the level name next to the percentage', () => {
    assert.equal(app.speedText({ print_speed: 100, speed_name: 'Standard' }), 'Standard · 100%');
    assert.equal(app.speedText({ print_speed: 124 }), '124%');
});

test('AMS names come from the unit list, with AMS HT numbered on its own', () => {
    const s = { ams_units: [{ index: 0, name: 'AMS 1', humidity: 35, temp: 28 }] };
    assert.equal(app.amsName(s, 0), 'AMS 1');
    assert.equal(app.amsName({}, 1), 'AMS 2');
    assert.equal(app.amsName({}, 128), 'AMS HT 1');
});

test('the expanded AMS header shows humidity and temperature when known', () => {
    const s = { ams_units: [
        { index: 0, name: 'AMS 1', humidity: 35, temp: 28 },
        { index: 1, name: 'AMS 2', humidity: null, temp: null },
    ] };
    assert.equal(app.amsHeader(s, 0), 'AMS 1 · 35% RH · 28°C');
    assert.equal(app.amsHeader(s, 1), 'AMS 2');
    assert.equal(app.amsHeader({}, 128), 'AMS HT 1');
});

test('the filament tooltip names an AMS HT unit correctly', () => {
    const tip = app._filamentTooltip({ source: 'ams', ams_index: 128, tray_index: 0, filament_type: 'PLA' });
    assert.match(tip, /AMS HT 1 · Slot 1/);
});

test('details list the known low-value fields and skip unknown ones', () => {
    const items = app.detailItems({
        online: true,
        fans: { part: 90, aux: 70, chamber: null, heatbreak: 90 },
        nozzles: [{ side: '', type: 'Hardened steel', diameter: 0.4 }],
        wifi_dbm: -53, door_open: null, sdcard: true, firmware: '01.08.01.00',
    });
    assert.deepEqual(j(items), [
        ['Fans', 'Part 90% · Aux 70% · Heatbreak 90%'],
        ['Nozzle', 'Hardened steel · 0.4 mm'],
        ['Wi-Fi', '-53 dBm'],
        ['SD card', 'Inserted'],
        ['Firmware', '01.08.01.00'],
    ]);
});

test('details name each nozzle of a dual-nozzle printer and the door', () => {
    const items = app.detailItems({
        online: true,
        nozzles: [
            { side: 'left', type: 'HH01', diameter: 0.6 },
            { side: 'right', type: 'HS01', diameter: 0.4 },
        ],
        door_open: true, sdcard: false,
    });
    assert.deepEqual(j(items), [
        ['Nozzles', 'L HH01 0.6 mm · R HS01 0.4 mm'],
        ['Door', 'Open'],
        ['SD card', 'None'],
    ]);
});

test('no details for an offline printer or an older server', () => {
    assert.deepEqual(j(app.detailItems({ online: false, firmware: '1', wifi_dbm: -50 })), []);
    assert.deepEqual(j(app.detailItems({ online: true })), []);
});

test('details html escapes values', () => {
    const html = app.detailsBodyHtml([['Firmware', '<b>']]);
    assert.match(html, /&lt;b&gt;/);
    assert.doesNotMatch(html, /<b>/);
});
