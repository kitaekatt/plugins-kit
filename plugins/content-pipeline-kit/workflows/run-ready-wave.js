// run-ready-wave -- native Workflow script (C1, thin recipient).
//
// Runs a prepared wave of content-pipeline units through the workflow-kit
// agent(), N lanes wide. Each agent claims its own unit, reads the prepared
// request, writes an answer, and submits it -- entirely through the
// consumer's protocol mount (claim / read / submit / fail commands supplied
// in each unit's pack). This script performs no persistence, no file I/O,
// and no state mutation: every mutation crosses the protocol seam inside an
// agent. See skills/content-pipeline-domain/references/workflow-lane.md for
// the full specification this file implements.
//
// Args (all computed in Python by the invoking session's pack builder):
//   runId          -- string, the content-pipeline run id
//   batchId        -- string, Python-minted; the only identity source (no
//                     Date.now/Math.random/crypto.randomUUID in this file --
//                     those break Workflow resume)
//   maxAgents      -- positive integer, caller's concurrency ceiling
//   units          -- non-empty array of packs, ordinal-sorted, each:
//                     { unitId, ordinal, workerId, claimCmd, readCmd,
//                       submitCmd, failCmd, answerPath, writeSubmitPath,
//                       writeFailPath, submitTemplate, failTemplate }
//
// Not run by this file: prepare, reap, status, finalize, pause/resume --
// those belong to the invoking top-level session through the same protocol
// mount, before/after this Workflow call.

export const meta = {
  name: 'run-ready-wave',
  description: 'Run one prepared content-pipeline wave through N parallel agent lanes; each agent claims, reads, answers, and submits its own unit through the protocol seam.',
  phases: [{ title: 'Execute' }],
}

const UNIT_RESULT_SCHEMA = {
  type: "object", additionalProperties: false,
  required: ["outcome", "attempts", "error_code"],
  properties: {
    outcome:    { enum: ["accepted", "failed", "halted", "claim_unavailable"] },
    attempts:   { type: "integer", minimum: 0, maximum: 8 },
    error_code: { enum: ["none", "run_halted", "already_claimed", "terminal_state",
                         "stale_fence", "env_mismatch", "rejected_exhausted", "other"] },
  },
};

const inputs = typeof args === "string" ? JSON.parse(args) : (args || {});

// --------------------------------------------------------------------------- //
// Validation guard -- throws, never returns a silent zero-count aggregate.
// --------------------------------------------------------------------------- //
function fail(msg) { throw new Error("run-ready-wave: " + msg); }
if (typeof inputs.runId !== "string" || !inputs.runId) fail("runId required");
if (typeof inputs.batchId !== "string" || !inputs.batchId) fail("batchId required");
if (!Number.isInteger(inputs.maxAgents) || inputs.maxAgents < 1) fail("maxAgents must be a positive integer");
if (!Array.isArray(inputs.units) || inputs.units.length === 0) fail("units must be a non-empty array");
if (inputs.units.length > 1000) fail("wave exceeds the 1000-agent workflow limit (P7)");

// Per-pack field checks, unit-id uniqueness, and strictly increasing ordinals
// across the (already wave-position-sorted) array.
const PACK_STRING_FIELDS = [
  "unitId", "workerId", "claimCmd", "readCmd", "submitCmd", "failCmd",
  "answerPath", "writeSubmitPath", "writeFailPath", "submitTemplate", "failTemplate",
];
const seenUnitIds = new Set();
let prevOrdinal = -Infinity;
for (let i = 0; i < inputs.units.length; i++) {
  const u = inputs.units[i];
  if (typeof u !== "object" || u === null) fail("units[" + i + "] must be an object");
  for (let f = 0; f < PACK_STRING_FIELDS.length; f++) {
    const key = PACK_STRING_FIELDS[f];
    if (typeof u[key] !== "string" || !u[key]) fail("units[" + i + "]." + key + " must be a non-empty string");
  }
  if (!Number.isInteger(u.ordinal) || u.ordinal < 0) fail("units[" + i + "].ordinal must be a non-negative integer");
  if (seenUnitIds.has(u.unitId)) fail("duplicate unitId: " + u.unitId);
  seenUnitIds.add(u.unitId);
  if (u.ordinal <= prevOrdinal) fail("units must be strictly increasing by ordinal (wave position " + i + ")");
  prevOrdinal = u.ordinal;
}

// --------------------------------------------------------------------------- //
// Identity attachment -- unit_id / ordinal / worker_id ALWAYS come from the
// pack, never from the agent's returned object, even when the agent supplies
// its own.
// --------------------------------------------------------------------------- //
function identityFields(u) {
  return { unit_id: u.unitId, ordinal: u.ordinal, worker_id: u.workerId };
}

