"""Tests for the yaml-data-editor-kit dispatch import boundary."""

import os
from pathlib import Path
import subprocess
import sys
import textwrap


PLUGIN_LIB = Path(__file__).resolve().parents[2] / "plugins" / "yaml-data-editor-kit" / "lib"
CPK_LIB = Path(__file__).resolve().parents[2] / "plugins" / "content-pipeline-kit" / "lib"


def _python(code: str, *paths: Path) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONPATH"] = os.pathsep.join(str(path) for path in paths)
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_dispatch_exports_agentic_and_question_protocol_surfaces() -> None:
    result = _python(
        """
        from yaml_data_editor_kit.dispatch import (
            AgenticCommentPlanner,
            CommentPlanner,
            PlannerPolicy,
            QuestionProtocolError,
            encode_question_failure,
            build_dispatch_handlers,
            materialize_failed_questions,
            prepare_background_dispatch,
            run_background_wave,
            get_background_dispatch_status,
            finalize_background_dispatch,
        )

        assert AgenticCommentPlanner
        assert CommentPlanner
        assert PlannerPolicy
        assert QuestionProtocolError
        assert encode_question_failure
        assert build_dispatch_handlers
        assert materialize_failed_questions
        assert prepare_background_dispatch
        assert run_background_wave
        assert get_background_dispatch_status
        assert finalize_background_dispatch
        """,
        PLUGIN_LIB,
        CPK_LIB,
    )
    assert result.returncode == 0, result.stderr


def test_missing_content_pipeline_reports_the_bootstrap_action() -> None:
    result = _python(
        """
        import builtins

        original_import = builtins.__import__

        def missing_content_pipeline(name, *args, **kwargs):
            if name == "content_pipeline":
                raise ModuleNotFoundError(
                    "No module named 'content_pipeline'", name="content_pipeline"
                )
            return original_import(name, *args, **kwargs)

        builtins.__import__ = missing_content_pipeline
        try:
            import yaml_data_editor_kit.dispatch
        except ModuleNotFoundError as exc:
            message = str(exc)
            assert "bootstrap" in message.lower()
            assert "content_pipeline" in message
        else:
            raise AssertionError("dispatch import did not fail")
        """,
        PLUGIN_LIB,
    )
    assert result.returncode == 0, result.stderr


def test_nested_module_not_found_is_not_relabelled_as_missing_cpk() -> None:
    result = _python(
        """
        import builtins

        original_import = builtins.__import__

        def nested_failure(name, *args, **kwargs):
            if name == "content_pipeline":
                return original_import(name, *args, **kwargs)
            if name == "content_pipeline.freshness.hashing":
                raise ModuleNotFoundError(
                    "No module named 'content_pipeline.freshness.hashing'",
                    name="content_pipeline.freshness.hashing",
                )
            return original_import(name, *args, **kwargs)

        builtins.__import__ = nested_failure
        try:
            import yaml_data_editor_kit.dispatch
        except ModuleNotFoundError as exc:
            assert exc.name == "content_pipeline.freshness.hashing"
            assert "shared library" not in str(exc).lower()
        else:
            raise AssertionError("dispatch import did not fail")
        """,
        PLUGIN_LIB,
        CPK_LIB,
    )
    assert result.returncode == 0, result.stderr
