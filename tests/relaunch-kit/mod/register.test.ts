// relaunch-kit hooks module, exercised through `claude plugin test` against
// the engine's own `$`. test_mod.py copies the plugin and this file into a
// scratch folder and runs it there; nothing here ships with the plugin.
//
// The world beneath the plugin is mocked: a mock clock (held waits), an
// environment map that `env.set` writes to, an in-memory file system, and a
// recording `command.run` that stands for the engine's /exit.

import { expect, mock, test } from 'claude-code/testing'

const TOKEN = 'abcdef0123456789abcdef0123456789'
const DIR = '/data/relaunch'
const TOOL = 'mcp__relaunch-kit__request_restart'
const SID = 'sid-aaaa'
const FILE = `${DIR}/${TOKEN}.json`

type World = {
  clock: ReturnType<typeof mock.clock>
  env: Record<string, string | undefined>
  files: Map<string, string>
  writes: string[]
  toasts: string[]
  commands: string[]
  tools: any[]
}

type Options = {
  env?: Record<string, string>
  denyEnvSet?: string
  denyExit?: boolean
  failWrite?: boolean
  holdWrite?: boolean
}

function boot(on: any, opts: Options = {}): World {
  const clock = mock.clock(on)
  const world: World = {
    clock,
    env: { ...(opts.env ?? { RELAUNCH_KIT_TOKEN: TOKEN, RELAUNCH_KIT_DIR: DIR }) },
    files: new Map(),
    writes: [],
    toasts: [],
    commands: [],
    tools: [],
  }
  on('env.get', (_: any, e: any) => ({ value: world.env[e.name] }))
  on('env.set', (_: any, e: any) => {
    if (opts.denyEnvSet !== undefined) return { value: { deny: opts.denyEnvSet } }
    world.env[e.name] = e.value
    return { value: {} }
  })
  on('session.start', (_: any, e: any) => ({ cwd: e.cwd }))
  on('tool.register', (_: any, e: any) => {
    world.tools.push(e)
    return { value: { tool: TOOL } }
  })
  on('command.register', () => ({ value: { command: 'relaunch-check' } }))
  on('ui.toast', (_: any, e: any) => {
    world.toasts.push(e.text)
    return { value: undefined }
  })
  on('session.id', () => ({ value: SID }))
  on('session.root', () => ({ value: '/proj' }))
  on('fs.exists', (_: any, e: any) => ({ value: world.files.has(e.path) }))
  on('fs.read', (_: any, e: any) => ({ value: world.files.get(e.path) ?? '' }))
  on('fs.write', async (_: any, e: any) => {
    if (opts.failWrite) return { deny: 'disk full' }
    if (opts.holdWrite) await clock.sleep(1)
    world.files.set(e.path, e.text)
    world.writes.push(e.path)
    return { value: undefined }
  })
  on('command.run', (_: any, e: any) => {
    world.commands.push(e.command)
    // A throwing hook beneath the plugin makes `$.command.run` reject, which
    // is how the engine reports a refused exit.
    if (opts.denyExit) throw new Error('exit refused')
    return { value: { text: '' } }
  })
  on('turn.complete', () => ({ text: '' }))
  on('prompt.submit', (_: any, e: any) => ({ text: e.text }))
  on('prompt.edit', (_: any, e: any) => ({
    text: e.text.slice(0, e.start) + e.inputText + e.text.slice(e.end),
    cursor: e.start + e.inputText.length,
  }))
  return world
}

async function start($: any): Promise<void> {
  await $.session.start({ cwd: '/proj', surface: 'terminal', isInteractive: true })
}

async function ask($: any, input: Record<string, unknown>): Promise<any> {
  const r = await $.tool.call({ tool: TOOL, reason: 'test', ...input })
  if (r.deny !== undefined) return { deny: r.deny }
  return JSON.parse(r.result as string)
}

async function complete($: any, reason: string, extra: Record<string, unknown> = {}): Promise<void> {
  await $.turn.complete({
    answer: 'ok',
    durationMs: 1,
    isAborted: reason === 'aborted',
    turnId: 't1',
    reason,
    ...(reason === 'refusal' ? { refusal: { category: null, explanation: null } } : {}),
    ...extra,
  })
}

