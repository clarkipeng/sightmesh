from pathlib import Path

from sightmesh.relationships import RelationshipStore


def test_records_parent_and_lists_direct_children(tmp_path: Path) -> None:
    store = RelationshipStore(tmp_path / "relationships.sqlite3")

    first = store.record(
        child_session_id="child-a",
        child_workspace_id="workspace-a",
        parent_session_id="parent",
        parent_workspace_id="workspace-parent",
    )
    store.record(
        child_session_id="child-b",
        child_workspace_id="workspace-b",
        parent_session_id="parent",
        parent_workspace_id="workspace-parent",
    )

    assert store.parent("child-a") == first
    assert [edge.child_session_id for edge in store.children("parent")] == [
        "child-a",
        "child-b",
    ]


def test_reparenting_replaces_the_child_edge(tmp_path: Path) -> None:
    store = RelationshipStore(tmp_path / "relationships.sqlite3")
    store.record(
        child_session_id="child",
        child_workspace_id="workspace-child",
        parent_session_id="parent-a",
        parent_workspace_id="workspace-a",
    )

    store.record(
        child_session_id="child",
        child_workspace_id="workspace-child",
        parent_session_id="parent-b",
        parent_workspace_id="workspace-b",
    )

    assert store.children("parent-a") == []
    assert store.parent("child").parent_session_id == "parent-b"
