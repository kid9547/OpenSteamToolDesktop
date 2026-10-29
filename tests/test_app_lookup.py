"""AppID 查询与本地更新检测的单元测试。"""
from __future__ import annotations

import struct
from pathlib import Path

from core.app_lookup import (
    build_app_index,
    read_appinfo,
    scan_installed_apps,
    search_apps,
    update_summary,
)


_KEYS = [
    "appinfo",
    "appid",
    "common",
    "name",
    "type",
    "config",
    "installdir",
    "depots",
    "branches",
    "public",
    "buildid",
    "timeupdated",
    "manifests",
    "gid",
    "size",
    "101",
    "102",
    "201",
    "301",
    "302",
]


def _key_index(name: str) -> int:
    return _KEYS.index(name)


def _key(name: str) -> bytes:
    return struct.pack("<i", _key_index(name))


def _string(name: str, value: str) -> bytes:
    return b"\x01" + _key(name) + value.encode("utf-8") + b"\x00"


def _int32(name: str, value: int) -> bytes:
    return b"\x02" + _key(name) + struct.pack("<i", value)


def _object(name: str, body: bytes) -> bytes:
    return b"\x00" + _key(name) + body + b"\x08"


def _appinfo_data(app_id: int, name: str, install_dir: str, build_id: int, depots: dict[str, str]) -> bytes:
    common = _object("common", _string("name", name) + _string("type", "Game"))
    config = _object("config", _string("installdir", install_dir))
    depot_body = b""
    for depot_id, gid in depots.items():
        public = _object("public", _string("gid", gid) + _string("size", "1000"))
        manifests = _object("manifests", public)
        depot_body += _object(depot_id, manifests)
    public_branch = _object(
        "public", _int32("buildid", build_id) + _int32("timeupdated", 1700000000)
    )
    branches = _object("branches", public_branch)
    depot_section = _object("depots", depot_body + branches)
    body = _int32("appid", app_id) + common + config + depot_section
    return b"\x00" + _key("appinfo") + body + b"\x08" + b"\x08"