async function edit($: any, inputText = 'a'): Promise<any> {
  return $.prompt.edit({
    origin: { kind: 'composer' },
    text: '',
    cursor: 0,
    start: 0,
    end: 0,
    inputText,
  })
}

function tokenFile(world: World, path = FILE): any {
  const text = world.files.get(path)
  if (text === undefined) throw new Error(`no file written at ${path}`)
  return JSON.parse(text)
}

test('registers the tool undeferred and the check command', async ($, on) => {
  const w = boot(on)
  await start($)
  expect(w.tools.length).toBe(1)
  expect(w.tools[0].name).toBe('request_restart')
  expect(w.tools[0].isDeferred).toBe(false)
  expect(w.tools[0].description).toContain('Not calling this tool')
  expect(w.tools[0].inputSchema.required).toEqual(['mode', 'reason'])
})

test('binding: the token and dir are read once, then removed from the environment', async ($, on) => {
  const w = boot(on)
  await start($)
  expect(w.env.RELAUNCH_KIT_TOKEN).toBeUndefined()
  expect(w.env.RELAUNCH_KIT_DIR).toBeUndefined()
  // A second session.start (a reload) finds the environment empty and must not
  // rebind to "none": the binding lives in $.state.
  await start($)
  expect(await ask($, { mode: 'resume' })).toEqual({ status: 'armed', exits_after_turn: true })
})

test('env.set denied -> disabled: nothing is armed and nothing is ever written', async ($, on) => {
  const w = boot(on, { denyEnvSet: 'managed settings' })
  await start($)
  const r = await ask($, { mode: 'resume' })
  expect(r.status).toBe('disabled')
  expect(r.reason).toBe('managed settings')
  expect(r.next_command).toBe(`claude --resume ${SID}`)
  await complete($, 'answer')
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
  expect(w.commands).toEqual([])
})

test('no launcher env -> no-launcher with the manual command for each mode', async ($, on) => {
  const w = boot(on, { env: {} })
  await start($)
  expect(await ask($, { mode: 'resume' })).toEqual({
    status: 'no-launcher',
    next_command: `claude --resume ${SID}`,
  })
  const hand = await ask($, { mode: 'hand-off', prompt: '/task work dev/tasks/x' })
  expect(hand.status).toBe('no-launcher')
  expect(hand.next_command).toBe('claude "/task work dev/tasks/x"')
  await complete($, 'answer')
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
  expect(w.commands).toEqual([])
})

test('malformed token -> disabled, and the variables are still removed', async ($, on) => {
  const w = boot(on, { env: { RELAUNCH_KIT_TOKEN: '../evil', RELAUNCH_KIT_DIR: DIR } })
  await start($)
  expect(w.env.RELAUNCH_KIT_TOKEN).toBeUndefined()
  const r = await ask($, { mode: 'resume' })
  expect(r.status).toBe('disabled')
})

test('answer -> 3 s countdown -> token file -> exit 300 ms later', async ($, on) => {
  const w = boot(on)
  await start($)
  expect(await ask($, { mode: 'resume' })).toEqual({ status: 'armed', exits_after_turn: true })
  expect(w.writes).toEqual([])
  await complete($, 'answer')
  expect(w.toasts.some(t => t.includes('3 s'))).toBe(true)
  await w.clock.advance(2999)
  expect(w.writes).toEqual([])
  await w.clock.advance(1)
  expect(w.writes).toEqual([FILE])
  expect(w.commands).toEqual([])
  const file = tokenFile(w)
  expect(Object.keys(file).sort()).toEqual(
    ['action', 'cwd', 'prompt', 'reason', 'schema', 'session_id', 'token', 'written_at', 'writer'].sort(),
  )
  expect(file.schema).toBe(1)
  expect(file.token).toBe(TOKEN)
  expect(file.action).toBe('resume')
  expect(file.session_id).toBe(SID)
  expect(file.cwd).toBe('/proj')
  expect(file.prompt).toBeNull()
  expect(file.reason).toBe('restart')
  expect(file.written_at).toMatch(/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?Z$/)
  expect(file.writer).toBe('relaunch-kit 0.1.0')
  await w.clock.advance(299)
  expect(w.commands).toEqual([])
  await w.clock.advance(1)
  expect(w.commands).toEqual(['exit'])
})

