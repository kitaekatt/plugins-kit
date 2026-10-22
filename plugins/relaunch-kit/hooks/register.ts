// relaunch-kit hooks module: asks Claude Code to exit after a turn so the
// terminal's `clauder` launcher can start the next session.
//
// Contract with the launcher: token file schema v1, written to
// <RELAUNCH_KIT_DIR>/<RELAUNCH_KIT_TOKEN>.json only once a countdown has been
// won, about EXIT_DELAY_MS before the exit. The launcher reads and deletes it.
//
// Only top-level function declarations take `$` (a closure helper that takes
// `$` fails `claude plugin validate`).

import type { EngineInterface, Register } from 'claude-code'
import type {
  RelaunchBinding,
  RelaunchPhase,
  RelaunchRequest,
} from '../types/index'

const PLUGIN = 'relaunch-kit'
const WRITER = 'relaunch-kit 0.1.0'
const TOOL_NAME = 'mcp__relaunch-kit__request_restart'
const COUNTDOWN_MS = 3000
const EXIT_DELAY_MS = 300
const TOKEN_RE = /^[A-Za-z0-9_-]{8,128}$/
const RUN_RE = /^[A-Za-z0-9_-]{1,64}$/
const MAX_PROMPT = 4096

const BINDING = { plugin: 'relaunch-kit', key: 'binding' } as const
const PHASE = { plugin: 'relaunch-kit', key: 'phase' } as const
const REQUEST = { plugin: 'relaunch-kit', key: 'request' } as const

const TOOL_DESCRIPTION =
  'Restart Claude Code in this terminal. Call this only from the main ' +
  'session, only when the user asked in this conversation for a restart or ' +
  "a hand-off, and only after the restart skill's checklist found no " +
  'blocker. When the answer is "armed", Claude Code exits about 3 seconds ' +
  "after this turn ends and this terminal's clauder starts the next " +
  'session. Not calling this tool is how an automatic restart is blocked. ' +
  'mode "resume" resumes this session and waits; mode "hand-off" starts a ' +
  'new session whose first prompt is `prompt` (required for hand-off, one ' +
  'line, at most 4096 characters, and forbidden for resume). `reason` is ' +
  'one line saying why.'

// The phase machine. `phase` is authoritative inside one process; $.state
// holds a mirror that is read only to recover after a hot reload.
let phase: RelaunchPhase = 'idle'
let countdown: { cancel: () => void } | null = null
let pending: RelaunchRequest | null = null

function pathSep(dir: string): string {
  return dir.includes('\\') && !dir.includes('/') ? '\\' : '/'
}

// The one synchronous transition. It throws unless `phase === from` and
// assigns before any await, so of two racing callbacks exactly one wins.
async function go(
  $: EngineInterface,
  from: RelaunchPhase,
  to: RelaunchPhase,
): Promise<void> {
  if (phase !== from) {
    throw new Error(`${PLUGIN}: phase is ${phase}, expected ${from} (to ${to})`)
  }
  phase = to
  if (from === 'counting' && countdown) {
    countdown.cancel()
    countdown = null
  }
  await $.state.set(PHASE, to)
}

// Reload recovery: adopt a mirrored phase without a transition check.
async function adopt($: EngineInterface, to: RelaunchPhase): Promise<void> {
  phase = to
  await $.state.set(PHASE, to)
}

// Binding, done once per session: the token and directory are read from the
// launcher's environment and removed from it, so nested `claude` children
// cannot inherit them.
async function bindOnce($: EngineInterface): Promise<void> {
  const held = await $.state.get(BINDING)
  if (held.value !== undefined) return
  const token = await $.env.get('RELAUNCH_KIT_TOKEN')
  const dir = await $.env.get('RELAUNCH_KIT_DIR')
  let binding: RelaunchBinding
  if (!token && !dir) {
    binding = { status: 'none' }
  } else {
    const r1 = await $.env.set('RELAUNCH_KIT_TOKEN', undefined)
    const r2 = await $.env.set('RELAUNCH_KIT_DIR', undefined)
    const denied = r1.deny ?? r2.deny
    if (denied !== undefined) {
      binding = { status: 'disabled', reason: denied }
    } else if (!token || !dir) {
      binding = {
        status: 'disabled',
        reason: 'RELAUNCH_KIT_TOKEN and RELAUNCH_KIT_DIR must both be set',
      }
    } else if (!TOKEN_RE.test(token)) {
      binding = { status: 'disabled', reason: 'RELAUNCH_KIT_TOKEN is malformed' }
    } else {
      binding = { status: 'bound', token, dir }
    }
  }
  await $.state.set(BINDING, binding)
}

