from agent_glue_lib.core import pascal_to_snake, snake_to_pascal


def test_pascal_to_snake_basic():
    assert pascal_to_snake("Topology") == "topology"


def test_pascal_to_snake_multi_word():
    assert pascal_to_snake("StateDecl") == "state_decl"
    assert pascal_to_snake("FixtureId") == "fixture_id"
    assert pascal_to_snake("TerminalNode") == "terminal_node"


def test_snake_to_pascal_roundtrip():
    for pascal in ["Name", "Topology", "StateDecl", "FixtureId", "TerminalNode", "SourceRunId"]:
        assert snake_to_pascal(pascal_to_snake(pascal)) == pascal
