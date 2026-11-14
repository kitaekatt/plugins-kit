"""Shader-language extensions are source the lanes do not read: listed as source, unmeasured."""

from __future__ import annotations

from clean_code_kit.languages import KIND_SOURCE_UNMEASURED, SOURCE_EXTENSIONS, kind_of

SHADER_EXTENSIONS = (".gdshader", ".glsl", ".hlsl", ".shader", ".vert", ".frag", ".comp", ".wgsl", ".metal")


def test_shader_extensions_are_source_unmeasured():
    assert all(ext in SOURCE_EXTENSIONS for ext in SHADER_EXTENSIONS)
    assert {ext: kind_of(ext) for ext in SHADER_EXTENSIONS} == {
        ext: KIND_SOURCE_UNMEASURED for ext in SHADER_EXTENSIONS}
