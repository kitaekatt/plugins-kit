"""Deprecation shims for the audit.py names the shipped-standards migration removed.

docs/reference/deprecation-policy.md: a removed or renamed public name keeps a
shim that emits DeprecationWarning for two minor versions. Each shim here keeps
its former signature and returns what the former function returned. EXPECTED
holds the rows the ORIGINAL functions (skills-kit 0.92.1, commit efd43c8b)
returned for these fixtures, captured by running that source; the shims must
reproduce them row for row.
"""

from __future__ import annotations

import ast
import json
import re
import warnings
from pathlib import Path

import pytest

from skills_kit_lib import audit as audit_mod
from skills_kit_lib.audit import (
    HAVE_YAML,
    THRESHOLDS,
    MechanicalCriterion,
    audit,
    check_claude_md_defaults,
    check_claude_md_record_floor,
    check_discipline_skill,
    check_domain_skill,
    check_facts_cross_rules,
    check_pattern_skill,
    check_reference_skill,
    check_technique_caution_cross_rule,
    check_technique_skill,
    mechanical_criteria,
    parse_body,
    skill_md_criteria,
)
from skills_kit_lib.markdown_heuristics import Frontmatter

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "skills-kit"
CONTRACT = json.loads((PLUGIN / "contract" / "public-surface.json").read_text(encoding="utf-8"))
ENTRIES = CONTRACT["entries"]
VERSION = tuple(
    int(p) for p in json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"].split(".")
)

needs_yaml = pytest.mark.skipif(not HAVE_YAML, reason="the YAML contract floors have no rows without pyyaml")

SNIP = {
    "example": "## Example\nx\n",
    "gotcha": "## Gotchas\ny\n",
    "recog": "Recognize this when X.\n",
    "counter": "Counter-example: do NOT apply when Y.\n",
    "tick": "- [ ] a\n- [ ] b\n",
    "rgr": "RED then GREEN then REFACTOR\n",
    "redflags": "## Red flags\n- z\n",
    "cond": "## Conditional Loading\n- r\n",
    "companion": "## Companion declaration\nno sibling\n",
    "excuse": "| Excuse | Reality |\n|---|---|\n| a | b |\n",
    "steps5": "1. a\n2. b\n3. c\n4. d\n5. e\n",
    "steps2": "1. a\n2. b\n",
    "tracker": "Use TaskCreate to track.\n",
    "h2a": "## Alpha\nq\n",
    "h2b": "## Beta\nq\n",
    "ident": "# Title\n\nThis skill does a thing.\n\n",
    "noident": "# Title\n\n",
}

# name -> (snippet keys joined in order, skill dir carries scripts/)
TYPE_FIXTURES = {
    "reference-skill": {
        "empty": ((), False),
        "example_gotcha": (("example", "gotcha"), False),
        "example_only": (("example",), False),
        "checklist_gotcha": (("gotcha", "tick"), False),
        "discipline_markers": (("example", "gotcha", "rgr", "excuse"), False),
    },
    "pattern-skill": {
        "empty": ((), False),
        "recog_counter": (("recog", "counter"), False),
        "recog_counter_bundle": (("recog", "counter"), True),
        "checklist_excuse": (("tick", "excuse"), False),
    },
    "technique-skill": {
        "empty": ((), False),
        "two_steps": (("steps2",), False),
        "five_steps_bare": (("steps5",), False),
        "five_steps_checklist": (("steps5", "tick"), False),
        "five_steps_tracker": (("steps5", "tracker"), False),
        "two_steps_pressure": (("steps2", "rgr"), False),
    },
    "discipline-skill": {
        "empty": ((), False),
        "full": (("excuse", "redflags", "rgr"), False),
        "no_pressure": (("excuse", "redflags"), False),
        "pressure_only": (("rgr",), False),
    },
    "domain-skill": {
        "empty": ((), False),
        "complete": (("ident", "companion", "h2a", "h2b", "cond"), False),
        "index_only": (("noident", "cond"), False),
        "one_h2_no_index": (("ident", "h2a"), False),
    },
}
SHIMS = {
    "reference-skill": check_reference_skill,
    "pattern-skill": check_pattern_skill,
    "technique-skill": check_technique_skill,
    "discipline-skill": check_discipline_skill,
    "domain-skill": check_domain_skill,
}