// The three schema fields are PICKED off the agent's reply, never spread.
// The aggregate is this script's own artifact, so the bound "no unit content
// reaches a workflow variable or the return value" is enforced HERE rather
// than trusted to the runtime stripping properties outside the schema --
// the same trust this file already withholds for the identity fields. Values
// are checked against the schema's OWN enums (read off UNIT_RESULT_SCHEMA,
// so there is no second copy to drift), and a nullish, non-object or
// out-of-enum reply is not a result at all: it returns null, and the lane
// records an agent_error for that unit instead.
const OUTCOME_VALUES = UNIT_RESULT_SCHEMA.properties.outcome.enum;
const ERROR_CODE_VALUES = UNIT_RESULT_SCHEMA.properties.error_code.enum;
const ATTEMPTS_MAX = UNIT_RESULT_SCHEMA.properties.attempts.maximum;

function resultFrom(u, r) {
  if (typeof r !== "object" || r === null) return null;
  if (OUTCOME_VALUES.indexOf(r.outcome) < 0) return null;
  if (ERROR_CODE_VALUES.indexOf(r.error_code) < 0) return null;
  const attempts =
    Number.isInteger(r.attempts) && r.attempts >= 0 ? Math.min(r.attempts, ATTEMPTS_MAX) : 0;
  return Object.assign(identityFields(u), {
    outcome: r.outcome,
    attempts: attempts,
    error_code: r.error_code,
  });
}

// agent_error / not_attempted are script-generated outcomes only -- they are
// deliberately absent from UNIT_RESULT_SCHEMA's enum.
function scriptResult(u, outcome) {
  const errorCode = outcome === "not_attempted" ? "none" : "other";
  return Object.assign({}, identityFields(u), { outcome: outcome, attempts: 0, error_code: errorCode });
}

// --------------------------------------------------------------------------- //
// Agent brief -- pure string concatenation over one pack's fields. Every step
// is an EXACT invocation to run verbatim or an EXACT Write target -- never an
// outcome to achieve, and never a redirect/pipe/substitute construct composed
// by the agent (P5 stall risk; workflow agents run at a fixed permission mode
// with no auto escape and nobody to answer a prompt).
// --------------------------------------------------------------------------- //
function briefFor(u) {
  return (
    "Run id: " + inputs.runId + "\n" +
    "Unit id: " + u.unitId + "\n" +
    "Worker id: " + u.workerId + "\n" +
    "Answer path: " + u.answerPath + "\n" +
    "\n" +
    "This unit is not yet claimed. You claim it yourself in step 2 below; the " +
    "fencing token in that reply is your only authority to submit it -- there " +
    "is no other source for that value.\n" +
    "\n" +
    "Perform exactly these invocations, in this order, and no others. Do not " +
    "compose a redirect, pipe, or any other shell construct to satisfy any " +
    "step below -- run each invocation exactly as written.\n" +
    "\n" +
    "1. You have your identity above: run id, unit id, worker id, answer " +
    "path. No fencing token exists yet and no unit content is available yet.\n" +
    "2. Claim the unit:\n   " + u.claimCmd + "\n" +
    "   Read the reply. If it is {\"ok\": true, ...}, take the fencing token " +
    "from it and continue to step 3. If it is any {\"ok\": false, ...} reply, " +
    "map its error to an outcome and return immediately without running the " +
    "claim invocation again:\n" +
    "     run_halted        -> outcome halted, error_code run_halted\n" +
    "     already_claimed    -> outcome claim_unavailable, error_code already_claimed\n" +
    "     terminal_state     -> outcome claim_unavailable, error_code terminal_state\n" +
    "     env_mismatch       -> outcome claim_unavailable, error_code env_mismatch\n" +
    "     anything else      -> outcome claim_unavailable, error_code other\n" +
    "3. Read the prepared request:\n   " + u.readCmd + "\n" +
    "   Only this step returns unit content. That content must never appear " +
    "anywhere in the object you return at the end.\n" +
    "4. Produce your answer text and write it, verbatim, with the Write " +
    "tool, to exactly this path (no other path):\n   " + u.answerPath + "\n" +
    "   The FIRST line of that file must be exactly:\n" +
    "     content-pipeline-fence: <the fencing token from step 2>\n" +
    "   Your answer text follows on the next line, verbatim and unaltered.\n" +
    "5. Author your submission envelope: with the Write tool, write EXACTLY " +
    "the template below, substituting ONLY the literal token <FENCING_TOKEN> " +
    "with the fencing token from step 2, to exactly this path (no other " +
    "path):\n   " + u.writeSubmitPath + "\n" +
    "   Template:\n" + u.submitTemplate + "\n" +
    "6. Submit your answer:\n   " + u.submitCmd + "\n" +
    "   If accepted, return outcome accepted, error_code none, and the " +
    "number of submit attempts you made.\n" +
    "   If the submission is rejected WITH feedback, revise the answer file " +
    "(step 4, fence line included) and repeat this step -- the submission " +
    "envelope from step 5 does not change and must not be rewritten. You may " +
    "repeat this step at most 4 more times (5 submit attempts total). If " +
    "every attempt is rejected, go to step 7 with error_code " +
    "rejected_exhausted.\n" +
    "   If the reply reports a stale or superseded fence, return immediately " +
    "with outcome claim_unavailable, error_code stale_fence -- do not retry " +
    "and do not re-claim.\n" +
    "7. If you cannot complete the unit (submit exhaustion, or any failure " +
    "not covered above), author your failure envelope the same way as step " +
    "5 -- write EXACTLY the template below, substituting ONLY " +
    "<FENCING_TOKEN>, to exactly this path (no other path):\n   " +
    u.writeFailPath + "\n" +
    "   Template:\n" + u.failTemplate + "\n" +
    "   Then report failure:\n   " + u.failCmd + "\n" +
    "   Return outcome failed with the error_code that best matches what " +
    "went wrong (rejected_exhausted for submit exhaustion, other otherwise). " +
    "Never fabricate a result -- if you did not actually run the fail " +
    "invocation, do not report failed.\n" +
    "8. Run only the exact invocations given above; never compose a " +
    "redirect, pipe, or substitute shell construct to satisfy any step.\n" +
    "9. Return exactly one JSON object matching your schema -- outcome, " +
    "attempts, error_code -- and nothing else. Do not include unit_id, " +
    "ordinal, worker_id, or any unit content; those are attached by the " +
    "caller.\n"
  );
}