async function readBinding($: EngineInterface): Promise<RelaunchBinding | undefined> {
  return (await $.state.get(BINDING)).value
}

async function tokenFile(
  $: EngineInterface,
  binding: { token: string; dir: string },
  action: 'resume' | 'prompt' | 'cancelled',
  request: RelaunchRequest,
): Promise<string> {
  const file = {
    schema: 1,
    token: binding.token,
    action,
    session_id: await $.session.id(),
    cwd: await $.session.root(),
    prompt: action === 'cancelled' ? null : request.prompt,
    reason: request.reason,
    written_at: new Date().toISOString(),
    writer: WRITER,
  }
  const path = `${binding.dir}${pathSep(binding.dir)}${binding.token}.json`
  await $.fs.write(path, JSON.stringify(file, null, 2) + '\n')
  return path
}

// Phase must be `committing`. Writes the token file, then schedules the exit.
async function commitAndExit(
  $: EngineInterface,
  request: RelaunchRequest,
): Promise<void> {
  const binding = await readBinding($)
  if (binding === undefined || binding.status !== 'bound') {
    await go($, 'committing', 'idle')
    $.ui.toast('relaunch-kit: no launcher binding; nothing written, no restart')
    return
  }
  try {
    await tokenFile($, binding, request.action, request)
  } catch (err) {
    await go($, 'committing', 'idle')
    $.ui.toast(`relaunch-kit: could not write the restart file: ${String(err)}`)
    return
  }
  $.clock.after(EXIT_DELAY_MS, () => {
    void exitNow($)
  })
  await go($, 'committing', 'committed')
}

async function exitNow($: EngineInterface): Promise<void> {
  if (phase !== 'committed') return
  try {
    await $.command.run({ command: 'exit' })
  } catch (err) {
    let note = ''
    const binding = await readBinding($)
    if (pending !== null && binding !== undefined && binding.status === 'bound') {
      try {
        await tokenFile($, binding, 'cancelled', pending)
      } catch (err2) {
        note = ` (and the cancel marker failed: ${String(err2)})`
      }
    }
    pending = null
    await go($, 'committed', 'idle')
    $.ui.toast(
      `relaunch-kit: exit was refused (${String(err)}); exit manually, no relaunch follows${note}`,
    )
  }
}

async function startCountdown($: EngineInterface): Promise<void> {
  const moved = go($, 'armed', 'counting')
  countdown = $.clock.after(COUNTDOWN_MS, () => {
    void countdownElapsed($)
  })
  await moved
  $.ui.toast('Restarting in 3 s. Type anything to cancel.', {
    timeoutMs: COUNTDOWN_MS,
  })
}

async function countdownElapsed($: EngineInterface): Promise<void> {
  if (phase !== 'counting') return
  const request = pending
  const moved = go($, 'counting', 'committing')
  await moved
  if (request === null) {
    await go($, 'committing', 'idle')
    $.ui.toast('relaunch-kit: the restart request was lost; no restart')
    return
  }
  await commitAndExit($, request)
}

async function cancelCountdown($: EngineInterface, why: string): Promise<void> {
  if (phase !== 'counting') return
  pending = null
  const moved = go($, 'counting', 'idle')
  await moved
  $.ui.toast(`Restart cancelled (${why}). Run /restart again to restart.`)
}

async function recover($: EngineInterface): Promise<void> {
  const mirrored = (await $.state.get(PHASE)).value ?? 'idle'
  const held = (await $.state.get(REQUEST)).value ?? null
  pending = held
  phase = mirrored
  switch (mirrored) {
    case 'idle':
    case 'armed':
      return
    case 'counting':
      // Timers died with the old environment; no file exists, nothing is stale.
      pending = null
      await go($, 'counting', 'idle')
      $.ui.toast(
        'relaunch-kit: restart countdown cancelled by a plugin reload; run /restart again',
      )
      return
    case 'committing':
    case 'committed':
      // The countdown had elapsed, so the user's intent stands.
      if (held === null) {
        await adopt($, 'idle')
        $.ui.toast('relaunch-kit: a reload lost the restart request; no restart')
        return
      }
      if (mirrored === 'committed') await go($, 'committed', 'committing')
      await commitAndExit($, held)
      return
    default: {
      const unreachable: never = mirrored
      throw new Error(`${PLUGIN}: unknown mirrored phase ${String(unreachable)}`)
    }
  }
}

