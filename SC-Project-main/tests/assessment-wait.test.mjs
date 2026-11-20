import test from 'node:test';
import assert from 'node:assert/strict';
import { waitForAssessment } from '../src/assessment-wait.mjs';

test('a timed-out HTTP request does not hide a successfully completed long assessment', async () => {
  const states = [{status:'running',completed:35,total:118},{status:'completed',completed:118,total:118}];
  const seen = [];
  await waitForAssessment({request:Promise.reject(new Error('The compliance service timed out.')),
    getState: async () => states.shift(), onProgress: value => seen.push(value.completed), sleep: async () => {}});
  assert.deepEqual(seen, [35,118]);
});

test('reports validation errors when the backend never starts the job', async () => {
  await assert.rejects(waitForAssessment({request:Promise.reject(new Error('AI configuration missing')),
    getState:async()=>({status:'pending'}), sleep:async()=>{}}), /AI configuration missing/);
});

test('cancellation remains effective after the long request disconnects', async () => {
  const states = [{status:'running'}, {status:'cancelled'}];
  await assert.rejects(waitForAssessment({request:Promise.reject(new Error('timed out')),
    getState:async()=>states.shift(), sleep:async()=>{}}), /Assessment stopped/);
});

test('failed jobs do not loop forever after a network error', async () => {
  await assert.rejects(waitForAssessment({request:Promise.reject(new Error('Gateway unavailable')),
    getState:async()=>({status:'failed'}), sleep:async()=>{}}), /Gateway unavailable/);
});
