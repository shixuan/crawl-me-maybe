const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const test = require('node:test');

const source = fs.readFileSync(path.join(__dirname, '../../dashboard/app.js'), 'utf8')
  .replace(/boot\(\);\s*$/, '');

function app() {
  const elements = new Map();
  const context = vm.createContext({
    AbortController, TypeError, setTimeout, clearTimeout,
    document: {
      querySelector(selector) {
        if (!elements.has(selector)) elements.set(selector, { innerHTML: '', textContent: '', hidden: false });
        return elements.get(selector);
      },
    },
  });
  vm.runInContext(source + `
    const requests = [];
    api = (path, signal) => new Promise((resolve, reject) => requests.push({ path, signal, resolve, reject }));
    renderRuns = () => {};
    visible = () => [];
    adopt = data => { state.rows = data.rows; renderCards(); };
    globalThis.app = { state, requests, loadRun, renderCards };
  `, context);
  return { ...context.app, elements };
}

const result = name => ({ rows: [name], goals: [], goal_id: name });

test('only the latest run response is adopted', async () => {
  const ui = app();
  const first = ui.loadRun('first');
  const second = ui.loadRun('second');
  assert.equal(ui.requests[0].signal.aborted, true);
  ui.requests[1].resolve(result('second'));
  await second;
  ui.requests[0].resolve(result('first'));
  await first;
  assert.equal(ui.state.run, 'second');
  assert.equal(ui.state.rows[0], 'second');
});

test('filters preserve loading and failure displays', async () => {
  const ui = app();
  ui.state.rows = ['old run'];
  const loading = ui.loadRun('new');
  ui.state.whens.add('open');
  ui.renderCards();
  assert.match(ui.elements.get('#cards').innerHTML, /reading/);
  ui.requests[0].reject(new Error('server offline'));
  await loading;
  ui.state.whens.clear();
  ui.renderCards();
  assert.match(ui.elements.get('#cards').innerHTML, /server offline/);
  assert.equal(ui.state.loading, false);
  const retry = ui.loadRun('new');
  ui.requests[1].resolve(result('new'));
  await retry;
  assert.equal(ui.state.error, '');
  assert.equal(ui.state.rows[0], 'new');
});

test('an older request failure cannot replace the new run', async () => {
  const ui = app();
  const first = ui.loadRun('first');
  const second = ui.loadRun('second');
  ui.requests[1].resolve(result('second'));
  await second;
  ui.requests[0].reject(new Error('old failure'));
  await first;
  assert.equal(ui.state.error, '');
  assert.equal(ui.state.rows[0], 'second');
});

test('request timeout includes response body reads', async () => {
  let expire;
  let cleared = false;
  let reading;
  const bodyStarted = new Promise(resolve => { reading = resolve; });
  const context = vm.createContext({
    AbortController, TypeError,
    setTimeout(callback) { expire = callback; return 1; },
    clearTimeout() { cleared = true; },
    fetch: async (url, { signal }) => ({
      ok: true,
      json: () => new Promise((resolve, reject) => {
        signal.addEventListener('abort', () => reject(new Error('aborted')));
        reading();
      }),
    }),
  });
  vm.runInContext(source, context);
  const request = vm.runInContext('api("/api/run/test")', context);
  await bodyStarted;
  expire();
  await assert.rejects(request, /request timed out/);
  assert.equal(cleared, true);
});