async function registerSurface($: EngineInterface): Promise<void> {
  await $.tool.register({
    name: 'request_restart',
    description: TOOL_DESCRIPTION,
    inputSchema: {
      type: 'object',
      properties: {
        mode: { type: 'string', enum: ['resume', 'hand-off'] },
        prompt: { type: 'string', maxLength: MAX_PROMPT },
        reason: { type: 'string' },
      },
      required: ['mode', 'reason'],
      additionalProperties: false,
    },
    isDeferred: false,
  })
  await $.command.register({
    name: 'relaunch-check',
    description: 'Run the relaunch-kit restart self-check (3 hops, no model calls).',
    argumentHint: '[hops] [run-id]',
  })
}

function validatePrompt(mode: unknown, prompt: unknown): string | null {
  if (mode !== 'resume' && mode !== 'hand-off') {
    return 'mode must be "resume" or "hand-off"'
  }
  if (mode === 'resume') {
    return prompt === undefined || prompt === null
      ? null
      : 'resume takes no prompt'
  }
  if (typeof prompt !== 'string' || prompt.trim() === '') {
    return 'hand-off requires a prompt'
  }
  if (prompt.length > MAX_PROMPT) {
    return `prompt is longer than ${MAX_PROMPT} characters`
  }
  if (/[\r\n\0]/.test(prompt)) {
    return 'prompt must be one line with no NUL'
  }
  return null
}