test('hand-off writes action prompt with the prompt', async ($, on) => {
  const w = boot(on)
  await start($)
  expect((await ask($, { mode: 'hand-off', prompt: '/task work dev/tasks/x' })).status).toBe('armed')
  await complete($, 'answer')
  await w.clock.advance(3000)
  const file = tokenFile(w)
  expect(file.action).toBe('prompt')
  expect(file.prompt).toBe('/task work dev/tasks/x')
  expect(file.reason).toBe('hand-off')
})

for (const reason of ['refusal', 'aborted', 'error']) {
  test(`${reason} clears the arm: no countdown, no file, no exit`, async ($, on) => {
    const w = boot(on)
    await start($)
    expect((await ask($, { mode: 'resume' })).status).toBe('armed')
    await complete($, reason)
    await w.clock.advance(20000)
    expect(w.writes).toEqual([])
    expect(w.commands).toEqual([])
    // The phase is idle again, so a later request can arm.
    expect((await ask($, { mode: 'resume' })).status).toBe('armed')
    // And a later answer for an unarmed session does nothing.
    const w2 = w.writes.length
    await complete($, 'answer')
    await w.clock.advance(0)
    expect(w.toasts.filter(t => t.includes('3 s')).length).toBe(1)
    expect(w.writes.length).toBe(w2)
  })
}

test('a turn.complete with nothing armed, or from a subagent, starts nothing', async ($, on) => {
  const w = boot(on)
  await start($)
  await complete($, 'answer')
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
  await complete($, 'answer', { agentId: 'sub-1' })
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
})

test('prompt.edit during the countdown cancels it and passes the edit through unchanged', async ($, on) => {
  const w = boot(on)
  await start($)
  await ask($, { mode: 'resume' })
  await complete($, 'answer')
  await w.clock.advance(1000)
  const box = await edit($, 'x')
  expect(box).toEqual({ text: 'x', cursor: 1 })
  expect(w.toasts.some(t => t.includes('cancelled'))).toBe(true)
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
  expect(w.commands).toEqual([])
  // Cancelled -> idle: the next request arms again.
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
})

test('prompt.edit outside the countdown does nothing', async ($, on) => {
  const w = boot(on)
  await start($)
  await ask($, { mode: 'resume' })
  const box = await edit($, 'x')
  expect(box).toEqual({ text: 'x', cursor: 1 })
  await complete($, 'answer')
  await w.clock.advance(3000)
  expect(w.writes).toEqual([FILE])
})

test('prompt.submit during the countdown cancels it (late guard) and is not dropped', async ($, on) => {
  const w = boot(on)
  await start($)
  await ask($, { mode: 'resume' })
  await complete($, 'answer')
  await w.clock.advance(500)
  const r = await $.prompt.submit({ text: 'hello', wait: false, origin: { kind: 'composer' } })
  expect(r.drop).toBeUndefined()
  expect(r.text).toBe('hello')
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
  expect(w.commands).toEqual([])
})

test('prompt.submit during committing is dropped; our own exit is not', async ($, on) => {
  const w = boot(on, { holdWrite: true })
  await start($)
  await ask($, { mode: 'resume' })
  await complete($, 'answer')
  await w.clock.advance(3000)
  // The write is held, so the phase is still committing.
  expect(w.writes).toEqual([])
  const dropped = await $.prompt.submit({ text: 'late', wait: false, origin: { kind: 'composer' } })
  expect(dropped.drop).toContain('exiting for a restart')
  const own = await $.prompt.submit({
    text: '/exit',
    wait: false,
    origin: { kind: 'plugin', name: 'relaunch-kit' },
    command: { name: 'exit', args: '' },
  })
  expect(own.drop).toBeUndefined()
  await w.clock.advance(1)
  expect(w.writes).toEqual([FILE])
  // Committed: still dropped.
  const again = await $.prompt.submit({ text: 'later', wait: false, origin: { kind: 'composer' } })
  expect(again.drop).toContain('exiting for a restart')
  await w.clock.advance(300)
  expect(w.commands).toEqual(['exit'])
})