REF_FACTS = """```yaml
reference_skill:
  _schema_version: "1"
  identity: x
  facts:
    - id: a
      summary: s
      keywords: [a, b, c]
      detail: d
      {gotchas}
      {example}
```
"""
TOP_FACTS = """```yaml
facts:
  - id: a
    summary: s
    keywords: [a, b, c]
    detail: d
    {gotchas}
    {example}
```
"""
TECH = """```yaml
technique_skill:
  _schema_version: "1"
  identity: x
  techniques:
    - id: t
      {technique_line}
  {extra}
```
"""
GOTCHAS = "gotchas: [g1]"
EXAMPLE = "example: {input: i, output: o}"
# name -> (body text, declared type, yaml root)
FACTS_FIXTURES = {
    "nested_full": (REF_FACTS.format(gotchas=GOTCHAS, example=EXAMPLE), "reference-skill", None),
    "nested_bare": (REF_FACTS.format(gotchas="", example=""), "reference-skill", None),
    "top_level_gotcha": (TOP_FACTS.format(gotchas=GOTCHAS, example=""), "reference-skill", None),
    "no_facts": ("```yaml\nreference_skill:\n  facts: []\n```\n", "reference-skill", None),
    "root_wins_over_tag": (REF_FACTS.format(gotchas=GOTCHAS, example=EXAMPLE), "pattern-skill", "reference_skill"),
    "root_gates_out": (REF_FACTS.format(gotchas=GOTCHAS, example=EXAMPLE), "reference-skill", "technique_skill"),
    "other_type": (REF_FACTS.format(gotchas=GOTCHAS, example=EXAMPLE), "pattern-skill", None),
    "no_yaml": ("plain text\n", "reference-skill", None),
}
CAUTION_FIXTURES = {
    "technique_gotcha": TECH.format(technique_line="gotchas: [x]", extra="other: 1"),
    "anti_pattern_only": TECH.format(technique_line="summary: s", extra="anti_patterns: [{id: a}]"),
    "no_caution": TECH.format(technique_line="summary: s", extra="other: 1"),
    "empty_anti_patterns": TECH.format(technique_line="summary: s", extra="anti_patterns: []"),
    "no_technique_unit": "```yaml\nreference_skill:\n  identity: x\n```\n",
}
RECORD_FLOOR_FIXTURES = {
    "no_block": {},
    "empty_block": {"claude_md": {}},
    "insights_only": {"claude_md": {"insights": [1, 2]}},
    "conventions_only": {"claude_md": {"conventions": [1]}},
    "both": {"claude_md": {"insights": [1], "conventions": [2]}},
    "block_not_a_mapping": {"claude_md": "text"},
    "lists_not_lists": {"claude_md": {"insights": "x", "conventions": "y"}},
}
# name -> (CLAUDE.md text, th overrides)
DEFAULTS_FIXTURES = {
    "clean": ("# T\n\nbody\n", {}),
    "broken_link": ("# T\n\n[x](missing.md) and [y](ok.md)\n", {}),
    "unclosed_fence": ("# T\n\n```\nopen\n", {}),
    "import_over_limit": ("# T\n\n@big.md\n@sub/small.md\n", {}),
    "import_over_tight_limit": ("# T\n\n@sub/small.md\n", {"import_max_lines": 3}),
    "import_missing": ("# T\n\n@gone.md\n", {}),
    "fenced_import_ignored": ("# T\n\n```\n@big.md\n[x](zz.md)\n```\n", {}),
    "oversized_body": ("# T\n\n" + "word " * 4000 + "\n", {}),
    "tight_body_limits": ("# T\n\n" + "l\n" * 30, {"body_max_lines": 10, "body_max_tokens": 50}),
}

