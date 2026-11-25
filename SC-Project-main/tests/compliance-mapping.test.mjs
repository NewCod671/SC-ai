import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import net from 'node:net';
import { spawn } from 'node:child_process';
import { mkdtemp, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
async function unusedPort() {
  const server = net.createServer();
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const port = server.address().port;
  await new Promise(resolve => server.close(resolve));
  return port;
}

test('mapping bridge requires sign-in and the linked assessment, forwards credentials and never saves proposals', { timeout: 30000 }, async t => {
  const data = await mkdtemp(path.join(os.tmpdir(), 'sc-mapping-'));
  const calls = [];
  const upstream = http.createServer(async (req, res) => {
    let text = ''; for await (const chunk of req) text += chunk;
    calls.push({ method: req.method, url: req.url, authorization: req.headers.authorization, body: text ? JSON.parse(text) : null });
    res.setHeader('Content-Type', 'application/json');
    if (req.url === '/api/assessments') { res.statusCode = 201; res.end(JSON.stringify({ id: 'linked-assessment', current_version: 1 })); }
    else if (req.url === '/api/assessments/linked-assessment/map-answers') res.end(JSON.stringify({ provider: 'fixture', current_version: 1, requires_review: true, mappings: [{ control_id: 'SC-A', answer_text: 'MFA enabled', suggested_status: 'yes', source_texts: ['MFA enabled'], confidence: .9, reason: 'Access control' }], unmatched: [{ source_text: 'unclear', reason: 'Needs context' }] }));
    else if (req.url === '/api/assessments/linked-assessment/questions/direct') { res.statusCode = 201; res.end(JSON.stringify({ id: 'linked-assessment', current_version: 2, questions: [{ control_ids: ['4.1'], text: 'Who reviews context?' }] })); }
    else if (req.url === '/api/assessments/linked-assessment/runs/start') { res.statusCode = 202; res.end(JSON.stringify({ job_id: 'fixture-job' })); }
    else if (req.url === '/api/assessment-run-jobs/fixture-job?assessment_id=linked-assessment') { res.end(JSON.stringify({ status: 'completed' })); }
    else if (req.url === '/api/assessment-run-jobs/fixture-job/cancel?assessment_id=linked-assessment') { res.end(JSON.stringify({ status: 'cancelled' })); }
    else { res.statusCode = 500; res.end('{}'); }
  });
  await new Promise(resolve => upstream.listen(0, '127.0.0.1', resolve));
  const port = await unusedPort(), base = `http://127.0.0.1:${port}`;
  const child = spawn(process.execPath, ['server.js'], { cwd: root, env: { ...process.env, NODE_ENV: 'production', SC_WEB_HOST: '127.0.0.1', PORT: String(port), SC_DATA_DIR: data, COMPLIANCE_API_URL: `http://127.0.0.1:${upstream.address().port}`, COMPLIANCE_API_KEY: 'test-bridge-key', SC_EXPERIMENTAL_ADMIN_USERNAME: 'testadmin', SC_EXPERIMENTAL_ADMIN_PASSWORD: 'Test-admin-123!' }, stdio: ['ignore','pipe','pipe'] });
  let startupLog = '';
  for (const stream of [child.stdout, child.stderr]) {
    stream.on('data', chunk => { startupLog = (startupLog + chunk).slice(-4000); });
  }
  t.after(async () => {
    if (child.exitCode === null) { child.kill(); await new Promise(resolve => child.once('exit', resolve)); }
    await new Promise(resolve => upstream.close(resolve));
    await rm(data, { recursive: true, force: true });
  });
  let ready = false;
  for (let i = 0; i < 80; i++) {
    if (child.exitCode !== null) throw Error(`Test web server exited: ${startupLog}`);
    try { if ((await fetch(base + '/api/health')).ok) { ready = true; break; } } catch {}
    await sleep(100);
  }
  assert.ok(ready, `web server started: ${startupLog}`);
  const login = await fetch(base + '/api/auth/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ username: 'testadmin', password: 'Test-admin-123!' }) });
  assert.equal(login.status, 200);
  const cookie = login.headers.get('set-cookie').split(';')[0];
  const post = (route, body, signedIn = true) => fetch(base + route, { method: 'POST', headers: { 'Content-Type': 'application/json', ...(signedIn ? { Cookie: cookie } : {}) }, body: JSON.stringify(body) });
  const projectResponse = await post('/api/projects', { name: 'Mapping test', client: 'Synthetic client' });
  assert.equal(projectResponse.status, 201);
  const { project } = await projectResponse.json();
  const route = `/api/compliance/projects/${project.id}/assessment`;
  assert.equal((await post(route, { standard_id: 'standard-1', context: {} })).status, 201);
  const mappingRoute = route + '/linked-assessment/map-answers';
  const before = calls.length;
  assert.equal((await post(mappingRoute, { text: 'MFA enabled' }, false)).status, 401);
  assert.equal((await post(route + '/another-assessment/map-answers', { text: 'MFA enabled' })).status, 404);
  assert.equal(calls.length, before, 'rejected requests never reach the backend');
  const response = await post(mappingRoute, { text: 'MFA enabled\nunclear' });
  assert.equal(response.status, 200);
  const preview = await response.json();
  assert.equal(preview.mappings[0].control_id, 'SC-A');
  assert.equal(preview.unmatched.length, 1);
  assert.equal(preview.requires_review, true);
  assert.deepEqual(calls.at(-1), { method: 'POST', url: '/api/assessments/linked-assessment/map-answers', authorization: 'Bearer test-bridge-key', body: { text: 'MFA enabled\nunclear' } });
  assert.equal(calls.length, before + 1, 'preview makes no answer-save request');
  assert.ok(!JSON.stringify(preview).includes('test-bridge-key'), 'server key is not returned');
  const question = { expected_version: 1, control_id: '4.1', text: 'Who reviews context?', answer_text: '' };
  assert.equal((await post(route + '/another-assessment/questions/direct', question)).status, 404);
  const added = await post(route + '/linked-assessment/questions/direct', question);
  assert.equal(added.status, 201);
  assert.deepEqual((await added.json()).questions[0].control_ids, ['4.1']);
  assert.deepEqual(calls.at(-1).body, question);
  const start = await post(route + '/linked-assessment/runs', { job_id: 'fixture-job', expected_version: 2, authorize_external: true });
  assert.equal(start.status, 202);
  const jobRoute = `/api/compliance/projects/${project.id}/jobs/fixture-job`;
  const progress = await fetch(base + jobRoute + '?assessment_id=other', { headers: { Cookie: cookie } });
  assert.equal(progress.status, 200);
  assert.equal((await progress.json()).status, 'completed');
  const cancelled = await post(jobRoute + '/cancel', {});
  assert.equal(cancelled.status, 200);
  assert.equal((await cancelled.json()).status, 'cancelled');
});
