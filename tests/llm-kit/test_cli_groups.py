"""Legacy CLI verbs retain their homes in the installed library's groups.

Native CLI routing tests live in katsoft-libs; these comparisons need the frozen
plugin CLIs and therefore remain in the consumer integration repository.
"""
from typing import Any
from test_cli_harness import final_mods, home, legacy_mods, run

def _choices(parser: Any) -> set[str]:
    for action in parser._actions:
        if action.__class__.__name__ == "_SubParsersAction":
            return set(action.choices)
    raise AssertionError("parser has no subcommands")

class TestVerbMap:

    def test_every_scripting_verb_has_exactly_one_home(self, legacy_mods, final_mods):
            legacy = _choices(legacy_mods.cli._parser())
            job_kit_verbs = {"run", "resume", "events", "gc"}  # job-kit's, not llm-scripting-kit's
            mapped = (set(final_mods.cli.LEGACY_VERBS) - job_kit_verbs) | {"frontdoor"}
            assert legacy == mapped, (legacy ^ mapped)

    def test_the_jobs_group_carries_the_job_kit_verbs_and_cancel(self, legacy_mods, final_mods, capsys):
            legacy = _choices(legacy_mods.jobs_cli._parser())
            assert legacy == {"run", "status", "resume", "resolve", "events", "gc"}
            for verb in legacy:
                assert run(final_mods.cli.main, ["jobs", verb, "--help"], capsys).code == 0, verb
            # Deliberate difference (ledger schema 13): llm-kit adds `cancel`; the
            # legacy verbs are all still there.
            assert _choices(final_mods.jobs_cli._parser()) == legacy | {"cancel"}
            assert run(final_mods.cli.main, ["jobs", "cancel", "--help"], capsys).code == 0

    def test_the_pipeline_group_carries_the_store_only_command_set(self, legacy_mods, final_mods, tmp_path):
            store = legacy_mods.cpk_store.ExecutionStore(tmp_path / "legacy.db")
            legacy = set(legacy_mods.cpk_run.build_commands(store))
            final = set(final_mods.run.build_commands(final_mods.exec_store.ExecutionStore(tmp_path / "final.db")))
            assert legacy == final == set(final_mods.pipeline._COMMANDS)

    def test_swapper_and_acceptance_subverbs_are_unchanged(self, legacy_mods, final_mods):
            def nested(parser: Any, name: str) -> set[str]:
                for action in parser._actions:
                    if action.__class__.__name__ == "_SubParsersAction":
                        return _choices(action.choices[name])
                raise AssertionError(name)

            legacy = legacy_mods.cli._parser()
            final = final_mods.local._parser()
            assert nested(final, "swapper") == nested(legacy, "swapper")
            assert nested(final, "acceptance") == nested(legacy, "acceptance")
            assert nested(final, "swapper") == {"running", "terminate", "unload", "strays", "terminate-listener",
                                                "guard-launch"}
            assert nested(final, "acceptance") == {"swapper", "frontdoor"}