# BEGIN-EXPECTED (rows as (row, verdict, note, rule), captured from the original functions)
EXPECTED: dict = {'type': {'reference-skill': {'empty': [('>=1 example block',
                                         'judgment-required',
                                         "no 'Example' heading detected",
                                         'ref-example-block'),
                                        ('>=1 gotcha block',
                                         'judgment-required',
                                         "no 'Gotcha' heading detected",
                                         'ref-gotcha-block'),
                                        ('prohibited: discipline content (rule+counter, RED/GREEN/REFACTOR)',
                                         'pass',
                                         '',
                                         'ref-prohibited-discipline'),
                                        ('prohibited: workflow checklist',
                                         'pass',
                                         '',
                                         'ref-prohibited-checklist')],
                              'example_gotcha': [('>=1 example block', 'pass', '', 'ref-example-block'),
                                                 ('>=1 gotcha block', 'pass', '', 'ref-gotcha-block'),
                                                 ('prohibited: discipline content (rule+counter, '
                                                  'RED/GREEN/REFACTOR)',
                                                  'pass',
                                                  '',
                                                  'ref-prohibited-discipline'),
                                                 ('prohibited: workflow checklist',
                                                  'pass',
                                                  '',
                                                  'ref-prohibited-checklist')],
                              'example_only': [('>=1 example block', 'pass', '', 'ref-example-block'),
                                               ('>=1 gotcha block',
                                                'judgment-required',
                                                "no 'Gotcha' heading detected",
                                                'ref-gotcha-block'),
                                               ('prohibited: discipline content (rule+counter, '
                                                'RED/GREEN/REFACTOR)',
                                                'pass',
                                                '',
                                                'ref-prohibited-discipline'),
                                               ('prohibited: workflow checklist',
                                                'pass',
                                                '',
                                                'ref-prohibited-checklist')],
                              'checklist_gotcha': [('>=1 example block',
                                                    'judgment-required',
                                                    "no 'Example' heading detected",
                                                    'ref-example-block'),
                                                   ('>=1 gotcha block', 'pass', '', 'ref-gotcha-block'),
                                                   ('prohibited: discipline content (rule+counter, '
                                                    'RED/GREEN/REFACTOR)',
                                                    'pass',
                                                    '',
                                                    'ref-prohibited-discipline'),
                                                   ('prohibited: workflow checklist',
                                                    'fail',
                                                    'tickbox list present',
                                                    'ref-prohibited-checklist')],
                              'discipline_markers': [('>=1 example block', 'pass', '', 'ref-example-block'),
                                                     ('>=1 gotcha block', 'pass', '', 'ref-gotcha-block'),
                                                     ('prohibited: discipline content (rule+counter, '
                                                      'RED/GREEN/REFACTOR)',
                                                      'fail',
                                                      'discipline markers detected',
                                                      'ref-prohibited-discipline'),
                                                     ('prohibited: workflow checklist',
                                                      'pass',
                                                      '',
                                                      'ref-prohibited-checklist')]},
          'pattern-skill': {'empty': [('recognition criteria block',
                                       'judgment-required',
                                       "no 'recognize/recognition/applies when' marker",
                                       'pattern-recognition-block'),
                                      ('counter-example(s) block',
                                       'judgment-required',
                                       "no 'counter-example' or 'do NOT apply' marker",
                                       'pattern-counter-example'),
                                      ('prohibited: utility bundle', 'pass', '', 'pattern-prohibited-bundle'),
                                      ('prohibited: workflow checklist',
                                       'pass',
                                       '',
                                       'pattern-prohibited-checklist'),
                                      ('prohibited: rule + counter pairs',
                                       'pass',
                                       '',
                                       'pattern-prohibited-rule-counter')],
                            'recog_counter': [('recognition criteria block',
                                               'pass',
                                               '',
                                               'pattern-recognition-block'),
                                              ('counter-example(s) block',
                                               'pass',
                                               '',
                                               'pattern-counter-example'),
                                              ('prohibited: utility bundle',
                                               'pass',
                                               '',
                                               'pattern-prohibited-bundle'),
                                              ('prohibited: workflow checklist',
                                               'pass',
                                               '',
                                               'pattern-prohibited-checklist'),
                                              ('prohibited: rule + counter pairs',
                                               'pass',
                                               '',
                                               'pattern-prohibited-rule-counter')],
                            'recog_counter_bundle': [('recognition criteria block',
                                                      'pass',
                                                      '',
                                                      'pattern-recognition-block'),
                                                     ('counter-example(s) block',
                                                      'pass',
                                                      '',
                                                      'pattern-counter-example'),
                                                     ('prohibited: utility bundle',
                                                      'fail',
                                                      'scripts/ or bin/ present',
                                                      'pattern-prohibited-bundle'),
                                                     ('prohibited: workflow checklist',
                                                      'pass',
                                                      '',
                                                      'pattern-prohibited-checklist'),
                                                     ('prohibited: rule + counter pairs',
                                                      'pass',
                                                      '',
                                                      'pattern-prohibited-rule-counter')],
                            'checklist_excuse': [('recognition criteria block',
                                                  'judgment-required',
                                                  "no 'recognize/recognition/applies when' marker",
                                                  'pattern-recognition-block'),
                                                 ('counter-example(s) block',
                                                  'judgment-required',
                                                  "no 'counter-example' or 'do NOT apply' marker",
                                                  'pattern-counter-example'),
                                                 ('prohibited: utility bundle',
                                                  'pass',
                                                  '',
                                                  'pattern-prohibited-bundle'),
                                                 ('prohibited: workflow checklist',
                                                  'fail',
                                                  '',
                                                  'pattern-prohibited-checklist'),
                                                 ('prohibited: rule + counter pairs',
                                                  'fail',
                                                  'rationalization/excuse->reality detected',
                                                  'pattern-prohibited-rule-counter')]},
          'technique-skill': {'empty': [('ordered-step body',
                                         'fail',
                                         '0 ordered-step entries detected',
                                         'technique-ordered-steps'),
                                        ('explicit step-tracking (conditional, IF >3 steps): checklist OR '
                                         'tracker invocation',
                                         'n/a',
                                         'only 0 steps',
                                         'step-tracking'),
                                        ('prohibited: adversarial pressure testing',
                                         'pass',
                                         '',
                                         'technique-prohibited-pressure-test')],
                              'two_steps': [('ordered-step body',
                                             'pass',
                                             '2 ordered-step entries detected',
                                             'technique-ordered-steps'),
                                            ('explicit step-tracking (conditional, IF >3 steps): checklist '
                                             'OR tracker invocation',
                                             'n/a',
                                             'only 2 steps',
                                             'step-tracking'),
                                            ('prohibited: adversarial pressure testing',
                                             'pass',
                                             '',
                                             'technique-prohibited-pressure-test')],
                              'five_steps_bare': [('ordered-step body',
                                                   'pass',
                                                   '5 ordered-step entries detected',
                                                   'technique-ordered-steps'),
                                                  ('explicit step-tracking (conditional, IF >3 steps): '
                                                   'checklist OR tracker invocation',
                                                   'fail',
                                                   '5 steps; neither tickbox checklist nor step-tracker '
                                                   'invocation present',
                                                   'step-tracking'),
                                                  ('prohibited: adversarial pressure testing',
                                                   'pass',
                                                   '',
                                                   'technique-prohibited-pressure-test')],
                              'five_steps_checklist': [('ordered-step body',
                                                        'pass',
                                                        '5 ordered-step entries detected',
                                                        'technique-ordered-steps'),
                                                       ('explicit step-tracking (conditional, IF >3 steps): '
                                                        'checklist OR tracker invocation',
                                                        'pass',
                                                        '5 steps; satisfied via tickbox checklist',
                                                        'step-tracking'),
                                                       ('prohibited: adversarial pressure testing',
                                                        'pass',
                                                        '',
                                                        'technique-prohibited-pressure-test')],
                              'five_steps_tracker': [('ordered-step body',
                                                      'pass',
                                                      '5 ordered-step entries detected',
                                                      'technique-ordered-steps'),
                                                     ('explicit step-tracking (conditional, IF >3 steps): '
                                                      'checklist OR tracker invocation',
                                                      'pass',
                                                      '5 steps; satisfied via step-tracker invocation',
                                                      'step-tracking'),
                                                     ('prohibited: adversarial pressure testing',
                                                      'pass',
                                                      '',
                                                      'technique-prohibited-pressure-test')],
                              'two_steps_pressure': [('ordered-step body',
                                                      'pass',
                                                      '2 ordered-step entries detected',
                                                      'technique-ordered-steps'),
                                                     ('explicit step-tracking (conditional, IF >3 steps): '
                                                      'checklist OR tracker invocation',
                                                      'n/a',
                                                      'only 2 steps',
                                                      'step-tracking'),
                                                     ('prohibited: adversarial pressure testing',
                                                      'pass',
                                                      '',
                                                      'technique-prohibited-pressure-test')]},
          'discipline-skill': {'empty': [('>=1 rule + counter pair',
                                          'fail',
                                          'no rule+counter / rationalization markers',
                                          'discipline-rule-counter'),
                                         ('red flags list',
                                          'fail',
                                          "no 'Red flags' heading",
                                          'discipline-red-flags'),
                                         ('adversarial pressure testing applied',
                                          'fail',
                                          'no RED/GREEN/REFACTOR markers',
                                          'discipline-pressure-test')],
                               'full': [('>=1 rule + counter pair', 'pass', '', 'discipline-rule-counter'),
                                        ('red flags list', 'pass', '', 'discipline-red-flags'),
                                        ('adversarial pressure testing applied',
                                         'fail',
                                         'no RED/GREEN/REFACTOR markers',
                                         'discipline-pressure-test')],
                               'no_pressure': [('>=1 rule + counter pair',
                                                'pass',
                                                '',
                                                'discipline-rule-counter'),
                                               ('red flags list', 'pass', '', 'discipline-red-flags'),
                                               ('adversarial pressure testing applied',
                                                'fail',
                                                'no RED/GREEN/REFACTOR markers',
                                                'discipline-pressure-test')],
                               'pressure_only': [('>=1 rule + counter pair',
                                                  'fail',
                                                  'no rule+counter / rationalization markers',
                                                  'discipline-rule-counter'),
                                                 ('red flags list',
                                                  'fail',
                                                  "no 'Red flags' heading",
                                                  'discipline-red-flags'),
                                                 ('adversarial pressure testing applied',
                                                  'fail',
                                                  'no RED/GREEN/REFACTOR markers',
                                                  'discipline-pressure-test')]},
          'domain-skill': {'empty': [('identity sentence',
                                      'judgment-required',
                                      'no clear single-sentence identity after H1',
                                      'domain-identity-sentence'),
                                     ('companion declaration',
                                      'fail',
                                      "no 'Companion declaration' heading or 'no sibling' / 'companion "
                                      "domains' phrase",
                                      'domain-companion-declaration'),
                                     ('orientation content (>=1 H2 beyond index)',
                                      'fail',
                                      '0 H2 sections',
                                      'domain-orientation'),
                                     ('reference index (Conditional Loading)',
                                      'fail',
                                      '',
                                      'domain-reference-index'),
                                     ('prohibited: index without orientation',
                                      'pass',
                                      '',
                                      'domain-prohibited-index-only')],
                           'complete': [('identity sentence', 'pass', '', 'domain-identity-sentence'),
                                        ('companion declaration', 'pass', '', 'domain-companion-declaration'),
                                        ('orientation content (>=1 H2 beyond index)',
                                         'pass',
                                         '4 H2 sections',
                                         'domain-orientation'),
                                        ('reference index (Conditional Loading)',
                                         'pass',
                                         '',
                                         'domain-reference-index'),
                                        ('prohibited: index without orientation',
                                         'pass',
                                         '',
                                         'domain-prohibited-index-only')],
                           'index_only': [('identity sentence',
                                           'judgment-required',
                                           'no clear single-sentence identity after H1',
                                           'domain-identity-sentence'),
                                          ('companion declaration',
                                           'fail',
                                           "no 'Companion declaration' heading or 'no sibling' / 'companion "
                                           "domains' phrase",
                                           'domain-companion-declaration'),
                                          ('orientation content (>=1 H2 beyond index)',
                                           'fail',
                                           '1 H2 sections',
                                           'domain-orientation'),
                                          ('reference index (Conditional Loading)',
                                           'pass',
                                           '',
                                           'domain-reference-index'),
                                          ('prohibited: index without orientation',
                                           'fail',
                                           'only Conditional Loading H2; no orientation content',
                                           'domain-prohibited-index-only')],
                           'one_h2_no_index': [('identity sentence', 'pass', '', 'domain-identity-sentence'),
                                               ('companion declaration',
                                                'fail',
                                                "no 'Companion declaration' heading or 'no sibling' / "
                                                "'companion domains' phrase",
                                                'domain-companion-declaration'),
                                               ('orientation content (>=1 H2 beyond index)',
                                                'fail',
                                                '1 H2 sections',
                                                'domain-orientation'),
                                               ('reference index (Conditional Loading)',
                                                'fail',
                                                '',
                                                'domain-reference-index'),
                                               ('prohibited: index without orientation',
                                                'pass',
                                                '',
                                                'domain-prohibited-index-only')]}},
 'facts': {'nested_full': [('yaml: facts present (cross-source)',
                            'pass',
                            '1 facts across all sources',
                            'facts-floor'),
                           ('yaml: >=1 fact carries gotchas (cross-source)', 'pass', '', 'facts-gotcha'),
                           ('yaml: >=1 fact carries example (cross-source)', 'pass', '', 'facts-example')],
           'nested_bare': [('yaml: facts present (cross-source)',
                            'pass',
                            '1 facts across all sources',
                            'facts-floor'),
                           ('yaml: >=1 fact carries gotchas (cross-source)',
                            'fail',
                            'no fact carries a gotchas list across any source',
                            'facts-gotcha'),
                           ('yaml: >=1 fact carries example (cross-source)',
                            'fail',
                            'no fact carries an example block across any source',
                            'facts-example')],
           'top_level_gotcha': [('yaml: facts present (cross-source)',
                                 'pass',
                                 '1 facts across all sources',
                                 'facts-floor'),
                                ('yaml: >=1 fact carries gotchas (cross-source)', 'pass', '', 'facts-gotcha'),
                                ('yaml: >=1 fact carries example (cross-source)',
                                 'fail',
                                 'no fact carries an example block across any source',
                                 'facts-example')],
           'no_facts': [('yaml: facts present (cross-source)',
                         'fail',
                         'reference-skill requires at least one fact (nested in reference_skill: or as a '
                         'top-level facts: unit, or both)',
                         'facts-floor')],
           'root_wins_over_tag': [('yaml: facts present (cross-source)',
                                   'pass',
                                   '1 facts across all sources',
                                   'facts-floor'),
                                  ('yaml: >=1 fact carries gotchas (cross-source)',
                                   'pass',
                                   '',
                                   'facts-gotcha'),
                                  ('yaml: >=1 fact carries example (cross-source)',
                                   'pass',
                                   '',
                                   'facts-example')],
           'root_gates_out': [],
           'other_type': [],
           'no_yaml': [('yaml: facts present (cross-source)',
                        'fail',
                        'reference-skill requires at least one fact (nested in reference_skill: or as a '
                        'top-level facts: unit, or both)',
                        'facts-floor')]},
 'caution': {'technique_gotcha': [('yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule)',
                                   'pass',
                                   '',
                                   'caution-floor')],
             'anti_pattern_only': [('yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule)',
                                    'pass',
                                    '',
                                    'caution-floor')],
             'no_caution': [('yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule)',
                             'fail',
                             'no technique carries a gotchas list and no anti_patterns record exists',
                             'caution-floor')],
             'empty_anti_patterns': [('yaml: >=1 gotcha OR >=1 anti_pattern record (caution-surface OR-rule)',
                                      'fail',
                                      'no technique carries a gotchas list and no anti_patterns record '
                                      'exists',
                                      'caution-floor')],
             'no_technique_unit': []},
 'record_floor': {'no_block': None,
                  'empty_block': ('yaml: >=1 record across insights/conventions (union floor)',
                                  'fail',
                                  'claude_md block carries neither insights nor conventions; the block must '
                                  'be non-empty',
                                  'claude-md-record-floor'),
                  'insights_only': ('yaml: >=1 record across insights/conventions (union floor)',
                                    'pass',
                                    '2 insight(s), 0 convention(s)',
                                    'claude-md-record-floor'),
                  'conventions_only': ('yaml: >=1 record across insights/conventions (union floor)',
                                       'pass',
                                       '0 insight(s), 1 convention(s)',
                                       'claude-md-record-floor'),
                  'both': ('yaml: >=1 record across insights/conventions (union floor)',
                           'pass',
                           '1 insight(s), 1 convention(s)',
                           'claude-md-record-floor'),
                  'block_not_a_mapping': None,
                  'lists_not_lists': ('yaml: >=1 record across insights/conventions (union floor)',
                                      'fail',
                                      'claude_md block carries neither insights nor conventions; the block '
                                      'must be non-empty',
                                      'claude-md-record-floor')},
 'defaults': {'clean': [('relative markdown links resolve', 'pass', '', 'claude-md-links-resolve'),
                        ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                        ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                        ('body size signal', 'pass', 'lines=3, tokens~3', 'claude-md-size-signal')],
              'broken_link': [('relative markdown links resolve',
                               'fail',
                               'line 3: missing.md',
                               'claude-md-links-resolve'),
                              ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                              ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                              ('body size signal', 'pass', 'lines=3, tokens~6', 'claude-md-size-signal')],
              'unclosed_fence': [('relative markdown links resolve', 'pass', '', 'claude-md-links-resolve'),
                                 ('code fences closed',
                                  'fail',
                                  'a ``` or ~~~ fence is opened and never closed',
                                  'claude-md-fences-closed'),
                                 ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                                 ('body size signal', 'pass', 'lines=4, tokens~5', 'claude-md-size-signal')],
              'import_over_limit': [('relative markdown links resolve',
                                     'pass',
                                     '',
                                     'claude-md-links-resolve'),
                                    ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                                    ('@imports <= 50 lines',
                                     'fail',
                                     'line 3: @big.md has 80 lines',
                                     'claude-md-import-size'),
                                    ('body size signal',
                                     'pass',
                                     'lines=4, tokens~5',
                                     'claude-md-size-signal')],
              'import_over_tight_limit': [('relative markdown links resolve',
                                           'pass',
                                           '',
                                           'claude-md-links-resolve'),
                                          ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                                          ('@imports <= 3 lines',
                                           'fail',
                                           'line 3: @sub/small.md has 5 lines',
                                           'claude-md-import-size'),
                                          ('body size signal',
                                           'pass',
                                           'lines=3, tokens~3',
                                           'claude-md-size-signal')],
              'import_missing': [('relative markdown links resolve',
                                  'fail',
                                  'line 3: @gone.md (missing import)',
                                  'claude-md-links-resolve'),
                                 ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                                 ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                                 ('body size signal', 'pass', 'lines=3, tokens~3', 'claude-md-size-signal')],
              'fenced_import_ignored': [('relative markdown links resolve',
                                         'pass',
                                         '',
                                         'claude-md-links-resolve'),
                                        ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                                        ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                                        ('body size signal',
                                         'pass',
                                         'lines=6, tokens~7',
                                         'claude-md-size-signal')],
              'oversized_body': [('relative markdown links resolve', 'pass', '', 'claude-md-links-resolve'),
                                 ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                                 ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                                 ('body size signal',
                                  'judgment-required',
                                  'lines=3, tokens~5202; run the size-signal judgment (split only when '
                                  'sections serve different reading tasks)',
                                  'claude-md-size-signal')],
              'tight_body_limits': [('relative markdown links resolve',
                                     'pass',
                                     '',
                                     'claude-md-links-resolve'),
                                    ('code fences closed', 'pass', '', 'claude-md-fences-closed'),
                                    ('@imports <= 50 lines', 'pass', '', 'claude-md-import-size'),
                                    ('body size signal',
                                     'judgment-required',
                                     'lines=32, tokens~41; run the size-signal judgment (split only when '
                                     'sections serve different reading tasks)',
                                     'claude-md-size-signal')]}}