def _write_appinfo(path: Path, apps: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = b""
    for app in apps:
        data = _appinfo_data(
            app["appid"], app["name"], app["install_dir"], app["build_id"], app["depots"]
        )
        header = (
            struct.pack("<IIQ", 1, 1700000000, 0)
            + b"\x00" * 20
            + struct.pack("<I", 1)
            + b"\x00" * 20
        )
        payload = header + data
        entries += struct.pack("<II", app["appid"], len(payload)) + payload

    with path.open("wb") as stream:
        stream.write(b")DV\x07")
        stream.write(struct.pack("<I", 1))
        stream.write(struct.pack("<q", 0))  # placeholder
        stream.write(entries)
        stream.write(struct.pack("<I", 0))
        table_offset = stream.tell()
        stream.write(struct.pack("<I", len(_KEYS)))
        for key in _KEYS:
            stream.write(key.encode("utf-8") + b"\x00")
        stream.seek(8)
        stream.write(struct.pack("<q", table_offset))


def _write_acf(path: Path, app_id: str, name: str, build_id: str, depots: dict[str, str], flags: int = 4) -> None:
    depot_lines = "\n".join(
        f'\t\t"{depot}"\n\t\t{{\n\t\t\t"manifest"\t\t"{gid}"\n\t\t}}'
        for depot, gid in depots.items()
    )
    path.write_text(
        f'''"AppState"
{{
\t"appid"\t\t"{app_id}"
\t"name"\t\t"{name}"
\t"StateFlags"\t\t"{flags}"
\t"installdir"\t\t"{name.replace(" ", "")}"
\t"LastUpdated"\t\t"1700000000"
\t"SizeOnDisk"\t\t"123456"
\t"buildid"\t\t"{build_id}"
\t"TargetBuildID"\t\t"0"
\t"InstalledDepots"
\t{{
{depot_lines}
\t}}
}}
''',
        encoding="utf-8",
    )


def _make_steam_root(tmp_path: Path) -> Path:
    steam = tmp_path / "Steam"
    steamapps = steam / "steamapps"
    steamapps.mkdir(parents=True)
    (steamapps / "libraryfolders.vdf").write_text(
        f'''"libraryfolders"
{{
\t"0"
\t{{
\t\t"path"\t\t"{steam.as_posix()}"
\t}}
}}
''',
        encoding="utf-8",
    )
    (steamapps / "depotcache").mkdir()
    (steamapps / "depotcache" / "101_111.manifest").write_bytes(b"manifest")
    _write_acf(steamapps / "appmanifest_100.acf", "100", "Test Game", "1000", {"101": "111", "102": "222"})
    _write_acf(steamapps / "appmanifest_300.acf", "300", "Pinned Game", "3000", {"301": "333"})
    _write_appinfo(
        steam / "appcache" / "appinfo.vdf",
        [
            {
                "appid": 100,
                "name": "Test Game",
                "install_dir": "TestGame",
                "build_id": 2000,
                "depots": {"101": "111", "102": "999"},
            },
            {
                "appid": 200,
                "name": "Uninstalled Game",
                "install_dir": "UninstalledGame",
                "build_id": 500,
                "depots": {"201": "777"},
            },
            {
                "appid": 300,
                "name": "Pinned Game",
                "install_dir": "PinnedGame",
                "build_id": 3000,
                "depots": {"301": "444"},
            },
        ],
    )
    lua_dir = steam / "config" / "lua"
    lua_dir.mkdir(parents=True)
    (lua_dir / "100.lua").write_text(
        'addappid(100)\naddappid(101, 0, "x")\nsetManifestid(101, "111")\n',
        encoding="utf-8",
    )
    return steam


def test_read_appinfo_v41(tmp_path):
    steam = _make_steam_root(tmp_path)
    info = read_appinfo(str(steam))
    assert set(info) == {"100", "200", "300"}
    assert info["100"].name == "Test Game"
    assert info["100"].latest_build_id == "2000"
    assert info["100"].depot_gids == {"101": "111", "102": "999"}
    assert info["200"].install_dir == "UninstalledGame"


def test_scan_installed_apps_reads_manifest_fields(tmp_path):
    steam = _make_steam_root(tmp_path)
    apps = scan_installed_apps(str(steam))
    by_id = {app.app_id: app for app in apps}
    assert set(by_id) == {"100", "300"}
    assert by_id["100"].name == "Test Game"
    assert by_id["100"].build_id == "1000"
    assert by_id["100"].state_flags == 4
    assert by_id["100"].installed_depots == {"101": "111", "102": "222"}


def test_build_index_detects_update_missing_manifest_and_lua(tmp_path, monkeypatch):
    steam = _make_steam_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    apps = build_app_index(str(steam))
    by_id = {app.app_id: app for app in apps}

    app = by_id["100"]
    assert app.installed is True
    assert app.lua_status == "已启用"
    assert app.missing_manifests == ["102_222"]
    assert any("新 Build 2000" in reason for reason in app.update_reasons)
    assert any("清单落后" in reason for reason in app.update_reasons)

    uninstalled = by_id["200"]
    assert uninstalled.installed is False
    assert uninstalled.update_status == "未安装"


def test_pinned_depot_is_reported_as_note_not_update(tmp_path, monkeypatch):
    steam = _make_steam_root(tmp_path)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    from core.manifest_pins import pin

    assert pin("300", "301", "333")
    apps = build_app_index(str(steam))
    app = {item.app_id: item for item in apps}["300"]
    assert app.pinned_depots == {"301": "333"}
    assert any("已固定清单" in note for note in app.notes)
    assert not any("清单落后" in reason for reason in app.update_reasons)


def test_search_apps_by_name_and_appid(tmp_path):
    steam = _make_steam_root(tmp_path)
    apps = build_app_index(str(steam))
    assert [app.app_id for app in search_apps(apps, "Pal")] == []
    assert {app.app_id for app in search_apps(apps, "Test Game")} == {"100"}
    assert {app.app_id for app in search_apps(apps, "Uninstalled")} == {"200"}
    assert [app.app_id for app in search_apps(apps, "100")] == ["100"]
    # 空查询只返回已安装游戏
    assert {app.app_id for app in search_apps(apps, "")} == {"100", "300"}
    assert "已安装 2 个游戏" in update_summary(apps)
