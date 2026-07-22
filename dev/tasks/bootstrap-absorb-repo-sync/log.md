# Log: Bootstrap absorb repo sync

## 2026-07-22 -- task created from cross-repo investigation

Surface: user asked whether repo-sync is a bootstrap capability or
user-config-owned, then ruled: bootstrap should absorb it. Finding: it is
entirely user-side today (contract script + registry in ~/.claude), by a
ratified 2026-07-06 design ("doctrine flip"), and bootstrap's own git code
(git_deps clone-once, marketplace lifecycle) is a distinct provisioning
concern -- full detail in investigation-2026-07-22.md. Follow-up: this
task, gated on the user confirming the nag-razor tuning is sufficient
(their explicit requirement at creation: "note the requirement that the
'nagging' has been tuned sufficiently").

Rationale that would re-bite if lost: do NOT build the engine feature on
git_deps (semantics differ: push-back, dirty policy, unpinned branches),
and do NOT redesign nag policy from scratch -- the tuned script is the
parity spec. The gate exists because plugin-side iteration costs a
version bump + publish per tweak; absorbing while policy still churns
pays that tax at the worst time.
- 2026-07-22: update: priority = 'P2'; description = 'Absorb repo-sync into bootstrap as a tested declarative engine feature; gated on user sign-off that nag tuning is sufficient'