// --------------------------------------------------------------------------- //
// Lane loop -- exactly one agent() call site, exactly one parallel() call.
// The await inside the lane body is load-bearing: without it a lane fires
// all its agents at once and the concurrency bound N is a fiction.
// --------------------------------------------------------------------------- //
const N = Math.min(inputs.maxAgents, 16, inputs.units.length);
// Each lane entry carries its OWN wave position, so the reassembly below
// places results instead of recomputing a stride. Exactly one entry is
// pushed per unit on every path -- the push is the last statement that can
// run for that unit in the `try`, and nothing after it can throw.
async function runLane(k) {
  const results = [];
  let halted = false;
  for (let i = k; i < inputs.units.length; i += N) {
    const u = inputs.units[i];
    if (halted) { results.push({ position: i, result: scriptResult(u, "not_attempted") }); continue; }
    let entry;
    try {
      const r = await agent(briefFor(u), { schema: UNIT_RESULT_SCHEMA, label: "unit " + u.ordinal, phase: "Execute" });
      // identity from the pack, never the agent; fields picked, never spread
      entry = resultFrom(u, r);
      if (entry === null) entry = scriptResult(u, "agent_error");
    } catch (e) {
      entry = scriptResult(u, "agent_error");
    }
    results.push({ position: i, result: entry });
    if (entry.outcome === "halted") halted = true;
  }
  return results;
}
const lanes = [];
for (let k = 0; k < N; k++) lanes.push(() => runLane(k));
const laneResults = await parallel(lanes);

// Reconstruct wave-position order from the per-lane arrays by reading each
// entry's EXPLICIT position -- never by recomputing the loop's stride, which
// silently shifts every later entry (and extends the array past
// units.length, leaving unwritten holes) whenever a lane's length is not
// exactly what the stride assumed.
const resultsInPositionOrder = new Array(inputs.units.length);
for (let k = 0; k < N; k++) {
  const lane = laneResults[k];
  for (let j = 0; j < lane.length; j++) {
    resultsInPositionOrder[lane[j].position] = lane[j].result;
  }
}

const OUTCOME_KEYS = ["accepted", "failed", "halted", "claim_unavailable", "agent_error", "not_attempted"];
const counts = {};
for (let c = 0; c < OUTCOME_KEYS.length; c++) counts[OUTCOME_KEYS[c]] = 0;
for (let r = 0; r < resultsInPositionOrder.length; r++) {
  const entry = resultsInPositionOrder[r];
  const outcome = entry ? entry.outcome : null;
  if (Object.prototype.hasOwnProperty.call(counts, outcome)) counts[outcome] += 1;
}

// advisory: true -- the store is truth. A lying agent can misreport its own
// enum value; this aggregate reconciles nothing and the invoking skill's
// post-return step must check `status` before acting on these counts.
return {
  run_id: inputs.runId,
  batch_id: inputs.batchId,
  wave_size: inputs.units.length,
  lanes: N,
  counts: counts,
  units: resultsInPositionOrder,
  advisory: true,
};
