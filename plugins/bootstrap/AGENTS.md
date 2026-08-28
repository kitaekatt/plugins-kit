# Bootstrap development guidance

## Primary product and technology goal

Build a portable state-management platform that exposes plugins-kit capabilities through a harness-compatible CLI. Treat `bootstrap_lib` as the reusable source of its capabilities and lifecycle semantics: inspection and discovery, explicit install, update/convergence and drift repair, checks, change reporting, and safe operations on managed resources.

The governing architecture is **capability = harness-independent core + harness layer**. The core owns capability and state semantics plus their operations. A harness layer translates those capabilities into Codex, OpenCode, or Claude Code invocation, permissions, lifecycle integration, and presentation. A harness layer may adapt a capability, but must never fork or redefine it.

Harness-specific enhancements are encouraged when they add native ergonomics, automation, richer presentation, or lifecycle integration for a popular environment. Keep them optional: essential capability semantics and behavior must remain complete in the harness-independent core. An enhancement must not become an implicit prerequisite, fork shared semantics, or cause the core to behave differently by harness.

The `AGENTS.md` symlink is an initial proving use case, not the architecture or the limit of the feature set. Keep the reusable core harness-neutral: it must not depend on Claude-specific hooks, tools, session events, or configuration types.

Codex is the first integration and must operate through Codex-legal CLI and permission semantics, never private harness mutation mechanisms. OpenCode and Claude Code are later harness layers over the same neutral core. Harness integrations are consumers, not the architecture; existing Claude SessionStart behavior may consume the core, but must not define it.

Before adding project-local bootstrap machinery, consult the repo-wide [reusable library capabilities](../../docs/reference/reusable-libraries.md) index and compose an existing shared capability when it fits.

Keep project-level `bootstrap.py` files thin and declarative: they describe the resources a project requires and expose explicit `install`, `update`, and `check` CLI operations by composing the shared library. A symlink is one kind of managed resource, not a reason to build a project-specific setup or update architecture.

Do not infer automatic startup or session-hook wiring from the existence of a bootstrap entry point. Bootstrap CLIs may be intentionally user-invoked only.