# END-EXPECTED


def _rows(rows):
    return [(r.row, r.verdict, r.note, r.rule) for r in rows]


def _body(keys):
    return parse_body("".join(SNIP[k] for k in keys))


def _skill_dir(tmp_path, bundle):
    d = tmp_path / ("with_scripts" if bundle else "plain")
    d.mkdir(exist_ok=True)
    if bundle:
        (d / "scripts").mkdir(exist_ok=True)
    return d


def _claude_md_dir(tmp_path):
    d = tmp_path / "cm"
    (d / "sub").mkdir(parents=True, exist_ok=True)
    (d / "ok.md").write_text("x\n", encoding="utf-8")
    (d / "big.md").write_text("line\n" * 80, encoding="utf-8")
    (d / "sub" / "small.md").write_text("line\n" * 5, encoding="utf-8")
    return d


def _assert_warns_naming(rec, old, replacement_hint):
    messages = [str(w.message) for w in rec if issubclass(w.category, DeprecationWarning)]
    assert len(messages) == 1, messages
    assert old in messages[0]
    assert "0.97.0" in messages[0] and "0.99.0" in messages[0]
    assert replacement_hint in messages[0]
    # stacklevel=2: the warning is attributed to the caller, this file.
    assert Path(rec[0].filename).resolve() == Path(__file__).resolve()