function quoteArg(text: string): string {
  return '"' + text.replace(/(["\\$`])/g, '\\$1') + '"'
}

async function requestRestart(
  $: EngineInterface,
  e: { agentId?: string; mode?: unknown; prompt?: unknown; reason?: unknown },
): Promise<{ result: string } | { deny: string }> {
  if (e.agentId !== undefined) {
    return { deny: `${PLUGIN}: request_restart is for the main session only` }
  }
  if (phase !== 'idle') {
    return { deny: `${PLUGIN}: a restart is already pending` }
  }
  const bad = validatePrompt(e.mode, e.prompt)
  if (bad !== null) return { deny: `${PLUGIN}: ${bad}` }
  if (typeof e.reason !== 'string' || e.reason.trim() === '') {
    return { deny: `${PLUGIN}: reason is required` }
  }
  const mode = e.mode as 'resume' | 'hand-off'
  const prompt = mode === 'hand-off' ? (e.prompt as string) : null
  const binding = await readBinding($)
  if (binding === undefined) {
    return { deny: `${PLUGIN}: the session was not bound (session.start did not run)` }
  }
  if (binding.status !== 'bound') {
    const sessionId = await $.session.id()
    const next_command =
      prompt === null ? `claude --resume ${sessionId}` : `claude ${quoteArg(prompt)}`
    const answer =
      binding.status === 'none'
        ? { status: 'no-launcher', next_command }
        : { status: 'disabled', reason: binding.reason, next_command }
    return { result: JSON.stringify(answer) }
  }
  const request: RelaunchRequest = {
    action: mode === 'resume' ? 'resume' : 'prompt',
    prompt,
    reason: mode === 'resume' ? 'restart' : 'hand-off',
  }
  pending = request
  await $.state.set(REQUEST, request)
  await go($, 'idle', 'armed')
  return { result: JSON.stringify({ status: 'armed', exits_after_turn: true }) }
}

// /relaunch-check [hops] [run-id]: no model calls, so no turn.complete.
async function relaunchCheck(
  $: EngineInterface,
  args: string,
): Promise<{ text: string }> {
  const parts = args.trim().split(/\s+/).filter(p => p !== '')
  const hopText = parts[0] ?? '2'
  if (!/^[0-2]$/.test(hopText) || parts.length > 2) {
    return { text: 'usage: /relaunch-check [hops 0-2] [run-id]' }
  }
  const hop = Number(hopText)
  let run = parts[1]
  if (run === undefined) {
    run = Array.from(crypto.getRandomValues(new Uint8Array(4)), b =>
      b.toString(16).padStart(2, '0'),
    ).join('')
  }
  if (!RUN_RE.test(run)) return { text: 'run-id must match [A-Za-z0-9_-]{1,64}' }
  const binding = await readBinding($)
  if (binding === undefined || binding.status === 'none') {
    return {
      text: 'relaunch-check: no-launcher: this session was not started under clauder',
    }
  }
  if (binding.status === 'disabled') {
    return { text: `relaunch-check: disabled: ${binding.reason}` }
  }
  if (phase !== 'idle') return { text: 'relaunch-check: a restart is already pending' }

  const sessionId = await $.session.id()
  const logPath = `${binding.dir}${pathSep(binding.dir)}check-${run}.log`
  const before = (await $.fs.exists(logPath)) ? ((await $.fs.read(logPath)) as string) : ''
  const entry = { run, hop, session_id: sessionId, token: binding.token }
  let log = before + JSON.stringify(entry) + '\n'

  if (hop === 0) {
    const rows = log
      .split('\n')
      .filter(l => l.trim() !== '')
      .map(l => JSON.parse(l) as Record<string, string | number>)
    const at = (n: number) => rows.find(r => r.hop === n)
    const [h2, h1, h0] = [at(2), at(1), at(0)]
    const reasons: string[] = []
    if (!h2 || !h1 || !h0) reasons.push('chain incomplete: hops 2, 1 and 0 are all required')
    else {
      if (h1.session_id !== h2.session_id) reasons.push('hop 1 did not keep hop 2 session id')
      if (h0.session_id === h2.session_id) reasons.push('hop 0 did not get a new session id')
      if (new Set([h2.token, h1.token, h0.token]).size !== 3) reasons.push('tokens are not distinct')
    }
    const verdict = reasons.length === 0 ? 'PASS' : 'FAIL'
    log += JSON.stringify({ run, verdict, reasons }) + '\n'
    await $.fs.write(logPath, log)
    // No token file: the launcher sees none and exits with claude's code.
    pending = null
    await go($, 'idle', 'committed')
    $.clock.after(EXIT_DELAY_MS, () => {
      void exitNow($)
    })
    return { text: `relaunch-check ${run}: ${verdict}${reasons.length ? ' ' + reasons.join('; ') : ''}` }
  }

  await $.fs.write(logPath, log)
  const request: RelaunchRequest =
    hop === 2
      ? { action: 'resume', prompt: `/relaunch-check 1 ${run}`, reason: 'check' }
      : { action: 'prompt', prompt: `/relaunch-check 0 ${run}`, reason: 'check' }
  pending = request
  await $.state.set(REQUEST, request)
  await go($, 'idle', 'committing')
  await commitAndExit($, request)
  return { text: `relaunch-check ${run}: hop ${hop} done, restarting` }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await bindOnce($)
    await recover($)
    await registerSurface($)
    return next(e)
  })

  on('tool.call', { tool: TOOL_NAME }, async ($, e) => requestRestart($, e)).catch(
    (_$, _e, next) =>
      next.called
        ? next(_e)
        : { deny: `${PLUGIN}: request_restart failed; nothing was armed` },
  )

  on('command.run', { command: 'relaunch-check' }, async ($, e) =>
    relaunchCheck($, e.args),
  )

  on('turn.complete', async ($, e, next) => {
    if (e.agentId !== undefined) return next(e)
    switch (e.reason) {
      case 'answer':
        if (phase === 'armed') await startCountdown($)
        break
      case 'aborted':
      case 'refusal':
      case 'error':
        if (phase === 'armed') {
          pending = null
          await go($, 'armed', 'idle')
          $.ui.toast(`Restart cancelled: the turn ended with ${e.reason}.`)
        }
        break
      default: {
        const unreachable: never = e.reason
        if (phase !== 'idle') await go($, phase, 'idle')
        throw new Error(`${PLUGIN}: unknown turn reason ${String(unreachable)}`)
      }
    }
    return next(e)
  })

  // Typing cancels the countdown: prompt.edit fires on keystrokes and pastes.
  // The edit passes through unchanged.
  on('prompt.edit', async ($, e, next) => {
    if (phase === 'counting') await cancelCountdown($, 'you typed')
    return next(e)
  })

  // Late guard: a prompt submitted while counting also cancels; once the
  // commit has begun, a foreign prompt is dropped so the exit never queues
  // behind an unexpected turn.
  on('prompt.submit', async ($, e, next) => {
    if (phase === 'counting') await cancelCountdown($, 'you submitted a prompt')
    else if (
      (phase === 'committing' || phase === 'committed') &&
      e.origin.kind !== 'plugin' &&
      e.command?.name !== 'exit'
    ) {
      $.ui.toast('relaunch-kit: exiting for a restart; prompt dropped')
      return { drop: 'relaunch-kit: exiting for a restart' }
    }
    return next(e)
  })
}
