"""Packaged data files read through importlib.resources.

A package rather than a bare directory so `[tool.setuptools.packages.find]`
discovers it and the declared package data ships in a built wheel. Read the
files through `skills_kit_lib.human_html.asset_css()` and its siblings, never
by filesystem path -- an installed consumer has no source tree.
"""