# ---- check_<type>_skill ----------------------------------------------------


@pytest.mark.parametrize(
    "skill_type,name", [(t, n) for t, fx in TYPE_FIXTURES.items() for n in fx]
)
def test_type_shim_returns_the_original_rows(tmp_path, skill_type, name):
    keys, bundle = TYPE_FIXTURES[skill_type][name]
    with pytest.warns(DeprecationWarning) as rec:
        rows = SHIMS[skill_type](_body(keys), _skill_dir(tmp_path, bundle))
    assert _rows(rows) == EXPECTED["type"][skill_type][name]
    _assert_warns_naming(rec, f"audit.{SHIMS[skill_type].__name__}", "audit()")


def test_check_technique_skill_still_accepts_and_ignores_fm(tmp_path):
    body = _body(("steps2",))
    with pytest.warns(DeprecationWarning):
        with_fm = check_technique_skill(body, tmp_path, Frontmatter("", {"disable-model-invocation": "true"}))
    assert _rows(with_fm) == EXPECTED["type"]["technique-skill"]["two_steps"]


# ---- facts / caution / record floor ----------------------------------------


@needs_yaml
@pytest.mark.parametrize("name", FACTS_FIXTURES)
def test_check_facts_cross_rules_returns_the_original_rows(name):
    text, declared, root = FACTS_FIXTURES[name]
    with pytest.warns(DeprecationWarning) as rec:
        rows = check_facts_cross_rules(text, declared, root)
    assert _rows(rows) == EXPECTED["facts"][name]
    _assert_warns_naming(rec, "audit.check_facts_cross_rules", "facts-floor")


