"""Bootstrap check: link <project>/.agents/skills -> <project>/.claude/skills.

Codex reads project skills from ``.agents/skills``; Claude Code reads them
from ``.claude/skills``. When Codex is installed and a project has Claude
skills, bootstrap creates the former as a link to the latter (a directory
symlink, or an NTFS junction on Windows when a symlink cannot be created),
and excludes the generated path from Git and Perforce first.

Everything here keys on ``.agents/skills``, the child -- never on ``.agents``
itself. ``.agents/`` is a SHARED directory: Codex also keeps repo-level
plugin config at ``.agents/plugins/marketplace.json``, which is why the
generated ignore rule below anchors the skills child rather than the parent.
Treating the parent as the sentinel contradicted that -- a project that
adopted ``.agents/plugins/`` silently never got its skills link, and the only
documented recovery ("delete .agents to rebuild") meant deleting that config.
So an existing ``.agents`` is ADOPTED, and only a missing one is created;
what this module refuses to touch is a pre-existing ``.agents/skills``.

Scope for v1 is the current PROJECT ROOT only -- plugin install paths are
explicitly out of scope. Codex resolves a project root by walking up from
CWD to a ``project_root_markers`` entry, defaulting to ``.git``; a plugin
cache directory is never a CWD and is not a git repo, so a link created
there would never be discovered by Codex. It would converge cleanly and do
nothing, which is not worth the second root sweep.

For the same reason, this module links ONLY when the project directory IS
the git repository root (``git rev-parse --show-toplevel`` normalizes to the
same path). ``project_dir`` is ``$PWD``, not a discovered root, and Codex's
behavior in a directory with no ``.git`` root marker is untested -- linking
there would be unverified work, so both "not a git worktree at all" and
"inside a worktree but not its top level" are quick-exit skips, not
failures. This also means a Perforce-only workspace with no ``.git`` skips
entirely for v1, even though the P4 exclusion path below is implemented in
full: a tree can be both git- and P4-managed (this is deliberately not the
same as "P4 is unsupported").

This module is side-effect-free in its check half and does the actual
filesystem/VCS work in its fix half, mirroring every other bootstrap check.
ALL user-facing logging (the exact ``ok``/``action``/``fail`` message text)
stays in the engine -- this module returns structured outcomes only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass

from .codex import detect_codex

AGENTS_DIRNAME = ".agents"
SKILLS_DIRNAME = "skills"
SOURCE_REL = os.path.join(".claude", "skills")
#: Display form used in engine messages -- always POSIX-style regardless of
#: platform, since it names a path inside the manifest/docs vocabulary.
SOURCE_DISPLAY = ".claude/skills"

#: The generated ignore rule anchors the SKILLS child, not the whole of
#: `.agents/` -- Codex also uses `.agents/plugins/marketplace.json` for
#: repo-level plugin config, and a whole-parent rule would silently hide
#: that from `git status`/`p4 status` if a user adds it later.
_GENERATED_RULE = "/%s/%s/" % (AGENTS_DIRNAME, SKILLS_DIRNAME)
_GIT_EXCLUDE_HEADER = "# plugins-kit bootstrap: generated agent skills link"
_P4_IGNORE_DEFAULT_NAME = "p4ignore.txt"
_P4_GIT_EXCLUDE_HEADER = "# plugins-kit bootstrap: generated Perforce ignore file"

_GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
_SUBPROCESS_TIMEOUT = 15.0

# WinError for an unelevated symlink attempt without SeCreateSymbolicLinkPrivilege
# (no Developer Mode, not elevated). Established elsewhere in this repo at
# bootstrap_lib/env_features.py.
_WINERROR_PRIVILEGE_NOT_HELD = 1314


# ---------------------------------------------------------------------------
# Side-effect-free check
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillsLinkCheck:
    """Outcome of the side-effect-free half of the check.

    ``status`` is one of:

    - ``existing``          -- .agents/skills already exists; quick-exit skip.
    - ``lstat_error``        -- could not lstat .agents/skills; failure. This
      is also how a non-directory ``.agents`` surfaces (ENOTDIR), which is
      unusual enough that the OS message is a better report than a status of
      its own.
    - ``not_directory``      -- project root is not a directory; failure.
    - ``not_worktree``       -- project root has no git repository at all; skip.
    - ``not_toplevel``       -- inside a worktree but not its root; skip.
    - ``root_check_error``   -- git rev-parse failed unexpectedly; failure.
    - ``invalid_option``     -- agent_skills_link is present but not a bool; failure.
    - ``opt_out``            -- agent_skills_link is explicitly false; skip.
    - ``codex_unavailable``  -- detect_codex() reports unavailable; skip.
    - ``source_missing``     -- .claude/skills is missing/not a directory; skip.
    - ``source_empty``       -- .claude/skills has no entries; skip.
    - ``source_read_error``  -- .claude/skills could not be enumerated; failure.
    - ``fixable``            -- every precondition holds; the fixer may run.

    ``detail`` carries an error/reason string where relevant; ``toplevel``
    carries the resolved git root for the ``not_toplevel`` message.
    """

    status: str
    detail: str = ""
    toplevel: str = ""


def check_project_agent_skills_link(project_dir, agent_skills_link_value):
    """The side-effect-free check. Never writes anything.

    ``agent_skills_link_value`` is the value already resolved from the
    effective layered manifest (``None`` when absent -- callers must not
    pass a JSON ``null`` through as a distinct value: manifest_merge already
    treats an explicit null as absent, so there is no way to observe one
    here).
    """
    link_path = os.path.join(project_dir, AGENTS_DIRNAME, SKILLS_DIRNAME)

    # Step 1 (design step 3): the .agents/skills quick-exit runs before
    # ANYTHING else -- config lookup, Codex detection, source inspection, VCS
    # commands -- so a manually-managed link always wins. lstat, not stat: a
    # dangling link is still somebody's link and is not ours to repair.
    try:
        os.lstat(link_path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        return SkillsLinkCheck("lstat_error", detail=str(exc))
    else:
        return SkillsLinkCheck("existing")

    if not os.path.isdir(project_dir):
        return SkillsLinkCheck("not_directory")

    # D2: link only when project_dir IS the git repository root.
    toplevel, root_status, root_detail = _git_toplevel(project_dir)
    if root_status == "no_git":
        return SkillsLinkCheck("not_worktree")
    if root_status == "error":
        return SkillsLinkCheck("root_check_error", detail=root_detail)
    if _normalize_root(toplevel) != _normalize_root(project_dir):
        return SkillsLinkCheck("not_toplevel", toplevel=toplevel)

    # Strict boolean validation. Absent (None) means "enabled by default";
    # anything present that is not exactly a bool is invalid.
    if agent_skills_link_value is not None:
        if type(agent_skills_link_value) is not bool:
            return SkillsLinkCheck("invalid_option")
        if agent_skills_link_value is False:
            return SkillsLinkCheck("opt_out")

    detection = detect_codex()
    if not detection.available:
        return SkillsLinkCheck("codex_unavailable", detail=detection.reason)

    source_path = os.path.join(project_dir, SOURCE_REL)
    try:
        if not os.path.isdir(source_path):
            return SkillsLinkCheck("source_missing")
        with os.scandir(source_path) as it:
            has_entry = any(True for _ in it)
    except OSError as exc:
        return SkillsLinkCheck("source_read_error", detail=str(exc))
    if not has_entry:
        return SkillsLinkCheck("source_empty")

    return SkillsLinkCheck("fixable")


def _normalize_root(path):
    return os.path.normcase(os.path.normpath(os.path.abspath(path)))


def _git_toplevel(root):
    """Returns (toplevel_or_None, status, detail).

    status is "ok", "no_git", or "error".
    """
    try:
        proc = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=_GIT_ENV, timeout=_SUBPROCESS_TIMEOUT, text=True,
        )
    except FileNotFoundError:
        # git itself is not on PATH -- fail closed, same posture as
        # detect_codex(): an environment without git cannot have a git root.
        return None, "no_git", "git not found on PATH"
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "error", str(exc)
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "not a git repository" in stderr.lower():
            return None, "no_git", stderr
        return None, "error", stderr or ("git rev-parse exited %s" % proc.returncode)
    toplevel = (proc.stdout or "").strip()
    if not toplevel:
        return None, "error", "git rev-parse --show-toplevel returned no output"
    return os.path.normpath(toplevel), "ok", ""


# ---------------------------------------------------------------------------
# Fixer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SkillsLinkFixResult:
    """Outcome of :func:`create_agent_skills_link`.

    ``status`` is one of: ``created``, ``race_existing``, ``mkdir_failed``,
    ``vcs_failed``, ``vcs_failed_cleanup_failed``, ``link_failed``,
    ``link_failed_cleanup_failed``, ``verify_error``.

    ``vcs_result`` carries the successful VCS summary (only meaningful when
    ``status == "created"``); ``detail``/``cleanup_detail`` carry failure
    reasons.
    """

    ok: bool
    status: str
    mechanism: str = ""
    vcs_result: str = ""
    detail: str = ""
    cleanup_detail: str = ""


def create_agent_skills_link(project_dir):
    """Ensure .agents exists, apply VCS exclusions, then create .agents/skills.

    ``.agents`` is created only when ABSENT and adopted when present -- see
    the module docstring: it is shared with Codex's own ``.agents/plugins/``,
    so its existence says nothing about whether the skills link is wanted.

    Every failure branch removes only what THIS attempt created (design
    EDGE CASES: a failed creation must not touch pre-existing content, and
    successful VCS exclusions may remain after a later failure -- they are
    harmless and make the next attempt cheaper). Adoption sharpens that rule
    rather than relaxing it: an ``.agents`` this attempt did not create is
    never removed, however the attempt ends.
    """
    agents_path = os.path.join(project_dir, AGENTS_DIRNAME)
    source_path = os.path.join(project_dir, SOURCE_REL)
    link_path = os.path.join(agents_path, SKILLS_DIRNAME)

    created_agents = True
    try:
        os.mkdir(agents_path)
    except FileExistsError:
        created_agents = False
        if not os.path.isdir(agents_path):
            # A regular file or a dangling symlink at .agents. The check half
            # reports this as lstat_error and never reaches the fixer, so this
            # is the race/direct-call guard, not the usual path.
            return SkillsLinkFixResult(
                False, "mkdir_failed",
                detail="%s exists and is not a directory" % agents_path)
    except OSError as exc:
        return SkillsLinkFixResult(False, "mkdir_failed", detail=str(exc))

    if os.path.lexists(link_path):
        # Another process created the link between the check and here. The
        # race used to be caught by mkdir; adopting .agents moves it down to
        # the child, which is the thing that actually has one owner. Checked
        # BEFORE the VCS phase: there is nothing left to exclude for, and the
        # winner has already done it.
        return SkillsLinkFixResult(False, "race_existing")

    vcs_ok, vcs_result = _apply_vcs_exclusions(project_dir)
    if not vcs_ok:
        cleanup_err = _cleanup_after_failure(link_path, agents_path, created_agents)
        if cleanup_err:
            return SkillsLinkFixResult(
                False, "vcs_failed_cleanup_failed",
                detail=vcs_result, cleanup_detail=cleanup_err,
            )
        return SkillsLinkFixResult(False, "vcs_failed", detail=vcs_result)

    link_ok, mechanism, link_detail = _create_link(source_path, link_path, agents_path)
    if not link_ok:
        if os.path.lexists(link_path):
            # Absent moments ago, present now, and _create_link removes its
            # own partial artifacts -- so this is the loser of a race. Leave
            # it: cleaning up here would delete the WINNER's link.
            return SkillsLinkFixResult(False, "race_existing")
        cleanup_err = _cleanup_after_failure(link_path, agents_path, created_agents)
        if cleanup_err:
            return SkillsLinkFixResult(
                False, "link_failed_cleanup_failed",
                vcs_result=vcs_result, detail=link_detail, cleanup_detail=cleanup_err,
            )
        return SkillsLinkFixResult(
            False, "link_failed", vcs_result=vcs_result, detail=link_detail,
        )

    # The fixer must verify the link before the engine's authoritative
    # re-check is allowed to trust it (design step 11/12).
    verify_err = _verify_link(link_path, source_path)
    if verify_err:
        cleanup_err = _cleanup_after_failure(link_path, agents_path, created_agents)
        return SkillsLinkFixResult(
            False, "verify_error", mechanism=mechanism,
            vcs_result=vcs_result, detail=verify_err,
            cleanup_detail=cleanup_err or "",
        )

    return SkillsLinkFixResult(True, "created", mechanism=mechanism, vcs_result=vcs_result)


def _cleanup_after_failure(link_path, agents_path, created_agents):
    """Remove only what this attempt created; returns an error string or None.

    ``created_agents`` is the whole point: an adopted ``.agents`` may hold
    Codex's ``.agents/plugins/`` config that this code did not create, so a
    failed link must not take it down.
    """
    try:
        if os.path.lexists(link_path):
            _remove_link_artifact(link_path)
    except OSError as exc:
        return str(exc)
    if not created_agents:
        return None
    try:
        os.rmdir(agents_path)
    except OSError as exc:
        return str(exc)
    return None


def _remove_link_artifact(link_path):
    """Remove a partially-created link/junction at link_path (D7: leave no
    partial target behind)."""
    if os.path.islink(link_path):
        os.unlink(link_path)
        return
    if os.name == "nt":
        try:
            from pathlib import Path
            if Path(link_path).is_junction():
                # A junction is a reparse-point directory; rmdir removes the
                # reparse point itself without touching the link target.
                os.rmdir(link_path)
                return
        except OSError:
            pass
    if os.path.isdir(link_path):
        os.rmdir(link_path)
    elif os.path.lexists(link_path):
        os.unlink(link_path)


def _create_link(source_path, link_path, agents_dir):
    """Returns (ok, mechanism, detail). mechanism is only set on success."""
    rel_target = os.path.relpath(source_path, start=agents_dir)
    try:
        os.symlink(rel_target, link_path, target_is_directory=True)
        return True, "directory symlink", ""
    except OSError as exc:
        # `except ... as name` unbinds `name` at the end of the block (PEP
        # 3110) -- capture the message now so it survives past this except.
        symlink_error = exc

    if os.name != "nt":
        return False, "", str(symlink_error)

    # D7: junction fallback only for a privilege/unsupported-filesystem
    # error. A destination collision or an unrelated access-denied error is
    # a real failure, not a reason to mask the problem with a junction.
    winerror = getattr(symlink_error, "winerror", None)
    if winerror != _WINERROR_PRIVILEGE_NOT_HELD:
        return False, "", str(symlink_error)

    try:
        import _winapi
        create_junction = getattr(_winapi, "CreateJunction", None)
    except (ImportError, AttributeError):
        create_junction = None
    if create_junction is None:
        return False, "", (
            "symlink failed (%s); junction fallback unavailable "
            "(_winapi.CreateJunction not present)" % symlink_error
        )

    abs_source = os.path.abspath(source_path)
    try:
        create_junction(abs_source, link_path)
        return True, "NTFS junction", ""
    except (OSError, AttributeError) as junction_error:
        try:
            if os.path.lexists(link_path):
                _remove_link_artifact(link_path)
        except OSError:
            pass
        return False, "", (
            "symlink failed (%s); junction failed (%s)" % (symlink_error, junction_error)
        )


def _verify_link(link_path, source_path):
    """Returns None on success, else an error string."""
    is_link = os.path.islink(link_path)
    is_junction = False
    if not is_link and os.name == "nt":
        try:
            from pathlib import Path
            is_junction = Path(link_path).is_junction()
        except OSError as exc:
            return "could not classify %s: %s" % (link_path, exc)
    if not is_link and not is_junction:
        return "%s is neither a symlink nor a junction" % link_path
    try:
        if not os.path.samefile(link_path, source_path):
            return "%s does not resolve to %s" % (link_path, source_path)
    except OSError as exc:
        return "could not verify link target: %s" % exc
    return None


# ---------------------------------------------------------------------------
# VCS exclusions
# ---------------------------------------------------------------------------


def _apply_vcs_exclusions(root):
    """Returns (ok, result_or_reason).

    On success, result is a "; "-joined summary of every VCS action taken
    (design {vcs_result}). On failure, it is the failure reason; the caller
    uses it verbatim in the {reason} slot of the failure message.
    """
    parts = []

    git_status, git_detail = _apply_git_exclusion(root)
    if git_status == "error":
        return False, git_detail
    if git_status != "none":
        parts.append(git_detail)

    p4_status, p4_detail = _apply_p4_exclusion(root)
    if p4_status == "error":
        return False, p4_detail
    if p4_status != "none":
        parts.append(p4_detail)

    if not parts:
        parts.append("no VCS workspace detected")
    return True, "; ".join(parts)


def _run_git(root, args):
    return subprocess.run(
        ["git", "-C", root] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=_GIT_ENV, timeout=_SUBPROCESS_TIMEOUT, text=True,
    )


def _git_check_ignore(root, rel_path):
    """True/False per `git check-ignore --no-index`, or None on an
    unexpected (non 0/1) outcome."""
    try:
        proc = _run_git(root, ["check-ignore", "--no-index", "-q", "--", rel_path])
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    return None


def _apply_git_exclusion(root):
    """Returns (status, detail). status is "none", "effective", "added", or
    "error"."""
    if shutil.which("git") is None:
        return "none", ""

    try:
        proc = _run_git(root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", "git rev-parse failed: %s" % exc
    if proc.returncode != 0:
        stderr = (proc.stderr or "").strip()
        if "not a git repository" in stderr.lower():
            return "none", ""
        return "error", "git rev-parse failed: %s" % stderr

    # D3: the generated rule anchors the skills child, not the whole of
    # .agents/. D5: --literal-pathspecs so a root path containing *, ?, or [
    # is never misread as a glob.
    tracked_rel = "%s/%s" % (AGENTS_DIRNAME, SKILLS_DIRNAME)
    try:
        proc = _run_git(root, ["--literal-pathspecs", "ls-files", "--", tracked_rel])
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", "git ls-files failed: %s" % exc
    if proc.returncode != 0:
        return "error", "git ls-files failed: %s" % (proc.stderr or "").strip()
    if (proc.stdout or "").strip():
        return "error", (
            "%s is tracked by git; delete it from the index before bootstrap "
            "can generate the ignore rule" % tracked_rel
        )

    # D4: the probe MUST use a trailing slash. Reproduced on Git 2.55.0:
    # `git check-ignore --no-index <path>` on a path that does not exist yet
    # returns 1 (not ignored) even under a matching directory rule, because
    # git cannot know an absent path is a directory. Exclusion is
    # established BEFORE .agents/skills is created, so a probe without the
    # slash would make first convergence impossible in a clean repo.
    probe_rel = "%s/%s/" % (AGENTS_DIRNAME, SKILLS_DIRNAME)
    already = _git_check_ignore(root, probe_rel)
    if already is None:
        return "error", "git check-ignore failed for %s" % probe_rel
    if already:
        return "effective", "Git exclusion already effective"

    try:
        proc = _run_git(root, ["rev-parse", "--git-path", "info/exclude"])
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", "git rev-parse --git-path failed: %s" % exc
    if proc.returncode != 0:
        return "error", "could not resolve info/exclude: %s" % (proc.stderr or "").strip()
    exclude_rel = (proc.stdout or "").strip()
    if not exclude_rel:
        return "error", "git rev-parse --git-path info/exclude returned no output"
    exclude_path = (
        exclude_rel if os.path.isabs(exclude_rel)
        else os.path.join(root, exclude_rel)
    )

    try:
        exclude_dir = os.path.dirname(exclude_path)
        if exclude_dir:
            os.makedirs(exclude_dir, exist_ok=True)
        existing = ""
        if os.path.isfile(exclude_path):
            with open(exclude_path, "r", encoding="utf-8") as f:
                existing = f.read()
        needs_newline = bool(existing) and not existing.endswith("\n")
        with open(exclude_path, "a", encoding="utf-8") as f:
            if needs_newline:
                f.write("\n")
            f.write("%s\n%s\n" % (_GIT_EXCLUDE_HEADER, _GENERATED_RULE))
    except OSError as exc:
        return "error", "could not write %s: %s" % (exclude_path, exc)

    now_ignored = _git_check_ignore(root, probe_rel)
    if not now_ignored:
        return "error", (
            "wrote %s but %s is still not ignored" % (exclude_path, probe_rel)
        )
    return "added", "Git exclusion added to %s" % exclude_path


def _run_p4(args, timeout=_SUBPROCESS_TIMEOUT):
    return subprocess.run(
        ["p4"] + list(args),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=timeout, text=True,
    )


def _p4_where_mapped(root, target_path):
    """True if `p4 where` reports target_path mapped, False if explicitly
    unmapped, None if the server could not be reached."""
    try:
        proc = _run_p4(["-ztag", "-d", root, "where", "%s/..." % target_path])
    except (OSError, subprocess.SubprocessError):
        return None
    combined = "%s\n%s" % (proc.stdout or "", proc.stderr or "")
    if "not in client view" in combined:
        return False
    if proc.returncode != 0 or "Perforce client error" in combined:
        return None
    return ("depotFile" in combined) or ("clientFile" in combined)


def _p4_local_evidence(root):
    """The name of the first local P4 evidence file found at or above root,
    or None. Checks an effective P4CONFIG filename first, then the
    documented default ignore filenames."""
    names = []
    p4config_name = os.environ.get("P4CONFIG")
    if p4config_name:
        names.append(p4config_name)
    names.extend((".p4ignore", "p4ignore.txt"))
    for name in names:
        if not os.path.isabs(name) and os.path.isfile(os.path.join(root, name)):
            return name
    return None


def _p4_ignores(root, path):
    """True/False per `p4 ignores -i -v`, or None on an unrecognized
    outcome. Documented as not requiring server access."""
    try:
        proc = _run_p4(["-d", root, "ignores", "-i", "-v", path])
    except (OSError, subprocess.SubprocessError):
        return None
    output = proc.stdout or ""
    if "not ignored" in output:
        return False
    if "ignored by" in output:
        return True
    return None


def _p4_set_p4ignore(root):
    """The explicit P4IGNORE filename list, or None when unset (defaults
    apply: .p4ignore then p4ignore.txt)."""
    try:
        proc = _run_p4(["-d", root, "set", "-q", "P4IGNORE"])
    except (OSError, subprocess.SubprocessError):
        return None
    output = (proc.stdout or "").strip()
    if not output:
        return None
    value = output.split("=", 1)[1] if "=" in output else output
    names = [n.strip() for n in value.replace(";", ",").split(",") if n.strip()]
    return names or None


def _apply_p4_exclusion(root):
    """Returns (status, detail). status is "none", "effective", "added", or
    "error"."""
    p4 = shutil.which("p4")
    agents_skills_path = os.path.join(root, AGENTS_DIRNAME, SKILLS_DIRNAME)

    workspace = False
    if p4 is not None:
        mapped = _p4_where_mapped(root, agents_skills_path)
        workspace = mapped  # True, False, or None (server unreachable)
    else:
        workspace = None

    if workspace is False:
        return "none", ""

    if workspace is None:
        # p4 unavailable, or the server could not be reached: fall back to
        # local evidence (an effective P4CONFIG file, a root/ancestor
        # .p4ignore or p4ignore.txt). Local evidence makes handling
        # conservative; no local evidence means no P4 action at all.
        evidence = _p4_local_evidence(root)
        if not evidence:
            return "none", ""
        if p4 is None:
            return "error", (
                "local Perforce evidence found (%s) but the p4 CLI is "
                "unavailable, so exclusion cannot be verified" % evidence
            )
        # p4 exists but the server is unreachable; `p4 ignores -i -v` is
        # documented to work without server access, so proceed using it.

    already = _p4_ignores(root, agents_skills_path)
    if already is None:
        return "error", "could not run p4 ignores -i -v for %s" % agents_skills_path
    if already:
        return "effective", "P4 exclusion already effective"

    explicit_names = _p4_set_p4ignore(root)
    candidates = explicit_names if explicit_names else [_P4_IGNORE_DEFAULT_NAME]

    target_name = None
    for name in candidates:
        candidate_path = os.path.abspath(os.path.join(root, name))
        try:
            inside = os.path.commonpath([os.path.abspath(root), candidate_path]) == os.path.abspath(root)
        except ValueError:
            inside = False
        if not inside:
            return "error", (
                "Perforce ignore candidate %s is outside the project root; "
                "no file was created" % name
            )
        if not os.path.exists(candidate_path):
            target_name = name
            break
    if target_name is None:
        return "error", (
            "no missing root-local Perforce ignore file is available to "
            "write (candidates already exist: %s) -- add %s to the "
            "configured ignore policy manually"
            % (", ".join(candidates), _GENERATED_RULE)
        )

    ignore_path = os.path.abspath(os.path.join(root, target_name))
    try:
        with open(ignore_path, "x", encoding="utf-8") as f:
            f.write("%s\n" % _GIT_EXCLUDE_HEADER)
            f.write("%s\n" % _GENERATED_RULE)
            f.write("/%s\n" % target_name)
    except FileExistsError:
        return "error", "%s was created concurrently" % ignore_path
    except OSError as exc:
        return "error", "could not create %s: %s" % (ignore_path, exc)

    git_status, git_detail = _apply_git_file_exclusion(root, "/%s" % target_name)
    if git_status == "error":
        return "error", git_detail

    ok_skills = _p4_ignores(root, agents_skills_path)
    ok_self = _p4_ignores(root, ignore_path)
    if not ok_skills or not ok_self:
        return "error", (
            "created %s but p4 ignores -i -v does not report both paths "
            "as ignored" % ignore_path
        )
    return "added", "P4 exclusion added to %s" % ignore_path


def _apply_git_file_exclusion(root, rule):
    """Exclude a generated file from Git when ``root`` is a Git repository."""
    try:
        proc = _run_git(root, ["rev-parse", "--show-toplevel"])
    except (OSError, subprocess.SubprocessError):
        return "error", "git rev-parse failed while excluding %s" % rule
    if proc.returncode != 0:
        stderr = (proc.stderr or "").lower()
        if "not a git repository" in stderr:
            return "none", ""
        return "error", "git rev-parse failed while excluding %s" % rule

    rel_path = rule.lstrip("/")
    already = _git_check_ignore(root, rel_path)
    if already is None:
        return "error", "git check-ignore failed for %s" % rel_path
    if already:
        return "effective", "Git exclusion already effective for %s" % rule

    try:
        proc = _run_git(root, ["rev-parse", "--git-path", "info/exclude"])
    except (OSError, subprocess.SubprocessError) as exc:
        return "error", "git rev-parse --git-path failed: %s" % exc
    if proc.returncode != 0:
        return "error", "could not resolve info/exclude: %s" % (proc.stderr or "").strip()
    exclude_rel = (proc.stdout or "").strip()
    if not exclude_rel:
        return "error", "git rev-parse --git-path info/exclude returned no output"
    exclude_path = exclude_rel if os.path.isabs(exclude_rel) else os.path.join(root, exclude_rel)
    try:
        exclude_dir = os.path.dirname(exclude_path)
        if exclude_dir:
            os.makedirs(exclude_dir, exist_ok=True)
        existing = ""
        if os.path.isfile(exclude_path):
            with open(exclude_path, "r", encoding="utf-8") as f:
                existing = f.read()
        with open(exclude_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("%s\n%s\n" % (_P4_GIT_EXCLUDE_HEADER, rule))
    except OSError as exc:
        return "error", "could not write %s: %s" % (exclude_path, exc)

    if not _git_check_ignore(root, rel_path):
        return "error", "wrote %s but %s is still not ignored" % (exclude_path, rel_path)
    return "added", "Git exclusion added to %s" % exclude_path
