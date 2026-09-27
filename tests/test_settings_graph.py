"""Native settings graph actions against a disposable canonical LORE store."""
import io
import json
from types import SimpleNamespace

from doxa import lore_bridge


def test_native_graph_wire_uses_canonical_db_for_ascii_and_browser(monkeypatch, tmp_path):
    from lore_core import store
    from lore_core.config import project_slug

    # Schema, asserted edges and renderers are LORE's actual implementations.
    # Only the disposable store root changes; no live memory or browser opens.
    root = tmp_path / "lore"
    monkeypatch.setattr(store, "ROOT", root)
    cwd = str(tmp_path / "project")
    slug = project_slug(cwd)
    with store.db_connect() as conn:
        conn.executemany(
            "INSERT INTO beliefs(id,subject,claim,confidence,status) VALUES(?,?,?,?,?)",
            [(1, "user", "user preference", 0.9, "active"),
             (2, f"project:{slug}", "visible project fact", 0.8, "active"),
             (3, "project:unrelated", "PRIVATE OTHER PROJECT", 0.9, "active")],
        )
        conn.executemany(
            "INSERT INTO belief_edges(src,dst,rel,source) VALUES(?,?,?,?)",
            [(1, 2, "depends_on", "derived"), (1, 3, "depends_on", "derived")],
        )
    conn.close()
    requests = [
        {"id": 1, "op": "belief_graph_v1", "cwd": cwd, "belief_id": 1, "browser": False},
        {"id": 2, "op": "belief_graph_v1", "cwd": cwd, "belief_id": 1, "browser": True},
        {"id": 3, "op": "belief_graph_v1", "cwd": cwd, "belief_id": 3, "browser": True},
    ]
    output = io.BytesIO()
    monkeypatch.setattr(lore_bridge.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b"".join(map(lore_bridge._frame, requests)))))
    monkeypatch.setattr(lore_bridge.sys, "stdout", SimpleNamespace(buffer=output))
    lore_bridge.serve()
    frames = [json.loads(line) for line in output.getvalue().splitlines()]
    assert "belief_graph_v1" in frames[0]["capabilities"]
    ascii_reply, browser_reply, refused = frames[1:]
    assert ascii_reply["ok"] and ascii_reply["value"]["html"] is None
    assert "visible project fact" in "\n".join(ascii_reply["value"]["lines"])
    assert browser_reply["ok"]
    assert "visible project fact" in browser_reply["value"]["html"]
    assert "mermaid" in browser_reply["value"]["html"]
    assert "PRIVATE OTHER PROJECT" not in output.getvalue().decode()
    assert not refused["ok"] and refused["error"] == "belief_unavailable"
    with store.db_connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM belief_edges").fetchone()[0] == 2
    conn.close()