@needs_yaml
def test_check_facts_cross_rules_keeps_its_two_argument_call():
    text, declared, _root = FACTS_FIXTURES["nested_full"]
    with pytest.warns(DeprecationWarning):
        rows = check_facts_cross_rules(text, declared)
    assert _rows(rows) == EXPECTED["facts"]["nested_full"]


@needs_yaml
@pytest.mark.parametrize("name", CAUTION_FIXTURES)
def test_check_technique_caution_cross_rule_returns_the_original_rows(name):
    with pytest.warns(DeprecationWarning) as rec:
        rows = check_technique_caution_cross_rule(CAUTION_FIXTURES[name])
    assert _rows(rows) == EXPECTED["caution"][name]
    _assert_warns_naming(rec, "audit.check_technique_caution_cross_rule", "caution-floor")


@pytest.mark.parametrize("name", RECORD_FLOOR_FIXTURES)
def test_check_claude_md_record_floor_returns_the_original_row(name):
    with pytest.warns(DeprecationWarning) as rec:
        row = check_claude_md_record_floor(RECORD_FLOOR_FIXTURES[name])
    assert (None if row is None else _rows([row])[0]) == EXPECTED["record_floor"][name]
    _assert_warns_naming(rec, "audit.check_claude_md_record_floor", "claude-md-record-floor")


