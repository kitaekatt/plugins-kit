// relaunch-kit: the named values the hooks module keeps in the session
// ($.state). The module variable `phase` is authoritative inside one process;
// these values are the mirror that survives a hot reload.

export type RelaunchBinding =
  | { status: 'none' }
  | { status: 'disabled'; reason: string }
  | { status: 'bound'; token: string; dir: string };

export type RelaunchPhase =
  | 'idle'
  | 'armed'
  | 'counting'
  | 'committing'
  | 'committed';

export type RelaunchRequest = {
  action: 'resume' | 'prompt';
  prompt: string | null;
  reason: 'restart' | 'hand-off' | 'check';
};

declare module 'claude-code' {
  interface PluginState {
    'relaunch-kit': {
      binding: RelaunchBinding;
      phase: RelaunchPhase;
      request: RelaunchRequest | null;
    };
  }
}
