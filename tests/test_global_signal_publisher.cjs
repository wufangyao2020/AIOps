const {test} = require('node:test');
const assert = require('node:assert/strict');
const publish = require('../scripts/publish_global_signal_alert.cjs');

function snapshot(value = 100, time = '2026-10-14T04:15:00+08:00') {
  return {version: 1, generated_at: time, failed_sources: [], unverified_events: [], signals: {
    a: {level: 'P2', trend: 1, value, hits: [['five_day', 'P2']], events: {}, episode: 'episode1'},
  }};
}
function receipt(s, state = 'closed') {
  return {state, user: {login: 'github-actions[bot]'}, body:
    `<!-- global-signal-publication-v1:${Buffer.from(JSON.stringify(s)).toString('base64')} -->`};
}
function mock(pages, fail = false) {
  const created = [], warnings = [], summaries = [];
  const args = {
    context: {repo: {owner: 'test', repo: 'offline'}},
    github: {rest: {issues: {
      listForRepo() {}, async create(issue) { if (fail) throw Error('simulated API failure'); created.push(issue); },
    }}, paginate: {async *iterator(method, params) {
      assert.equal(params.state, 'all'); for (const page of pages) yield {data: page};
    }}},
    core: {info() {}, warning(s) {warnings.push(s);}, summary: {
      addHeading() {return this;}, addRaw(s) {summaries.push(s); return this;}, async write() {},
    }},
  };
  return {args, created, warnings, summaries};
}
const alert = (s) => ({title: 'offline', body: 'audit', notification_snapshot: s});

test('closed receipt on a later page suppresses minor change; ignore forged human receipt', async () => {
  const fake = receipt(snapshot(999)); fake.user.login = 'human';
  const m = mock([[fake], [receipt(snapshot())]]);
  const d = await publish(m.args, {alert: alert(snapshot(101))});
  assert.equal(d.issue_required, false); assert.equal(m.created.length, 0);
});

test('successful receipt prevents retries, unsuccessful publish leaves no receipt', async () => {
  const old = snapshot(), current = snapshot(110);
  const failed = mock([[receipt(old)]], true);
  await assert.rejects(publish(failed.args, {alert: alert(current)}), /simulated API failure/);
  const retry = mock([[receipt(old)]]);
  await publish(retry.args, {alert: alert(current)});
  assert.equal(retry.created.length, 1);
  const body = retry.created[0].body;
  assert.match(body, /material_numeric_change/);
  const next = mock([[{user: {login: 'github-actions[bot]'}, body}]]);
  await publish(next.args, {alert: alert(current)});
  assert.equal(next.created.length, 0);
});

test('health changes stay in warnings and summary', async () => {
  const current = snapshot(); current.failed_sources = ['sourceX'];
  const m = mock([[receipt(snapshot())]]);
  await publish(m.args, {alert: alert(current)});
  assert.equal(m.created.length, 0); assert.equal(m.warnings.length, 1);
  assert.match(m.summaries[0], /sourceX/);
});

test('legacy Issues cause one safe bootstrap; API read errors prevent unsafe publish', async () => {
  const m = mock([[{user: {login: 'github-actions[bot]'}, body: '<!-- global-signal-fingerprint:legacy -->'}]]);
  await publish(m.args, {alert: alert(snapshot())});
  assert.equal(m.created.length, 1);
  const bad = mock([]);
  bad.args.github.paginate.iterator = async function* () {throw Error('read failure');};
  await assert.rejects(publish(bad.args, {alert: alert(snapshot())}), /read failure/);
  assert.equal(bad.created.length, 0);
});

test('unknown/corrupt baseline fails visibly instead of silently suppressing', async () => {
  const m = mock([[receipt({version: 99})]]);
  await assert.rejects(publish(m.args, {alert: alert(snapshot())}), /Unsupported publication/);
});