# ---- check_claude_md_defaults ----------------------------------------------


@pytest.mark.parametrize("name", DEFAULTS_FIXTURES)
def test_check_claude_md_defaults_old_call_shape_returns_the_original_four_rows(tmp_path, name):
    text, overrides = DEFAULTS_FIXTURES[name]
    path = _claude_md_dir(tmp_path) / "CLAUDE.md"
    th = {**THRESHOLDS, "import_max_lines": 50, **overrides}
    with pytest.warns(DeprecationWarning) as rec:
        rows = check_claude_md_defaults(path, text, parse_body(text), th)
    assert _rows(rows) == EXPECTED["defaults"][name]
    _assert_warns_naming(rec, "audit.check_claude_md_defaults(body, th)", "audit_claude_md")


@pytest.mark.parametrize("name", DEFAULTS_FIXTURES)
def test_check_claude_md_defaults_current_shape_is_the_first_two_original_rows_and_silent(tmp_path, name):
    text, _overrides = DEFAULTS_FIXTURES[name]
    path = _claude_md_dir(tmp_path) / "CLAUDE.md"
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        rows = check_claude_md_defaults(path, text)
    assert _rows(rows) == EXPECTED["defaults"][name][:2]


def test_check_claude_md_defaults_old_shape_works_with_the_current_thresholds_mapping(tmp_path):
    """Callers that pass audit.THRESHOLDS (which no longer holds import_max_lines)
    got a KeyError from the reduced function; the old shape supplies the default."""
    text, _overrides = DEFAULTS_FIXTURES["import_over_limit"]
    path = _claude_md_dir(tmp_path) / "CLAUDE.md"
    with pytest.warns(DeprecationWarning):
        rows = check_claude_md_defaults(path, text, parse_body(text), THRESHOLDS)
    assert _rows(rows) == EXPECTED["defaults"]["import_over_limit"]


def test_check_claude_md_defaults_refuses_a_half_old_call_shape(tmp_path):
    with pytest.raises(TypeError, match="together"):
        check_claude_md_defaults(tmp_path / "CLAUDE.md", "x", parse_body("x"))
    with pytest.raises(TypeError, match="together"):
        check_claude_md_defaults(tmp_path / "CLAUDE.md", "x", th=dict(THRESHOLDS))


# ---- THRESHOLDS ------------------------------------------------------------


def test_thresholds_import_max_lines_lookup_warns_and_answers():
    with pytest.warns(DeprecationWarning) as rec:
        value = THRESHOLDS["import_max_lines"]
    assert value == 50
    _assert_warns_naming(rec, 'THRESHOLDS["import_max_lines"]', "claude-md-import-size")


def test_thresholds_import_max_lines_is_not_a_member_and_other_misses_still_raise():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        assert "import_max_lines" not in THRESHOLDS
        assert "import_max_lines" not in list(THRESHOLDS)
        assert THRESHOLDS.get("import_max_lines") is None
        assert {**THRESHOLDS}.keys() == THRESHOLDS.keys()
        with pytest.raises(KeyError):
            THRESHOLDS["no_such_threshold"]


@needs_yaml
def test_the_deprecated_import_max_lines_default_equals_the_shipped_criterion_default():
    from skills_kit_lib import standards_resolve

    shipped = standards_resolve.resolve_shipped().threshold_defaults["import_max_lines"]
    assert audit_mod._DEPRECATED_IMPORT_MAX_LINES == shipped


# ---- SkillCriterion, TYPE_RUNNERS, skill_md_criteria -----------------------


def test_skill_criterion_is_the_mechanical_criterion_and_warns_on_access():
    with pytest.warns(DeprecationWarning) as rec:
        cls = audit_mod.SkillCriterion
    assert cls is MechanicalCriterion
    assert cls("desc-160-char", "fail") == MechanicalCriterion("desc-160-char", "fail")
    _assert_warns_naming(rec, "audit.SkillCriterion", "MechanicalCriterion")


def test_skill_criterion_import_form_warns():
    with pytest.warns(DeprecationWarning, match="SkillCriterion"):
        from skills_kit_lib.audit import SkillCriterion  # noqa: F401


def test_type_runners_warns_on_access_and_holds_the_original_four_runners(tmp_path):
    with pytest.warns(DeprecationWarning) as rec:
        runners = audit_mod.TYPE_RUNNERS
    _assert_warns_naming(rec, "audit.TYPE_RUNNERS", "audit()")
    assert set(runners) == {"reference-skill", "pattern-skill", "discipline-skill", "domain-skill"}
    for skill_type, fn in runners.items():
        keys, bundle = TYPE_FIXTURES[skill_type]["empty"]
        with pytest.warns(DeprecationWarning):
            rows = fn(_body(keys), _skill_dir(tmp_path, bundle))
        assert _rows(rows) == EXPECTED["type"][skill_type]["empty"]


