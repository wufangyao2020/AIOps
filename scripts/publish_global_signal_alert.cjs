// Receipts live in successfully created Issues, not in the pre-publication commit.
// No search index dependency. Include closed Issues so closing one cannot re-alert.
const {spawnSync, execFileSync} = require('node:child_process');
const fs = require('node:fs');

function runPolicy(payload) {
  const result = spawnSync('python', ['scripts/global_signal_alert_policy.py'], {
    input: JSON.stringify(payload), encoding: 'utf8', maxBuffer: 4 * 1024 * 1024,
  });
  if (result.status !== 0) throw new Error(`Alert policy failed: ${result.stderr}`);
  return JSON.parse(result.stdout);
}

module.exports = async ({github, context, core}, options = {}) => {
  const alert = options.alert || JSON.parse(fs.readFileSync('data/global_signals/latest_alert.json', 'utf8'));
  if (!alert.notification_snapshot) throw new Error('Missing notification snapshot; refusing legacy publisher');
  const repo = {owner: context.repo.owner, repo: context.repo.repo};
  let published = null;
  // Stream until the most recent successful v1 receipt; do not cap at 100 Issues.
  for await (const page of github.paginate.iterator(github.rest.issues.listForRepo, {
    ...repo, state: 'all', sort: 'created', direction: 'desc', per_page: 100,
  })) {
    for (const issue of page.data) {
      if (issue.pull_request || issue.user?.login !== 'github-actions[bot]') continue;
      const match = (issue.body || '').match(/<!-- global-signal-publication-v1:([A-Za-z0-9+/=]+) -->/);
      if (!match) continue;
      published = JSON.parse(Buffer.from(match[1], 'base64').toString('utf8'));
      break;
    }
    if (published) break;
  }
  const decision = (options.policy || runPolicy)({
    current: alert.notification_snapshot, published, observed: alert.previous_observation,
  });
  core.info(JSON.stringify(decision));
  if (decision.health.new_failures.length || decision.health.recovered.length) {
    core.warning(`数据源健康变化（非产业预警）：${JSON.stringify(decision.health)}`);
  }
  if (decision.unverified_events.length) {
    core.warning(`待核实页面变化：${decision.unverified_events.join(', ')}`);
  }
  await core.summary.addHeading('海外产业信号增量判断')
    .addRaw(JSON.stringify({classification: decision.classification,
      reasons: decision.reasons, health: decision.health,
      unverified_events: decision.unverified_events}, null, 2)).write();
  if (!decision.issue_required) return decision;
  const receipt = Buffer.from(JSON.stringify(decision.publication_snapshot)).toString('base64');
  const reasons = decision.reasons.map(r => `- ${r.signal_id}: ${r.kind} (${r.level})` +
    (r.events || []).map(e => `\n  - ${e.title} — ${e.source_url}`).join('')).join('\n');
  const sha = options.commitSha || execFileSync('git', ['rev-parse', 'HEAD'], {encoding: 'utf8'}).trim();
  const report = `https://github.com/${repo.owner}/${repo.repo}/blob/${sha}/reports/global_signals/latest.md`;
  const auditBody = alert.body.replace('`reports/global_signals/latest.md`', `[完整报告](${report})`);
  const body = `${auditBody}\n\n本次有效增量（其余榜单为背景）：\n${reasons}\n\n` +
    `<!-- global-signal-notification:${decision.notification_key} -->\n` +
    `<!-- global-signal-publication-v1:${receipt} -->`;
  // A failure here leaves the last successful receipt unchanged for next run.
  await github.rest.issues.create({...repo, title: alert.title, body});
  return decision;
};
