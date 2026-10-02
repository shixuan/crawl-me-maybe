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

function groupingApp() {
  const context = vm.createContext({ URL, document: {
    createElement: () => ({ innerHTML: '', className: '', children: [], append(child) { this.children.push(child); } }),
  } });
  vm.runInContext(source + '\nglobalThis.ui = {state, groupedRows, visible, whenOf, card};', context);
  return context.ui;
}

test('groups retain original sources and average all scores under filtering', () => {
  const ui = groupingApp();
  ui.state.rows = [
    {analysis_id: 'a', classification: 'RELEVANT', relevance: 0.9, when: 'over', ends_on: '2026-09-30',
      summary: 'two large', url: 'https://example.com/a', extracted: {}},
    {analysis_id: 'b', classification: 'RELEVANT', relevance: 0.7, when: 'open', ends_on: '2026-10-04',
      summary: 'any two', url: 'https://example.com/b', extracted: {price: {value: '10'}}},
  ];
  ui.state.groups = [{members: ['a', 'b'], overview: 'Same event; conditions differ.'}];
  ui.state.query = 'any two';
  ui.state.hasField = 'price';
  ui.state.whens.add('open');
  const rows = ui.visible();
  assert.equal(rows.length, 1);
  assert.equal(rows[0].members.length, 2);
  assert.equal(rows[0].relevance, 0.8);
  assert.equal(rows[0].ends_on, '');
  const card = ui.card(rows[0]);
  assert.match(card.innerHTML, /avg 0.80/);
  assert.match(card.children[0].innerHTML, /two large/);
  assert.match(card.children[1].innerHTML, /any two/);
  assert.match(card.children[0].className, /result-ended/);
  assert.doesNotMatch(card.children[1].className, /result-ended/);
});

test('unknown dates prevent a group from being classified as over', () => {
  const ui = groupingApp();
  assert.equal(ui.whenOf({members: [{when: 'over'}, {when: 'undated'}]}, ''), 'undated');
  assert.equal(ui.whenOf({members: [{when: 'over'}, {when: 'over'}]}, ''), 'over');
  assert.equal(ui.whenOf({members: [{when: 'over'}, {when: 'open', starts_on: '2099-01-01'}]}, '2026-01-01'), 'later');
});

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