test('exit rejected -> tombstone, toast, idle', async ($, on) => {
  const w = boot(on, { denyExit: true })
  await start($)
  await ask($, { mode: 'hand-off', prompt: '/task work dev/tasks/x' })
  await complete($, 'answer')
  await w.clock.advance(3300)
  expect(w.commands).toEqual(['exit'])
  const file = tokenFile(w)
  expect(file.action).toBe('cancelled')
  expect(file.prompt).toBeNull()
  expect(file.token).toBe(TOKEN)
  expect(w.writes).toEqual([FILE, FILE])
  expect(w.toasts.some(t => t.includes('exit was refused'))).toBe(true)
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
})

test('token file write failure -> idle, toast, no exit', async ($, on) => {
  const w = boot(on, { failWrite: true })
  await start($)
  await ask($, { mode: 'resume' })
  await complete($, 'answer')
  await w.clock.advance(20000)
  expect(w.commands).toEqual([])
  expect(w.toasts.some(t => t.includes('could not write'))).toBe(true)
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
})

test('reload during the countdown -> idle, no file', async ($, on) => {
  const w = boot(on)
  await start($)
  await ask($, { mode: 'resume' })
  await complete($, 'answer')
  await w.clock.advance(1000)
  // A reload re-runs session.start; the old environment's timers are dropped
  // by the engine, so the countdown timer is not advanced afterwards here.
  await start($)
  expect(w.toasts.some(t => t.includes('plugin reload'))).toBe(true)
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
  expect(w.commands).toEqual([])
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
})

test('reload after the commit -> the file is rewritten and the exit rescheduled', async ($, on) => {
  const w = boot(on)
  await start($)
  await ask($, { mode: 'hand-off', prompt: '/task work dev/tasks/x' })
  await complete($, 'answer')
  await w.clock.advance(3000)
  expect(w.writes).toEqual([FILE])
  w.files.delete(FILE)
  await start($)
  expect(w.writes).toEqual([FILE, FILE])
  const file = tokenFile(w)
  expect(file.action).toBe('prompt')
  expect(file.prompt).toBe('/task work dev/tasks/x')
  await w.clock.advance(300)
  expect(w.commands.length).toBeGreaterThan(0)
})

test('reload while armed stays armed and a later answer proceeds', async ($, on) => {
  const w = boot(on)
  await start($)
  await ask($, { mode: 'resume' })
  await start($)
  await complete($, 'answer')
  await w.clock.advance(3000)
  expect(w.writes).toEqual([FILE])
})

test('/relaunch-check hop 2 commits resume with no turn.complete', async ($, on) => {
  const w = boot(on)
  await start($)
  const out = await $.command.run({ command: 'relaunch-check', args: '2 run1' })
  expect(out.text).toContain('hop 2')
  const file = tokenFile(w)
  expect(file.action).toBe('resume')
  expect(file.prompt).toBe('/relaunch-check 1 run1')
  expect(file.reason).toBe('check')
  expect(JSON.parse(w.files.get(`${DIR}/check-run1.log`)!.trim())).toEqual({
    run: 'run1',
    hop: 2,
    session_id: SID,
    token: TOKEN,
  })
  expect(w.commands).toEqual([])
  await w.clock.advance(300)
  expect(w.commands).toEqual(['exit'])
})

test('/relaunch-check hop 1 commits a prompt action for hop 0', async ($, on) => {
  const w = boot(on)
  await start($)
  await $.command.run({ command: 'relaunch-check', args: '1 run1' })
  const file = tokenFile(w)
  expect(file.action).toBe('prompt')
  expect(file.prompt).toBe('/relaunch-check 0 run1')
  expect(file.reason).toBe('check')
})

