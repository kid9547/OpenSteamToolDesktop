"""Lua 构建/解析中 D 加密票据与清单绑定的往返一致性测试。

对应上游 OpenSteamTool 的 Lua 契约：
``addappid`` / ``setManifestid`` / ``setAppTicket`` / ``setETicket``。
"""
from __future__ import annotations

from core.game_manager import DepotInfo, GameMetadata, LuaGameManager
from core.game_manager import _build_ticket_lines


def _manager(tmp_path) -> LuaGameManager:
    lua_dir = tmp_path / "config" / "lua"
    lua_dir.mkdir(parents=True, exist_ok=True)
    return LuaGameManager(lua_dir=str(lua_dir))


# ── 票据行构建 ────────────────────────────────────────────────
def test_build_ticket_lines_empty_when_no_tickets():
    assert _build_ticket_lines(GameMetadata(app_id="1")) == []


def test_build_ticket_lines_emits_both_directives():
    metadata = GameMetadata(app_id="1361510", app_ticket="AABB", eticket="CCDD")
    lines = _build_ticket_lines(metadata)
    assert 'setAppTicket(1361510, "aabb")' in lines
    assert 'setETicket(1361510, "ccdd")' in lines


def test_build_ticket_lines_strips_separators():
    metadata = GameMetadata(app_id="7", app_ticket="01 02:0a-0b")
    lines = _build_ticket_lines(metadata)
    assert 'setAppTicket(7, "01020a0b")' in lines


# ── 完整 Lua 内容 ─────────────────────────────────────────────
def test_lua_content_orders_appid_before_ticket(tmp_path):
    manager = _manager(tmp_path)
    metadata = GameMetadata(
        app_id="1361510",
        name="Denuvo Game",
        depots=[DepotInfo(depot_id="1361511", depot_key="ab" * 32)],
        app_ticket="aa" * 52,
        eticket="bb" * 20,
    )
    content = LuaGameManager._build_lua_content(metadata)
    lines = content.splitlines()

    addappid_idx = next(i for i, line in enumerate(lines) if line.startswith("addappid(1361510)"))
    app_ticket_idx = next(i for i, line in enumerate(lines) if line.startswith("setAppTicket("))
    eticket_idx = next(i for i, line in enumerate(lines) if line.startswith("setETicket("))

    # 票据必须在 addappid 之后，否则 OpenSteamTool 会忽略（LuaConfig::HasDepot）
    assert addappid_idx < app_ticket_idx < eticket_idx


def test_lua_content_includes_both_tickets_verbatim(tmp_path):
    metadata = GameMetadata(app_id="42", app_ticket="deadbeef", eticket="cafebabe")
    content = LuaGameManager._build_lua_content(metadata)
    assert 'setAppTicket(42, "deadbeef")' in content
    assert 'setETicket(42, "cafebabe")' in content


# ── 解析往返 ──────────────────────────────────────────────────
def test_parse_roundtrip_preserves_manifest_gid_and_size(tmp_path):
    """回归：构建器输出带 size 的三参 setManifestid，解析器必须能读回。"""
    manager = _manager(tmp_path)
    metadata = GameMetadata(
        app_id="1623730",
        name="Palworld",
        depots=[
            DepotInfo(depot_id="1623731", manifest_gid="868868087024202254", size=35943136912),
            DepotInfo(depot_id="1623732", depot_key="cd" * 32),
        ],
    )
    content = LuaGameManager._build_lua_content(metadata)
    lua_path = tmp_path / "config" / "lua" / "1623730.lua"
    lua_path.write_text(content, encoding="utf-8")

    parsed = manager.parse_lua_to_metadata("1623730")

    assert parsed is not None
    by_id = {depot.depot_id: depot for depot in parsed.depots}
    assert by_id["1623731"].manifest_gid == "868868087024202254"
    assert by_id["1623731"].size == 35943136912
    assert by_id["1623732"].depot_key == "cd" * 32


def test_parse_reads_tickets_from_lua(tmp_path):
    manager = _manager(tmp_path)
    lua_path = tmp_path / "config" / "lua" / "1361510.lua"
    lua_path.write_text(
        "addappid(1361510)\n"
        'setAppTicket(1361510, "1400AABB")\n'
        'setETicket(1361510, "1500CCDD")\n',
        encoding="utf-8",
    )

    parsed = manager.parse_lua_to_metadata("1361510")

    assert parsed is not None
    assert parsed.app_ticket == "1400AABB"
    assert parsed.eticket == "1500CCDD"


def test_parse_ignores_tickets_of_other_appids(tmp_path):
    manager = _manager(tmp_path)
    lua_path = tmp_path / "config" / "lua" / "111.lua"
    lua_path.write_text(
        "addappid(111)\n"
        'setAppTicket(111, "aaaa")\n'
        'setAppTicket(222, "bbbb")\n',
        encoding="utf-8",
    )

    parsed = manager.parse_lua_to_metadata("111")

    assert parsed is not None
    assert parsed.app_ticket == "aaaa"


