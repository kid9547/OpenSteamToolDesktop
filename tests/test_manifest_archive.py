"""测试 core.manifest_archive — 社区 AppID 分支归档客户端（全部离线）。"""
from __future__ import annotations

import io
import zlib
import zipfile

from core.manifest_archive import (
    ArchiveManifest,
    ManifestArchive,
    ManifestArchiveClient,
    build_archive_urls,
    build_branch_raw_urls,
)
from core.manifest_downloader import STEAM_MANIFEST_MAGIC

BRANCH = "730"


def make_archive_bytes(files: dict[str, bytes], prefix: str = "ManifestAutoUpdate-730") -> bytes:
    """构造一个 ManifestAutoUpdate 风格的 AppID 分支归档 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{prefix}/", b"")
        for rel, data in files.items():
            zf.writestr(f"{prefix}/{rel}", data)
    return buf.getvalue()


SAMPLE_CONFIG = b'{"appId": 730, "depots": [731, 732], "dlcs": [1001], "packagedlcs": []}'
SAMPLE_KEYS = (
    b'"depots"\n{\n\t"731"\n\t{\n\t\t"DecryptionKey" '
    b'"bca9a9cde94bb4dff61849c6a87230ee45867a590fdd28826366e35e7d62c08e"\n\t}\n'
    b'\t"732"\n\t{\n\t\t"DecryptionKey" '
    b'"da1f76913633e9ce1b2bda5ec464dc507205388fac5c4c614b6a2706cdbd0912"\n\t}\n}\n'
)


class FakeResponse:
    def __init__(self, status_code: int = 404, content: bytes = b""):
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self):
        import json

        return json.loads(self.text)


class FakeHttpClient:
    """按 URL 匹配返回预设响应的假 httpx 客户端，并记录请求顺序。

    以 ``http`` 开头的 needle 使用前缀匹配（便于区分 ``https://github.com/...``
    与 ``https://ghfast.top/https://github.com/...``），否则使用子串匹配。
    """

    def __init__(self, routes: list[tuple[str, int, bytes]] | None = None, default: int = 404):
        self.routes = list(routes or [])
        self.default = default
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None):  # noqa: ARG002 - 签名对齐 httpx
        self.calls.append(url)
        for needle, status, body in self.routes:
            matched = url.startswith(needle) if needle.startswith("http") else needle in url
            if matched:
                return FakeResponse(status, body)
        return FakeResponse(self.default, b"Not Found")

    def close(self) -> None:
        pass


# ── URL 构造与镜像回退顺序 ────────────────────────────────


class TestUrlBuilders:
    def test_branch_raw_urls_mirror_order(self):
        urls = build_branch_raw_urls("Auiowu/ManifestAutoUpdate", BRANCH, "731_123.manifest")
        assert urls[0][1] == (
            "https://raw.githubusercontent.com/Auiowu/ManifestAutoUpdate/730/731_123.manifest"
        )
        joined = [u for _label, u in urls]
        assert joined[1].startswith("https://ghfast.top/")
        assert joined[2].startswith("https://ghproxy.net/")
        # jsDelivr 使用 <repo>@<branch> 形态
        assert "cdn.jsdelivr.net/gh/Auiowu/ManifestAutoUpdate@730/731_123.manifest" in joined[3]

    def test_archive_urls_mirror_order(self):
        urls = build_archive_urls("Auiowu/ManifestAutoUpdate", BRANCH)
        assert urls[0][1] == (
            "https://github.com/Auiowu/ManifestAutoUpdate/archive/refs/heads/730.zip"
        )
        assert urls[1][1].startswith("https://ghfast.top/https://github.com/")
        assert urls[2][1].startswith("https://ghproxy.net/https://github.com/")


# ── 归档解析（纯函数）────────────────────────────────────


class TestArchiveParsing:
    def test_parse_archive_extracts_manifests_keys_and_config(self):
        payload = make_archive_bytes(
            {
                "731_7127896784363312296.manifest": STEAM_MANIFEST_MAGIC + b"\x01" * 32,
                "732_522925226152885710.manifest": STEAM_MANIFEST_MAGIC + b"\x02" * 48,
                "Key.vdf": SAMPLE_KEYS,
                "config.json": SAMPLE_CONFIG,
                "appinfo.vdf": b'"appid" "730"',
                "README.md": b"# ManifestAutoUpdate",
            }
        )

        archive = ManifestArchiveClient.parse_archive(BRANCH, payload, repo="Auiowu/ManifestAutoUpdate")

        assert archive.ok is True
        assert {m.depot_id for m in archive.manifests} == {"731", "732"}
        assert archive.gid_map == {
            "731": "7127896784363312296",
            "732": "522925226152885710",
        }
        assert archive.depot_keys["731"].startswith("bca9a9cd")
        assert archive.config["appId"] == 730
        assert archive.dlc_ids == ["1001"]
        assert archive.depot_ids[:2] == ["731", "732"]
        assert archive.get("732").filename == "732_522925226152885710.manifest"
        assert all(m.has_valid_magic for m in archive.manifests)

    def test_parse_archive_rejects_entries_with_bad_magic(self):
        payload = make_archive_bytes(
            {
                "731_1111111111111111111.manifest": b"garbage-not-a-manifest",
                "732_2222222222222222222.manifest": STEAM_MANIFEST_MAGIC + b"\x00" * 24,
            }
        )
        archive = ManifestArchiveClient.parse_archive(BRANCH, payload)
        assert archive.ok is True
        assert [m.depot_id for m in archive.manifests] == ["732"]

    def test_parse_archive_without_valid_manifest_is_not_ok(self):
        payload = make_archive_bytes({"README.md": b"# empty"})
        archive = ManifestArchiveClient.parse_archive(BRANCH, payload)
        assert archive.ok is False
        assert "未找到" in archive.message

    def test_parse_archive_rejects_non_zip_payload(self):
        archive = ManifestArchiveClient.parse_archive(BRANCH, b"not a zip at all")
        assert archive.ok is False
        assert "ZIP" in archive.message

    def test_parse_archive_accepts_compressed_manifest_entries(self):
        raw = STEAM_MANIFEST_MAGIC + b"compressed-payload" * 4
        payload = make_archive_bytes(
            {"731_7777777777777777777.manifest": zlib.compress(raw)}
        )
        archive = ManifestArchiveClient.parse_archive(BRANCH, payload)
        assert archive.ok is True
        assert archive.manifests[0].data == raw

    def test_parse_key_vdf_variants(self):
        keys = ManifestArchiveClient.parse_key_vdf(SAMPLE_KEYS.decode())
        assert set(keys) == {"731", "732"}
        assert len(keys["731"]) == 64
        assert ManifestArchiveClient.parse_key_vdf("") == {}

    def test_parse_branch_tree_index(self):
        tree = {
            "truncated": False,
            "tree": [
                {"path": "731_7127896784363312296.manifest", "type": "blob", "size": 188},
                {"path": "config.json", "type": "blob", "size": 183},
                {"path": "nested/732_522925226152885710.manifest", "type": "blob", "size": 204},
            ],
        }
        archive = ManifestArchiveClient.parse_branch_tree(BRANCH, tree)
        assert archive.ok is True
        assert archive.gid_map == {
            "731": "7127896784363312296",
            "732": "522925226152885710",
        }
        assert archive.get("731").size == 188


# ── 网络层（注入假客户端）────────────────────────────────


class TestArchiveNetwork:
    def test_fetch_appid_archive_falls_back_to_mirrors(self):
        archive_bytes = make_archive_bytes(
            {"731_7127896784363312296.manifest": STEAM_MANIFEST_MAGIC + b"\x05" * 20}
        )
        http = FakeHttpClient(
            routes=[
                ("/730/config.json", 200, SAMPLE_CONFIG),
                ("https://github.com/Auiowu", 500, b"server error"),
                ("https://ghfast.top/", 200, archive_bytes),
            ]
        )
        client = ManifestArchiveClient(http=http, direct_http=http, retries=0)
        archive = client.fetch_appid_archive(BRANCH)

        assert archive.ok is True
        assert "ghfast.top" in archive.source_url
        assert archive.repo == "Auiowu/ManifestAutoUpdate"
        assert archive.gid_map == {"731": "7127896784363312296"}
        # 顺序：先探测 config.json → 直链失败 → 镜像成功
        assert any("/730/config.json" in url for url in http.calls)
        assert any("ghfast.top" in url for url in http.calls)

    def test_fetch_appid_archive_reports_missing_branch(self):
        http = FakeHttpClient(default=404)
        client = ManifestArchiveClient(http=http, direct_http=http, retries=0)
        archive = client.fetch_appid_archive("2361770")
        assert archive.ok is False
        assert "均无此 AppID 分支" in archive.message

    def test_fetch_single_manifest_mirror_fallback_order(self):
        manifest = STEAM_MANIFEST_MAGIC + b"\x09" * 24
        http = FakeHttpClient(
            routes=[
                ("https://raw.githubusercontent.com", 404, b"Not Found"),
                ("https://ghfast.top/", 404, b"Not Found"),
                ("https://ghproxy.net/", 200, manifest),
            ]
        )
        client = ManifestArchiveClient(http=http, direct_http=http, retries=0)
        item = client.fetch_single_manifest(BRANCH, "731", "7127896784363312296")

        assert item is not None
        assert item.data == manifest
        assert item.filename == "731_7127896784363312296.manifest"
        # 镜像按 raw → ghfast → ghproxy 顺序回退
        raw_idx = next(i for i, u in enumerate(http.calls) if "raw.githubusercontent.com" in u)
        fast_idx = next(i for i, u in enumerate(http.calls) if "ghfast.top" in u)
        proxy_idx = next(i for i, u in enumerate(http.calls) if "ghproxy.net" in u)
        assert raw_idx < fast_idx < proxy_idx

    def test_fetch_keys_from_branch(self):
        http = FakeHttpClient(
            routes=[
                ("/730/config.json", 200, SAMPLE_CONFIG),
                ("/730/Key.vdf", 200, SAMPLE_KEYS),
            ]
        )
        client = ManifestArchiveClient(http=http, direct_http=http, retries=0)
        keys = client.fetch_keys(BRANCH)
        assert set(keys) == {"731", "732"}

    def test_probe_branch_caches_missing_repo(self):
        http = FakeHttpClient(default=404)
        client = ManifestArchiveClient(http=http, direct_http=http, retries=0)
        assert client.probe_branch("999999", "Auiowu/ManifestAutoUpdate") is False
        calls_after_first = len(http.calls)
        assert client.probe_branch("999999", "Auiowu/ManifestAutoUpdate") is False
        assert len(http.calls) == calls_after_first  # 已证伪的组合不再重复请求


def test_archive_manifest_size_is_derived_from_payload():
    item = ArchiveManifest(depot_id="1", gid="2", data=STEAM_MANIFEST_MAGIC + b"x" * 10)
    assert item.size == 14
    assert item.filename == "1_2.manifest"
    assert item.has_valid_magic is True


def test_manifest_archive_helpers():
    archive = ManifestArchive(app_id="730")
    assert archive.gid_map == {}
    assert archive.depot_ids == []
    assert archive.get("1") is None
