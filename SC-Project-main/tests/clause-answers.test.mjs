import test from 'node:test';
import assert from 'node:assert/strict';
import { checklistPrompts, unpackAnswers, packAnswers, toAuditNotes } from '../src/clause-answers.mjs';

test('clause notes retain all previous answers while removing old question headings', () => {
  const original = {'4.1':{status:'partial',text:'Context register v3.\n\n[Q1] Who owns the register?\nISMS mgr.\n\n[Q2] Latest review?\nReviewed Oct; vendor risk open.'}};
  const notes = toAuditNotes(original);
  assert.equal(notes['4.1'].text, 'Context register v3.\n\nISMS mgr.\n\nReviewed Oct; vendor risk open.');
  assert.equal(notes['4.1'].status, 'partial');
  assert.ok(original['4.1'].text.includes('[Q1]'));
  assert.deepEqual(toAuditNotes(notes), notes);
});

test('splits numbered checklist questions using the original workflow rules', () => {
  assert.deepEqual(checklistPrompts('Context\n1. External factors?\nInclude risks.\n2. Internal factors?'), ['Context External factors? Include risks.', 'Internal factors?']);
  assert.deepEqual(checklistPrompts('First? || Second?'), ['First?', 'Second?']);
});

test('keeps each answer attached to its question, including unanswered questions', () => {
  const prompts = ['First?', 'Second?', 'Added question?'];
  const text = packAnswers('Original audit notes', { 0: 'First answer', 2: 'Additional answer\nMore detail' }, prompts);
  assert.deepEqual(unpackAnswers(text, prompts), { previous: 'Original audit notes', values: { 0: 'First answer', 2: 'Additional answer\nMore detail' } });
  const updated = packAnswers('Original audit notes', { ...unpackAnswers(text, prompts).values, 1: 'Second answer' }, prompts);
  assert.equal(unpackAnswers(updated, prompts).values[0], 'First answer');
  assert.equal(unpackAnswers(updated, prompts).values[2], 'Additional answer\nMore detail');
});

test('preserves unstructured imported notes without guessing which question they answer', () => {
  const text = 'Imported SiamCloud observations\nDocument reference: approved policy';
  assert.deepEqual(unpackAnswers(text, ['Question one?', 'Question two?']), { previous: text, values: {} });
  assert.ok(packAnswers(text, { 1: 'New answer' }, ['Question one?', 'Question two?']).startsWith(text));
});

test('reads older named additional answers and retains unknown notes', () => {
  const text = 'คำถามเพิ่มเติม: Extra?\nคำตอบ: Existing answer';
  assert.deepEqual(unpackAnswers(text, ['Main?', 'Extra?']), { previous: '', values: { 1: 'Existing answer' } });
  assert.equal(unpackAnswers('[Q8] Missing question\nDo not discard', ['First?']).previous, '[Q8] Missing question\nDo not discard');
});
