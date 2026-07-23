"""Shared fixtures for the skills-kit schema validation suite.

Each fixture returns a *minimal floor* satisfying the type's schema. Tests then
mutate one field at a time and re-validate, confirming the schema catches each
class of drift. Mutation-driven tests are clearer than building bad fixtures
ad-hoc -- the diff between the floor and the failure case names exactly what
the schema requires.
"""

import copy

import pytest


def _kw(*words: str) -> list:
    """Helper: produce a >=3-keyword list."""
    return [*words] if len(words) >= 3 else [*words, "extra1", "extra2", "extra3"][:max(3, len(words))]


@pytest.fixture
def minimal_reference_skill():
    """Minimal valid reference_skill: 1 fact carrying both gotcha and example."""
    return {
        "reference_skill": {
            "_schema_version": "1",
            "identity": "Reference for thing X.",
            "scope": {
                "covers": ["topic A"],
                "excludes": ["topic B"],
            },
            "facts": [
                {
                    "id": "f1",
                    "summary": "Thing X is Y.",
                    "keywords": _kw("thing x", "is y", "fact"),
                    "detail": "Thing X works because Y. The detail.",
                    "gotchas": ["When Z, X breaks."],
                    "example": {
                        "input": "do X",
                        "output": "see Y",
                    },
                },
            ],
        }
    }


@pytest.fixture
def minimal_pattern_skill():
    """Minimal valid pattern_skill: 1 pattern with all required sub-records."""
    return {
        "pattern_skill": {
            "_schema_version": "1",
            "identity": "Pattern for problem X.",
            "scope": {
                "covers": ["X-shaped problems"],
                "excludes": ["Y-shaped problems"],
            },
            "patterns": [
                {
                    "id": "p1",
                    "name": "Pattern P",
                    "keywords": _kw("pattern", "p", "x problem"),
                    "problem": "When X happens",
                    "mechanic": "Apply M to resolve.",
                    "why": "Because M closes the X-shaped gap.",
                    "apply_when": [
                        {"signal": "X is observed", "example": "see input file F"},
                    ],
                    "do_not_apply_when": [
                        {"signal": "X is illusory", "counter_example": "see input file G"},
                    ],
                    "examples": [
                        {"title": "Worked", "before": "before state", "after": "after state"},
                    ],
                },
            ],
        }
    }


@pytest.fixture
def minimal_technique_skill():
    """Minimal valid technique_skill: 1 technique with steps + gotchas."""
    return {
        "technique_skill": {
            "_schema_version": "1",
            "identity": "Technique for procedure X.",
            "scope": {
                "covers": ["X procedure"],
                "excludes": ["Y procedure"],
            },
            "techniques": [
                {
                    "id": "t1",
                    "name": "Technique T",
                    "keywords": _kw("technique", "t", "do x"),
                    "goal": "Accomplish X.",
                    "steps": [
                        {"n": 1, "action": "Do A.", "expected": "A is done."},
                    ],
                    "gotchas": ["Watch out for Z."],
                },
            ],
        }
    }


@pytest.fixture
def minimal_discipline_skill():
    """Minimal valid discipline_skill: 1 rule with counter, red flags, pressure_test."""
    return {
        "discipline_skill": {
            "_schema_version": "1",
            "identity": "Discipline enforcing rule X.",
            "scope": {
                "covers": ["X enforcement"],
                "excludes": ["unrelated rules"],
            },
            "target": {
                "type": "technique",
                "ref": "../some-technique/SKILL.md",
            },
            "rules": [
                {
                    "id": "r1",
                    "keywords": _kw("rule", "must do x", "discipline"),
                    "statement": "Do X before Y.",
                    "why": "Because Y depends on X being done first.",
                    "counters": [
                        {
                            "excuse": "It's faster to skip X.",
                            "reality": "Skipping X costs more downstream.",
                            "observed_in": "baseline run B-1",
                        },
                    ],
                    "red_flags": ["just this once", "X is too small"],
                },
            ],
            "pressure_test": {
                "baseline": "B-1: agent skipped X under time pressure.",
                "green": "After skill installed, agent did X.",
                "refactor": [
                    {
                        "loophole": "Agent claimed X was implicit.",
                        "closed_by": "Added counter requiring explicit X invocation.",
                    },
                ],
            },
        }
    }


@pytest.fixture
def minimal_domain_skill():
    """Minimal valid domain_skill: companions + scope + orientation + index."""
    return {
        "domain_skill": {
            "_schema_version": "1",
            "identity": "Domain for area X.",
            "companions": {
                "siblings": [],
                "note": "no siblings",
            },
            "scope": {
                "covers": ["X area"],
                "excludes": ["Y area"],
            },
            "orientation": {
                "summary": "X has these sub-areas: A, B, C.",
            },
            "index": {
                "references": [
                    {
                        "id": "ref1",
                        "path": "references/x.md",
                        "keywords": _kw("x reference", "details", "lookup"),
                        "summary": "Details about X.",
                    },
                ],
            },
        }
    }


@pytest.fixture
def minimal_capability_skill():
    """Minimal valid capability_skill: 1 capability + external_capability + layering + 1 gotcha."""
    return {
        "capability_skill": {
            "_schema_version": "1",
            "identity": "Capability-skill wrapping external thing X.",
            "scope": {
                "covers": ["X operations in this project"],
                "excludes": ["unrelated tooling"],
            },
            "external_capability": {
                "kind": "tool",
                "name": "X CLI",
                "description": "X provides operations Y and Z.",
            },
            "layering": {
                "claude_md": [],
                "skill_md": ["orientation paragraph", "capability list"],
                "references": [],
            },
            "capabilities": [
                {
                    "id": "c1",
                    "keywords": _kw("operation y", "x cli", "do y"),
                    "user_objective": "Accomplish Y in the project workflow.",
                    "operation": "x do-y --target=foo",
                },
            ],
            "gotchas": [
                "X CLI swallows stderr on Windows; redirect with 2>&1.",
            ],
        }
    }


@pytest.fixture
def minimal_claude_md():
    """Minimal valid claude_md: scope + 1 insight with all required fields."""
    return {
        "claude_md": {
            "_schema_version": "1",
            "scope": {
                "directory": "some/dir",
                "covers": ["x"],
            },
            "insights": [
                {
                    "id": "i1",
                    "keywords": _kw("insight", "i1", "context"),
                    "summary": "Insight about X.",
                    "detail": "Long detail about X.",
                    "origin": "Session 2026-04-30 surface review.",
                    "added": "2026-04-30",
                },
            ],
        }
    }


@pytest.fixture
def minimal_standards_set():
    """Minimal valid standards_set: 1 criterion with all required fields."""
    return {
        "standards_set": {
            "identity": "Optional description-hygiene standards for SKILL.md files.",
            "applies_to": "skill_md",
            "criteria": [
                {
                    "id": "desc-160-char",
                    "statement": "The description frontmatter field is at most 160 characters.",
                    "severity": "fail",
                    "keywords": _kw("description length", "160 char", "hygiene"),
                },
            ],
        }
    }


@pytest.fixture
def make_invalid():
    """Helper: deep-copy a fixture and apply a mutation, return the mutated copy.

    Usage:
        bad = make_invalid(minimal_reference_skill, lambda d: d['reference_skill'].pop('facts'))
    """
    def _mutate(source, mutator):
        result = copy.deepcopy(source)
        mutator(result)
        return result
    return _mutate
