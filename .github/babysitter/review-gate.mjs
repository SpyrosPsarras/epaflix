/**
 * Mandatory review gate for babysitter processes.
 *
 * Canonical copy: ~/.pi/shared/skills/review-gate/review-gate.mjs
 * Vendor it into a repo with that skill's install.sh; do not edit the vendored
 * copy, edit the canonical one and re-install.
 *
 * Why it is vendored into the repo rather than imported from the home
 * directory: the whole `.a5c/` tree is git-ignored, so a helper living there is
 * gone after a fresh clone, and an absolute `~/.pi/...` import works on exactly
 * one machine and never in CI.
 *
 * It deliberately imports nothing. Most repos have no `node_modules` at the
 * root, so a bare `@a5c-ai/babysitter-sdk` import from here would not resolve.
 * Task definitions are passed in by the caller instead, which also makes the
 * loop testable on its own.
 *
 * Usage from `.a5c/processes/<name>.js`:
 *
 *   import { implementWithReview, REVIEW_OUTPUT_SCHEMA, buildReviewInstructions }
 *     from '../../.github/babysitter/review-gate.mjs';
 *
 *   const gate = await implementWithReview(ctx, {
 *     implementTask, verifyTask, reviewTask,
 *     args: { ...cfg, design, approvedPlan },
 *   });
 *   if (!gate.passed) {
 *     return { success: false, stage: 'implementation-review', ...gate };
 *   }
 *
 * The contract:
 *   - implement (agent A) -> verify (real commands) -> review (agent B)
 *   - agent B has a different name AND a different model from agent A
 *   - agent B sees the diff and the captured verification stdout, nothing else
 *   - a failed review feeds `issues` back as `feedback` and re-runs implement
 *   - after `maxAttempts` the run fails; it does not merge and does not open a PR
 */

export const DEFAULT_MAX_ATTEMPTS = 4;

/**
 * Output schema every reviewer task must use. `pass` is the gate; `issues` is
 * what gets fed back to the implementer, so it has to be concrete and
 * actionable rather than a grade.
 */
export const REVIEW_OUTPUT_SCHEMA = {
  type: 'object',
  required: ['pass', 'issues', 'summary'],
  properties: {
    pass: { type: 'boolean' },
    issues: { type: 'array', items: { type: 'string' } },
    summary: { type: 'string' },
  },
};

/**
 * Keys the reviewer must never receive. Passing the implementer's own account
 * of its work turns the review into a reading-comprehension exercise.
 */
const IMPLEMENTER_NARRATIVE_KEYS = new Set([
  'implementation',
  'implementationSummary',
  'summary',
  'notes',
  'narrative',
  'design',
]);

/**
 * Build the instruction block for a reviewer prompt. `artifactsVerbatim` is the
 * diff plus the verification stdout. `specVerbatim` is the issue body or spec,
 * interpolated at run time so a later phase cannot redefine the acceptance
 * standard. `failIf` is the list of concrete, falsifiable failure conditions —
 * without it the reviewer invents its own bar and passes almost anything.
 */
export function buildReviewInstructions({
  artifactsVerbatim,
  specVerbatim,
  approvedPlan,
  failIf = [],
  extra = [],
}) {
  if (!artifactsVerbatim) {
    throw new Error('review-gate: artifactsVerbatim is required — a review without artifacts is not a review');
  }
  if (!failIf.length) {
    throw new Error('review-gate: failIf must list concrete failure conditions');
  }
  const block = (label, body) => (body ? [`${label} (verbatim):`, '---', String(body), '---'] : []);
  return [
    'Judge only the diff and the verification output. Ignore any narrative about how it was built.',
    ...block('ARTIFACTS', artifactsVerbatim),
    ...block('SPEC', specVerbatim),
    ...block('APPROVED PLAN', approvedPlan),
    'Fail the review if any of these hold:',
    ...failIf.map((condition) => `  - ${condition}`),
    ...extra,
    'Return ONLY JSON. Set pass=false with concrete, actionable issues if anything above holds.',
  ];
}

/**
 * Strip implementer narrative from the argument bag handed to the reviewer.
 */
export function reviewerArgs(args, artifactsVerbatim) {
  const clean = {};
  for (const [key, value] of Object.entries(args || {})) {
    if (!IMPLEMENTER_NARRATIVE_KEYS.has(key)) clean[key] = value;
  }
  return { ...clean, artifactsVerbatim };
}

/**
 * Run implement -> verify -> review until the reviewer passes or the attempts
 * run out.
 *
 * @param ctx                 babysitter process context (needs `ctx.task`)
 * @param implementTask       agent task; receives `{ ...args, attempt, feedback }`
 * @param verifyTask          shell task running real commands; must return `{ stdout }`
 * @param reviewTask          agent task using REVIEW_OUTPUT_SCHEMA
 * @param artifactsTask       optional task that bundles diff + verification into `{ stdout }`
 * @param args                shared argument bag
 * @param maxAttempts         default 4
 * @returns { passed, attempts, implementation, verification, artifacts, review, history }
 */
export async function implementWithReview(ctx, {
  implementTask,
  verifyTask,
  reviewTask,
  artifactsTask = null,
  args = {},
  maxAttempts = DEFAULT_MAX_ATTEMPTS,
}) {
  if (!ctx || typeof ctx.task !== 'function') {
    throw new Error('review-gate: ctx.task is required');
  }
  for (const [name, task] of [['implementTask', implementTask], ['verifyTask', verifyTask], ['reviewTask', reviewTask]]) {
    if (!task) throw new Error(`review-gate: ${name} is required — the gate is mandatory, not optional`);
  }

  let feedback = null;
  let implementation;
  let verification;
  let artifacts;
  let review;
  const history = [];

  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    implementation = await ctx.task(implementTask, { ...args, attempt, feedback });

    // Real commands, real stdout. Reviewing the implementer's prose instead of
    // artifacts is the failure mode this whole file exists to prevent.
    verification = await ctx.task(verifyTask, { ...args, attempt });

    artifacts = artifactsTask
      ? await ctx.task(artifactsTask, { ...args, attempt, verificationStdout: verification.stdout })
      : verification;

    review = await ctx.task(reviewTask, reviewerArgs({ ...args, attempt }, artifacts.stdout));

    history.push({ attempt, pass: !!review.pass, issues: review.issues || [], summary: review.summary });
    if (review.pass) {
      return { passed: true, attempts: attempt, implementation, verification, artifacts, review, history };
    }
    feedback = (review.issues || []).join('\n');
  }

  return { passed: false, attempts: maxAttempts, implementation, verification, artifacts, review, history };
}