function chainLog(h2: string, h1: string, h0: string, t = ['t2', 't1']): string {
  return (
    JSON.stringify({ run: 'run1', hop: 2, session_id: h2, token: t[0] }) + '\n' +
    JSON.stringify({ run: 'run1', hop: 1, session_id: h1, token: t[1] }) + '\n' +
    (h0 === '' ? '' : '')
  )
}

test('/relaunch-check hop 0 writes a PASS verdict, no token file, and exits', async ($, on) => {
  const w = boot(on)
  w.files.set(`${DIR}/check-run1.log`, chainLog('s-old', 's-old', ''))
  await start($)
  const out = await $.command.run({ command: 'relaunch-check', args: '0 run1' })
  expect(out.text).toContain('PASS')
  expect(w.files.has(FILE)).toBe(false)
  const lines = w.files.get(`${DIR}/check-run1.log`)!.trim().split('\n').map(l => JSON.parse(l))
  expect(lines[lines.length - 1]).toEqual({ run: 'run1', verdict: 'PASS', reasons: [] })
  await w.clock.advance(300)
  expect(w.commands).toEqual(['exit'])
})

test('/relaunch-check hop 0 FAILs on a broken chain', async ($, on) => {
  // hop 1 lost hop 2's session id.
  const a = boot(on)
  a.files.set(`${DIR}/check-run1.log`, chainLog('s-old', 's-other', ''))
  await start($)
  const out = await $.command.run({ command: 'relaunch-check', args: '0 run1' })
  expect(out.text).toContain('FAIL')
  expect(out.text).toContain('hop 1 did not keep hop 2 session id')
})

test('/relaunch-check hop 0 FAILs when hop 0 shares hop 2 session id or tokens repeat', async ($, on) => {
  const w = boot(on)
  w.files.set(`${DIR}/check-run1.log`, chainLog(SID, SID, '', [TOKEN, 't1']))
  await start($)
  const out = await $.command.run({ command: 'relaunch-check', args: '0 run1' })
  expect(out.text).toContain('FAIL')
  expect(out.text).toContain('hop 0 did not get a new session id')
  expect(out.text).toContain('tokens are not distinct')
})

test('/relaunch-check without a launcher reports and stops', async ($, on) => {
  const w = boot(on, { env: {} })
  await start($)
  const out = await $.command.run({ command: 'relaunch-check', args: '2 run1' })
  expect(out.text).toContain('no-launcher')
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
  expect(w.commands).toEqual([])
})

test('/relaunch-check refuses bad arguments', async ($, on) => {
  boot(on)
  await start($)
  const a = await $.command.run({ command: 'relaunch-check', args: '5 run1' })
  expect(a.text).toContain('usage')
  const b = await $.command.run({ command: 'relaunch-check', args: '2 ../x' })
  expect(b.text).toContain('run-id')
})

test('request_restart from a subagent is denied and arms nothing', async ($, on) => {
  const w = boot(on)
  await start($)
  const r = await $.tool.call({ tool: TOOL, mode: 'resume', reason: 'x', agentId: 'sub-1' })
  expect(r.deny).toContain('main session only')
  await complete($, 'answer')
  await w.clock.advance(20000)
  expect(w.writes).toEqual([])
})

test('a second request while one is pending is denied', async ($, on) => {
  boot(on)
  await start($)
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
  const second = await ask($, { mode: 'resume' })
  expect(second.deny).toContain('already pending')
})

test('input rules: hand-off needs a one-line prompt, resume forbids one, reason is required', async ($, on) => {
  boot(on)
  await start($)
  expect((await ask($, { mode: 'hand-off' })).deny).toContain('requires a prompt')
  expect((await ask($, { mode: 'hand-off', prompt: 'a\nb' })).deny).toContain('one line')
  expect((await ask($, { mode: 'hand-off', prompt: 'x'.repeat(4097) })).deny).toContain('4096')
  expect((await ask($, { mode: 'resume', prompt: 'hi' })).deny).toContain('no prompt')
  expect((await ask($, { mode: 'nope' })).deny).toContain('mode must be')
  expect((await ask($, { mode: 'resume', reason: '' })).deny).toContain('reason is required')
  // None of those armed anything.
  expect((await ask($, { mode: 'resume' })).status).toBe('armed')
})
