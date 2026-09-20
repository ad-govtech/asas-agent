import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL('../examples/recruiting/static/index.html', import.meta.url), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1].replace(/\nstart\(\);/, '');
const payload = '<img src=x onerror="window.reviewMarker=1">';

function page() {
  const nodes = {};
  const sandbox = { document: { getElementById: id => nodes[id] ??= { innerHTML: '', value: '' } }, payload };
  vm.createContext(sandbox);
  vm.runInContext(script, sandbox);
  return { nodes, sandbox };
}

for (const expression of [
  'payload',
  '({ fit_summary: payload, evidence: [payload] })',
  '({ recommendation: payload })',
  '({ [payload]: payload })',
]) {
  test(`model output is escaped: ${expression}`, () => {
    const { nodes, sandbox } = page();
    vm.runInContext(`renderAnswer(${expression})`, sandbox);
    assert.ok(!nodes.answer.innerHTML.includes('<img'));
    assert.ok(nodes.answer.innerHTML.includes('&lt;img'));
    assert.ok(nodes.answer.innerHTML.includes('&quot;'));
  });
}

test('model names and tool metadata are escaped', () => {
  const { nodes, sandbox } = page();
  vm.runInContext('renderRan({agent_version: 1, prompt_name: payload, model: payload, tools_allowed: [payload], max_turns: 1, latency_ms: 1})', sandbox);
  assert.ok(!nodes.ran.innerHTML.includes('<img'));
  assert.ok(nodes.ran.innerHTML.includes('&lt;img'));
});

test('request errors are escaped', async () => {
  const { nodes, sandbox } = page();
  // Failing before a request is sent also exercises the actual UI error handler.
  sandbox.fetch = async () => { throw new Error(payload); };
  vm.runInContext('selectedTools = () => []', sandbox);
  await vm.runInContext('run()', sandbox);
  assert.ok(!nodes.banner.innerHTML.includes('<img'));
  assert.ok(nodes.banner.innerHTML.includes('&lt;img'));
});
