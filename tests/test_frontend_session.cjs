// Run with: node --test tests/test_frontend_session.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../src/tokentracker/static/index.html'), 'utf8');
const flush = () => new Promise(resolve => setImmediate(resolve));

test('Codex private figures do not survive logout or appear under a new account', async () => {
  const nodes = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(([, id]) => [id, {
    textContent: '', innerHTML: '', value: '', listeners: {},
    classList: { add() {}, remove() {}, toggle() {}, contains() { return false; } },
    addEventListener(event, handler) { this.listeners[event] = handler; },
    setAttribute() {}, reset() {}, focus() {},
  }]));
  const listeners = {};
  const timers = new Map();
  let timerId = 0;
  let account = 'alice';
  const pending = [];
  const aliceUsage = {
    total_requests: 2, total_tokens: 900, total_cost_usd: 12, unpriced_requests: 1,
    by_model: { 'alice-private-model': { cost_usd: 12, total_tokens: 900, requests: 2, unpriced_requests: 1 } },
  };
  const context = {
    Intl, URLSearchParams, performance,
    location: { hash: '#/codex/me', origin: 'https://example.test' },
    document: { hidden: false, getElementById: id => nodes.get(id), querySelectorAll: () => [], addEventListener() {} },
    window: { addEventListener: (event, handler) => { listeners[event] = handler; } },
    setInterval(handler) { timers.set(++timerId, handler); return timerId; },
    clearInterval: id => timers.delete(id), setTimeout() {},
    requestAnimationFrame() {}, cancelAnimationFrame() {},
    fetch: async url => {
      let data = {};
      if (url === '/me' || url === '/login') data = { user_id: account === 'alice' ? 1 : 2, username: account };
      if (url === '/usage/me/codex') {
        if (nodes.get('codex-me-cost').textContent === '$12.00' || account === 'bob') {
          return new Promise((resolve, reject) => pending.push({ resolve, reject }));
        }
        data = aliceUsage;
      }
      return { ok: true, json: async () => data };
    },
  };
  vm.runInNewContext(html.split('<script>')[1].split('</script>')[0], context);
  await flush();
  assert.equal(nodes.get('codex-me-cost').textContent, '$12.00');
  assert.match(nodes.get('codex-me-models').innerHTML, /alice-private-model/);
  // Leave an Alice request in flight to exercise the existing route-version guard.
  for (const handler of timers.values()) handler();
  await flush();
  assert.equal(pending.length, 1);
  context.location.hash = '#logout';
  await listeners.hashchange();
  assert.equal(nodes.get('codex-me-cost').textContent, '—');
  assert.equal(nodes.get('codex-me-handle').textContent, '');
  assert.equal(nodes.get('codex-me-models').innerHTML, '');
  assert.equal(nodes.get('codex-me-unpriced').textContent, '');

  account = 'bob';
  context.location.hash = '#/login?next=codex';
  await listeners.hashchange();
  await nodes.get('form-login').listeners.submit({ preventDefault() {} });
  await listeners.hashchange();
  await flush();
  assert.equal(nodes.get('codex-me-handle').textContent, 'bob');
  assert.equal(nodes.get('codex-me-cost').textContent, '—');
  assert.equal(nodes.get('codex-me-tokens').textContent, '—');
  assert.equal(nodes.get('codex-me-requests').textContent, '—');
  assert.equal(nodes.get('codex-me-models').innerHTML, '');
  assert.match(nodes.get('codex-me-status').textContent, /loading/);

  pending[0].resolve({ ok: true, json: async () => aliceUsage });
  await flush();
  assert.equal(nodes.get('codex-me-cost').textContent, '—');
  pending[1].reject(new Error('offline'));
  await flush();
  assert.equal(nodes.get('codex-me-cost').textContent, '—');
  assert.equal(nodes.get('codex-me-models').innerHTML, '');
  assert.equal(nodes.get('codex-me-unpriced').textContent, '');
  assert.match(nodes.get('codex-me-status').textContent, /Retrying/);
});
