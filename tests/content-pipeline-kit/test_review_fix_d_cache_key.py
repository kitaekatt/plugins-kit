"""Response-cache keys are pinned to literal digests.

Every cached response on disk is addressed by ``build_cache_key``. A comparison
of the function with itself passes whatever the function does, so it cannot
notice a key change that makes an entire corpus miss and re-spend. These
literals were computed from build_cache_key as of content-pipeline-kit 0.27.0; a deliberate key
change must update them and say so.
"""

from pathlib import Path

import pytest

from content_pipeline.llm import backends
from content_pipeline.llm.backends import ModelEndpointBackend
from content_pipeline.llm.platform import BackendOptions, build_cache_key

MINIMAL_DIGEST = "963f763125a571547223cde37c92acaf745f0becfec00fd2912f8800574540b5"
FULL_DIGEST = "02898b111e18cf7fa3d2267b867dc3f8dfed755314058232eabf91be0249e179"


def test_minimal_key_matches_the_pinned_digest():
    key = build_cache_key(backend="mock", model="m", system="s", user="u")
    assert key == MINIMAL_DIGEST


def test_key_with_every_participating_field_matches_the_pinned_digest():
    options = BackendOptions(
        temperature=0.2,
        max_tokens=100,
        effort="high",
        allowed_tools=["Read"],
        extras={"b": 1, "a": 2},
        cwd="/w",
        cache_salt="salt",
        user_cache_prefix="pre",
    )
    key = build_cache_key(
        backend="claude-cli", model="opus", system="sys\n", user="hi", options=options
    )
    assert key == FULL_DIGEST


def test_deadline_and_client_id_do_not_move_the_pinned_digest():
    options = BackendOptions(timeout_s=5, client_id="run-1")
    key = build_cache_key(
        backend="mock", model="m", system="s", user="u", options=options
    )
    assert key == MINIMAL_DIGEST


# ---------------------------------------------------------------------------
# ModelEndpointBackend: the effective-options key moves exactly where the wire
# does.
#
# content-pipeline-kit 0.29.0 stopped injecting a style-blind top-level
# extras["reasoning_effort"] and began keying off the extra body the delegate
# actually sends (llm-scripting-kit's plan_effort). These literals were
# computed from content-pipeline-kit 0.28.1's ModelEndpointBackend
# .effective_options over entries declaring reasoning_effort: medium, before
# that change. 0.28.1 keyed every entry shape alike, so each literal is that
# release's key for one caller-options case on EVERY shape.
# ---------------------------------------------------------------------------

PRE_029_DIGESTS = {
    "nothing": "1ed3f93a5e0d51be00daac6a15e5fc0b1fdadf45873f61d24be4d9b720c1d01f",
    "extras-value": "88cc4405c6de94a9d14504bb6d874b61ce81374d7e75796c09027eee5d15c96a",
    "extras-null": "2560abcdc840809a8c02b788ab1a0b67651b2294d247d727e79045c7bf75f9ff",
    "effort+extras-value": "47f0fc1acfea5ceaea7deb1de61857809cbbe5f02f7692f597131d9e07dd7347",
    "effort+extras-null": "b744fb0085a6b22564c21e788f866dc1ace8d7283c518c13fbfb96779fe389ef",
    "effort": "f7e6cbe261195e8406719df73c765c76b47ee072de1d007683ff5794bec5c250",
}

_CASES = {
    "nothing": {},
    "extras-value": {"extras": {"reasoning_effort": "low"}},
    "extras-null": {"extras": {"reasoning_effort": None}},
    "effort+extras-value": {"effort": "high", "extras": {"reasoning_effort": "low"}},
    "effort+extras-null": {"effort": "high", "extras": {"reasoning_effort": None}},
    "effort": {"effort": "high"},
}

# Entry shapes of the fleet registry: qwen38-5090 (ninfer), qwen38-m5
# (chat_template_kwargs), qwen38 (front door), qwen38-m5-64k (no style).
_REGISTRY = (
    "models:\n"
    "  shape-ninfer:\n    base_url: http://ninfer.invalid/v1\n    model: ninfer-m\n"
    "    reasoning_effort: medium\n"
    "    routing: {group: shapes, order: 1, effort_style: ninfer}\n"
    "  shape-ctk:\n    base_url: http://ctk.invalid/v1\n    model: ctk-m\n"
    "    reasoning_effort: medium\n"
    "    routing: {group: shapes, order: 2, effort_style: chat_template_kwargs}\n"
    "  shape-frontdoor:\n    base_url: http://fd.invalid/v1\n    model: fd-m\n"
    "    reasoning_effort: medium\n    frontdoor: true\n"
    "  shape-nostyle:\n    base_url: http://plain.invalid/v1\n    model: plain-m\n"
    "    reasoning_effort: medium\n"
)

# Where the wire request stays byte-identical to 0.28.1's, the key must too.
# The wire changes for the default on a chat_template_kwargs entry (nested now)
# and on a no-style entry (not sent now), and for a caller effort with no
# explicit extras on every shape (0.28.1 ignored it and sent the default).
_UNCHANGED = {
    "shape-ninfer": {"nothing", "extras-value", "extras-null",
                     "effort+extras-value", "effort+extras-null"},
    "shape-frontdoor": {"nothing", "extras-value", "extras-null",
                        "effort+extras-value", "effort+extras-null"},
    "shape-ctk": {"extras-value", "extras-null",
                  "effort+extras-value", "effort+extras-null"},
    "shape-nostyle": {"extras-value", "extras-null",
                      "effort+extras-value", "effort+extras-null"},
}


@pytest.fixture
def shape_registry(tmp_path, monkeypatch):
    shared_lib = Path(__file__).resolve().parents[2] / "plugins" / "llm-scripting-kit" / "lib"
    monkeypatch.syspath_prepend(str(shared_lib))
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    path = tmp_path / "model-endpoints.yaml"
    path.write_text(_REGISTRY, encoding="utf-8")
    monkeypatch.setenv("MODEL_ENDPOINTS_REGISTRY", str(path))
    return tmp_path


def _key(entry, root, case):
    b = ModelEndpointBackend(endpoint=entry, project_root=root)
    return build_cache_key(
        backend=b.name, model="m", system="s", user="u",
        options=b.effective_options(BackendOptions(**_CASES[case])),
    )


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.parametrize("entry", sorted(_UNCHANGED))
def test_model_endpoint_key_moves_exactly_where_the_wire_does(shape_registry, entry, case):
    assert backends._effort_seam() is not None, "the new-seam branch must be under test"
    key = _key(entry, shape_registry, case)
    if case in _UNCHANGED[entry]:
        assert key == PRE_029_DIGESTS[case]
    else:
        assert key != PRE_029_DIGESTS[case]


@pytest.mark.parametrize("case", sorted(_CASES))
@pytest.mark.parametrize("entry", sorted(_UNCHANGED))
def test_legacy_fallback_keeps_every_pre_029_key(shape_registry, monkeypatch, entry, case):
    """Against a shared lib predating plan_effort the old injection -- and so
    the old key -- stands on every shape."""
    monkeypatch.setattr(backends, "_effort_seam", lambda: None)
    assert _key(entry, shape_registry, case) == PRE_029_DIGESTS[case]