def test_an_unknown_module_attribute_still_raises_attribute_error():
    with pytest.raises(AttributeError, match="no_such_name"):
        audit_mod.no_such_name  # noqa: B018


def test_skill_md_criteria_delegates_to_mechanical_criteria():
    with pytest.warns(DeprecationWarning) as rec:
        old = skill_md_criteria(None)
    assert old == mechanical_criteria("skill_md", None)
    _assert_warns_naming(rec, "audit.skill_md_criteria", 'mechanical_criteria("skill_md", resolved)')


# ---- the shims serve what audit() reports ----------------------------------


@pytest.mark.parametrize("skill_type", ["reference-skill", "pattern-skill", "technique-skill",
                                         "discipline-skill", "domain-skill"])
def test_a_type_shim_reports_the_rows_audit_puts_in_type_specific(tmp_path, skill_type):
    """The shim and audit() read the same criteria: a legacy-fallback document
    (no contract block) gets exactly the shim's rows in its type_specific list."""
    keys, bundle = TYPE_FIXTURES[skill_type]["empty"]
    skill = tmp_path / "SKILL.md"
    skill.write_text(
        f"---\nname: demo\ndescription: Use when testing. Do NOT use for anything else.\nskill-type: {skill_type}\n---\n"
        "# Demo\n\nThis skill does a thing.\n",
        encoding="utf-8",
    )
    report = audit(skill)
    body = parse_body(skill.read_text(encoding="utf-8"))
    with pytest.warns(DeprecationWarning):
        shim_rows = SHIMS[skill_type](body, tmp_path)
    assert [(r["row"], r["verdict"], r["note"], r["rule"]) for r in report["type_specific"]] == _rows(shim_rows)


# ---- the contract listing --------------------------------------------------


def _base_name(entry_name: str) -> str:
    return re.sub(r"(\(.*\)|\[.*\])$", "", entry_name)


def _resolve(dotted: str):
    module_name, _, attr = dotted.rpartition(".")
    import importlib

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        return getattr(importlib.import_module(module_name), attr)


def test_contract_entries_are_well_formed():
    names = [e["name"] for e in ENTRIES]
    assert len(names) == len(set(names)), "duplicate contract entries"
    for e in ENTRIES:
        assert e["status"] in {"public", "deprecated", "removed"}, e
        assert e["name"].startswith(CONTRACT["package"] + "."), e
        if e["status"] == "deprecated":
            assert e.get("since") and e.get("remove_after") and e.get("replacement"), e
        if e["status"] == "removed":
            assert e.get("since") and e.get("replacement"), e


@pytest.mark.parametrize("entry", [e for e in ENTRIES if e["status"] in {"public", "deprecated"}],
                         ids=lambda e: e["name"])
def test_listed_name_still_resolves(entry):
    assert _resolve(_base_name(entry["name"])) is not None, (
        f"{entry['name']} is listed {entry['status']} but no longer resolves. Removing a public name needs a "
        "DeprecationWarning shim for the policy window first (docs/reference/deprecation-policy.md)."
    )


def test_removed_names_stay_removed():
    for entry in (e for e in ENTRIES if e["status"] == "removed"):
        with pytest.raises(AttributeError):
            _resolve(_base_name(entry["name"]))


def test_deprecated_entries_are_inside_their_window():
    for e in ENTRIES:
        if e["status"] != "deprecated":
            continue
        since = tuple(int(p) for p in e["since"].split("."))
        remove_after = tuple(int(p) for p in e["remove_after"].split("."))
        assert since <= VERSION, f"{e['name']} is deprecated since {e['since']}, which is ahead of the plugin version"
        assert VERSION < remove_after, (
            f"{e['name']} outlived remove_after {e['remove_after']}: delete the shim and record the name as removed."
        )
        # Two minor versions after the deprecating release (deprecation-policy.md, rule 2).
        assert (remove_after[0], remove_after[1]) == (since[0], since[1] + 2), e


def test_every_name_audit_deprecates_is_listed():
    """Derived from the code: a shim added to audit.py without a deprecated
    entry fails, because every shim builds its warning with _deprecation_message
    naming the old name as the first argument."""
    tree = ast.parse(Path(audit_mod.__file__).read_text(encoding="utf-8"))
    named = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_deprecation_message"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
            named.add(node.args[0].value)
    assert len(named) >= 13, named
    listed = {e["name"].removeprefix("skills_kit_lib.audit.") for e in ENTRIES
              if e["status"] == "deprecated" and e["name"].startswith("skills_kit_lib.audit.")}
    assert named == listed, (named - listed, listed - named)


def test_every_name_lane_models_deprecates_is_listed():
    """Derived from the code: each lane_models shim names itself in _deprecated()."""
    from skills_kit_lib import lane_models

    tree = ast.parse(Path(lane_models.__file__).read_text(encoding="utf-8"))
    named = {node.args[0].value for node in ast.walk(tree)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_deprecated"
             and node.args and isinstance(node.args[0], ast.Constant)}
    listed = {e["name"].removeprefix("skills_kit_lib.lane_models.") for e in ENTRIES
              if e["status"] == "deprecated" and e["name"].startswith("skills_kit_lib.lane_models.")}
    assert named == listed == {"agent_route", "agent_routes"}
