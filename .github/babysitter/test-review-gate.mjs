/**
 * Fixture suite for the shared review gate. No dependencies, no network.
 *
 *   node --test .github/babysitter/
 *
 * These assert the properties the gate exists to guarantee, not just the happy
 * path: that a failed review actually re-runs the implementer with the issues
 * fed back, that the run fails after the attempt budget instead of passing, and
 * that the reviewer never receives the implementer's own narrative.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  implementWithReview,
  buildReviewInstructions,
  reviewerArgs,
  REVIEW_OUTPUT_SCHEMA,
  DEFAULT_MAX_ATTEMPTS,
} from './review-gate.mjs';

/** Minimal stand-in for the babysitter process context. */
function makeCtx(handlers) {
  const calls = [];
  return {
    calls,
    async task(task, args) {
      calls.push({ task, args });
      const handler = handlers[task];
      if (!handler) throw new Error(`no handler for task ${task}`);
      return handler(args, calls.filter((c) => c.task === task).length);
    },
  };
}

const baseTasks = { implementTask: 'implement', verifyTask: 'verify', reviewTask: 'review' };

test('passes on the first attempt and does not re-run the implementer', async () => {
  const ctx = makeCtx({
    implement: () => ({ branch: 'feat/x' }),
    verify: () => ({ stdout: 'ALL CHECKS PASSED' }),
    review: () => ({ pass: true, issues: [], summary: 'fine' }),
  });

  const result = await implementWithReview(ctx, { ...baseTasks, args: { repo: 'r' } });

  assert.equal(result.passed, true);
  assert.equal(result.attempts, 1);
  assert.equal(ctx.calls.filter((c) => c.task === 'implement').length, 1);
});

test('feeds reviewer issues back into the next implement attempt', async () => {
  const ctx = makeCtx({
    implement: () => ({ branch: 'feat/x' }),
    verify: () => ({ stdout: 'output' }),
    review: (_args, nth) =>
      nth === 1
        ? { pass: false, issues: ['fixture only asserts the happy path', 'no rollback note'], summary: 'no' }
        : { pass: true, issues: [], summary: 'fixed' },
  });

  const result = await implementWithReview(ctx, { ...baseTasks, args: {} });

  assert.equal(result.passed, true);
  assert.equal(result.attempts, 2);

  const implementCalls = ctx.calls.filter((c) => c.task === 'implement');
  assert.equal(implementCalls[0].args.feedback, null);
  assert.equal(implementCalls[0].args.attempt, 1);
  assert.equal(
    implementCalls[1].args.feedback,
    'fixture only asserts the happy path\nno rollback note',
  );
  assert.equal(implementCalls[1].args.attempt, 2);
});

test('fails the run after the attempt budget instead of passing', async () => {
  const ctx = makeCtx({
    implement: () => ({ branch: 'feat/x' }),
    verify: () => ({ stdout: 'output' }),
    review: () => ({ pass: false, issues: ['still wrong'], summary: 'no' }),
  });

  const result = await implementWithReview(ctx, { ...baseTasks, args: {}, maxAttempts: 3 });

  assert.equal(result.passed, false);
  assert.equal(result.attempts, 3);
  assert.equal(result.history.length, 3);
  assert.equal(ctx.calls.filter((c) => c.task === 'implement').length, 3);
});

test('default attempt budget is 4', async () => {
  const ctx = makeCtx({
    implement: () => ({}),
    verify: () => ({ stdout: 'o' }),
    review: () => ({ pass: false, issues: ['no'], summary: 'no' }),
  });

  const result = await implementWithReview(ctx, { ...baseTasks, args: {} });

  assert.equal(result.attempts, DEFAULT_MAX_ATTEMPTS);
  assert.equal(DEFAULT_MAX_ATTEMPTS, 4);
});

test('reviewer receives verification stdout, not the implementer narrative', async () => {
  let seen = null;
  const ctx = makeCtx({
    implement: () => ({ summary: 'I did a great job and everything is correct' }),
    verify: () => ({ stdout: 'DIFF + REAL COMMAND OUTPUT' }),
    review: (args) => {
      seen = args;
      return { pass: true, issues: [], summary: 'ok' };
    },
  });

  await implementWithReview(ctx, {
    ...baseTasks,
    args: { repo: 'r', implementation: { summary: 'trust me' }, design: { approach: 'mine' } },
  });

  assert.equal(seen.artifactsVerbatim, 'DIFF + REAL COMMAND OUTPUT');
  assert.equal(seen.implementation, undefined);
  assert.equal(seen.design, undefined);
  assert.equal(seen.repo, 'r');
});

test('artifactsTask output replaces raw verification output when supplied', async () => {
  let seen = null;
  const ctx = makeCtx({
    implement: () => ({}),
    verify: () => ({ stdout: 'raw verify' }),
    bundle: (args) => ({ stdout: `diff + ${args.verificationStdout}` }),
    review: (args) => {
      seen = args;
      return { pass: true, issues: [], summary: 'ok' };
    },
  });

  await implementWithReview(ctx, { ...baseTasks, artifactsTask: 'bundle', args: {} });

  assert.equal(seen.artifactsVerbatim, 'diff + raw verify');
});

test('the gate cannot be made optional by omitting a task', async () => {
  const ctx = makeCtx({});
  await assert.rejects(
    () => implementWithReview(ctx, { implementTask: 'implement', verifyTask: 'verify', args: {} }),
    /reviewTask is required/,
  );
  await assert.rejects(
    () => implementWithReview(ctx, { implementTask: 'implement', reviewTask: 'review', args: {} }),
    /verifyTask is required/,
  );
});

test('a review prompt without artifacts or failure conditions is rejected', () => {
  assert.throws(
    () => buildReviewInstructions({ failIf: ['x'] }),
    /artifactsVerbatim is required/,
  );
  assert.throws(
    () => buildReviewInstructions({ artifactsVerbatim: 'diff' }),
    /failIf must list concrete failure conditions/,
  );
});

test('review instructions interpolate spec and plan verbatim', () => {
  const lines = buildReviewInstructions({
    artifactsVerbatim: 'THE DIFF',
    specVerbatim: 'THE ISSUE BODY',
    approvedPlan: 'THE PLAN',
    failIf: ['the guard got weaker'],
  });
  const joined = lines.join('\n');

  assert.match(joined, /Judge only the diff and the verification output/);
  assert.match(joined, /ARTIFACTS \(verbatim\):\n---\nTHE DIFF\n---/);
  assert.match(joined, /SPEC \(verbatim\):\n---\nTHE ISSUE BODY\n---/);
  assert.match(joined, /APPROVED PLAN \(verbatim\):\n---\nTHE PLAN\n---/);
  assert.match(joined, /  - the guard got weaker/);
  assert.match(joined, /Return ONLY JSON/);
});

test('reviewerArgs drops narrative keys and keeps the rest', () => {
  const clean = reviewerArgs(
    { repo: 'r', issue: 800, notes: 'n', narrative: 'x', implementationSummary: 's' },
    'ARTIFACTS',
  );
  assert.deepEqual(clean, { repo: 'r', issue: 800, artifactsVerbatim: 'ARTIFACTS' });
});

test('review output schema demands a boolean pass and actionable issues', () => {
  assert.deepEqual(REVIEW_OUTPUT_SCHEMA.required, ['pass', 'issues', 'summary']);
  assert.equal(REVIEW_OUTPUT_SCHEMA.properties.pass.type, 'boolean');
  assert.equal(REVIEW_OUTPUT_SCHEMA.properties.issues.items.type, 'string');
});