def test_parse_accepts_legacy_two_arg_manifest_form(tmp_path):
    """兼容旧版 Lua（无 size 参数、GID 带引号）。"""
    manager = _manager(tmp_path)
    lua_path = tmp_path / "config" / "lua" / "730.lua"
    lua_path.write_text(
        'addappid(730)\nsetManifestid(731, "7127896784363312296")\n',
        encoding="utf-8",
    )

    parsed = manager.parse_lua_to_metadata("730")

    assert parsed is not None
    by_id = {depot.depot_id: depot for depot in parsed.depots}
    assert by_id["731"].manifest_gid == "7127896784363312296"
    assert by_id["731"].size == 0


# ── 票据写入现有 Lua（幂等更新）──────────────────────────────
def test_apply_ticket_bundle_inserts_and_keeps_existing_lines(tmp_path):
    manager = _manager(tmp_path)
    lua_path = tmp_path / "config" / "lua" / "1361510.lua"
    lua_path.write_text('addappid(1361510)\naddtoken(1361510, "tok")\n', encoding="utf-8")

    metadata = GameMetadata(app_id="1361510", app_ticket="aabb", eticket="ccdd")
    manager.apply_ticket_bundle("1361510", metadata)

    content = lua_path.read_text(encoding="utf-8")
    lines = content.splitlines()
    assert lines[0] == "addappid(1361510)"
    assert 'setAppTicket(1361510, "aabb")' in lines
    assert 'setETicket(1361510, "ccdd")' in lines
    # 原有内容不得丢失，且 addappid 必须仍排在票据之前
    assert 'addtoken(1361510, "tok")' in lines
    assert lines.index("addappid(1361510)") < lines.index('setAppTicket(1361510, "aabb")')


def test_apply_ticket_bundle_replaces_old_tickets_without_duplicating(tmp_path):
    manager = _manager(tmp_path)
    lua_path = tmp_path / "config" / "lua" / "77.lua"
    lua_path.write_text(
        'addappid(77)\nsetAppTicket(77, "oldold")\nsetETicket(77, "oldet")\n',
        encoding="utf-8",
    )

    manager.apply_ticket_bundle("77", GameMetadata(app_id="77", app_ticket="newnew"))

    content = lua_path.read_text(encoding="utf-8")
    assert content.count("setAppTicket(") == 1
    assert content.count("setETicket(") == 0
    assert 'setAppTicket(77, "newnew")' in content


def test_apply_ticket_bundle_rejects_empty_ticket(tmp_path):
    """空票据视为清除模式：删掉票据行，但保留 addappid。"""
    manager = _manager(tmp_path)
    lua_path = tmp_path / "config" / "lua" / "5.lua"
    lua_path.write_text(
        'addappid(5)\nsetAppTicket(5, "aa")\nsetETicket(5, "bb")\n', encoding="utf-8"
    )

    manager.apply_ticket_bundle("5", GameMetadata(app_id="5"))

    content = lua_path.read_text(encoding="utf-8")
    assert "setAppTicket" not in content
    assert "setETicket" not in content
    assert "addappid(5)" in content
    assert manager.ticket_status_from_lua("5") == {
        "has_app_ticket": False,
        "has_eticket": False,
    }


def test_apply_ticket_bundle_requires_existing_lua(tmp_path):
    manager = _manager(tmp_path)
    try:
        manager.apply_ticket_bundle("9", GameMetadata(app_id="9", app_ticket="aabb"))
    except ValueError as exc:
        assert "Lua 文件不存在" in str(exc)
    else:
        raise AssertionError("missing lua file was accepted")


def test_ticket_status_from_lua_reports_both_flags(tmp_path):
    manager = _manager(tmp_path)
    (tmp_path / "config" / "lua" / "12.lua").write_text(
        'addappid(12)\nsetAppTicket(12, "aa")\n', encoding="utf-8"
    )
    (tmp_path / "config" / "lua" / "13.lua").write_text(
        'addappid(13)\nsetAppTicket(13, "aa")\nsetETicket(13, "bb")\n', encoding="utf-8"
    )

    assert manager.ticket_status_from_lua("12") == {
        "has_app_ticket": True,
        "has_eticket": False,
    }
    assert manager.ticket_status_from_lua("13") == {
        "has_app_ticket": True,
        "has_eticket": True,
    }
    assert manager.ticket_status_from_lua("404") == {
        "has_app_ticket": False,
        "has_eticket": False,
    }


def test_ticket_status_from_lua_ignores_other_appids(tmp_path):
    manager = _manager(tmp_path)
    (tmp_path / "config" / "lua" / "21.lua").write_text(
        'addappid(21)\nsetAppTicket(22, "aa")\n', encoding="utf-8"
    )
    assert manager.ticket_status_from_lua("21")["has_app_ticket"] is False
