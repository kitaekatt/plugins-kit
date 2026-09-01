"""The web editor surface -- owned by Databench, not by this kit.

Databench is a separate local web workbench for browsing and editing a
project's data files. It owns the browser surface outright: rendering,
navigation, viewers, and the safe-write path that puts bytes on disk.

This kit deliberately ships no editor of its own. The two meet at the file
seam described in this plugin's CLAUDE.md: this package's siblings decide
what a valid record IS (`schema/`) and carry anchored intent about it
(`comments/`), and Databench is what a person actually reads and writes it
through. Nothing here imports Databench and nothing in Databench imports
this module.

This package remains as the name of that boundary. Do not grow an editor
here; add to `schema/` or `comments/` and let Databench consume it.
"""
