"""诊断中心：日志过滤、崩溃扫描、脱敏与诊断包导出测试。"""
from __future__ import annotations

import json
import zipfile
from pathlib import Path

from core.diagnostics import (
    collect_snapshot,
    export_diagnostics,
    filter_log_entries,
    log_sources,
    parse_log_file,
    redact_config,
    redact_text,
    scan_crash_artifacts,
    snapshot_text,
)


def _write_logs(log_dir: Path) -> None:
    log_dir.mkdir(parents=True)
    (log_dir / "2026-10-28.log").write_text(
        "\n".join(
            [
                "[2026-10-28 10:00:00] [INFO] [core.test] hello",
                "[2026-10-28 10:01:00] [ERROR] [core.test] boom",
                "Traceback (most recent call last):",
                '  File "x.py", line 1',
                "[2026-10-28 10:02:00] [WARNING] [gui.test] careful",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (log_dir / "2026-10-29.log").write_text(
        "[2026-10-29 11:00:00] [INFO] [core.other] newer\n", encoding="utf-8"
    )
    (log_dir / "crash.log").write_text("fatal crash\n", encoding="utf-8")
    (log_dir / "stderr.log").write_text("stderr line\n", encoding="utf-8")


def test_log_sources_and_parse(tmp_path):
    logs = tmp_path / "logs"
    _write_logs(logs)
    sources = log_sources(logs)
    labels = [source.label for source in sources]
    assert "2026-10-28" in labels
    assert "2026-10-29" in labels
    assert any("crash.log" in label for label in labels)
    assert sources[0].paths

    entries = parse_log_file(logs / "2026-10-28.log")
    assert len(entries) == 3
    assert entries[1].level == "ERROR"
    assert "Traceback" in entries[1].message


def test_filter_log_entries_by_level_keyword_date(tmp_path):
    logs = tmp_path / "logs"
    _write_logs(logs)
    entries = parse_log_file(logs / "2026-10-28.log")
    assert len(filter_log_entries(entries, min_level="ERROR")) == 1
    assert len(filter_log_entries(entries, keyword="careful")) == 1
    assert len(filter_log_entries(entries, date_prefix="2026-10-29")) == 0
    assert len(filter_log_entries(entries, min_level="WARNING")) == 2


def test_scan_crash_artifacts(tmp_path):
    logs = tmp_path / "logs"
    data = tmp_path / "data"
    steam = tmp_path / "Steam"
    _write_logs(logs)
    data.mkdir()
    (data / "app.dmp").write_bytes(b"dump")
    (steam / "dumps").mkdir(parents=True)
    (steam / "dumps" / "steam.dmp").write_bytes(b"dump2")

    artifacts = scan_crash_artifacts(str(steam), log_dir=logs, data_dir=data)
    names = {artifact.name for artifact in artifacts}
    assert {"crash.log", "stderr.log", "app.dmp", "steam.dmp"} <= names
    kinds = {artifact.name: artifact.kind for artifact in artifacts}
    assert kinds["steam.dmp"] == "Steam 转储"


def test_redaction():
    data = {
        "manifest_api_key": "secret-key",
        "nested": {"webdav_password": "pw", "normal": 1},
        "list": [{"token": "t"}],
    }
    redacted = redact_config(data)
    assert redacted["manifest_api_key"] == "***REDACTED***"
    assert redacted["nested"]["webdav_password"] == "***REDACTED***"
    assert redacted["nested"]["normal"] == 1
    assert redacted["list"][0]["token"] == "***REDACTED***"

    text = '[webdav]\npassword = "secret"\napi_key = "abc"\nnormal = "ok"\n'
    cleaned = redact_text(text)
    assert "secret" not in cleaned
    assert "abc" not in cleaned
    assert 'normal = "ok"' in cleaned


def test_collect_snapshot_and_export(tmp_path):
    logs = tmp_path / "logs"
    data = tmp_path / "data"
    steam = tmp_path / "Steam"
    _write_logs(logs)
    data.mkdir()
    (data / "app.dmp").write_bytes(b"dump")
    (steam / "dumps").mkdir(parents=True)
    (steam / "dumps" / "steam.dmp").write_bytes(b"dump2")
    (steam / "opensteamtool.toml").write_text(
        '[denuvo]\nmode = "normal"\n\n[webdav]\npassword = "secret"\n',
        encoding="utf-8",
    )
    config_file = tmp_path / "config.json"
    config_file.write_text(
        json.dumps({"steam_path": str(steam), "manifest_api_key": "secret-key"}),
        encoding="utf-8",
    )

    snapshot = collect_snapshot(str(steam), log_dir=logs, data_dir=data)
    assert snapshot.denuvo_mode == "normal"
    assert snapshot.generated_at
    text = snapshot_text(snapshot)
    assert "诊断快照" in text
    assert "Denuvo 模式：normal" in text

    destination = tmp_path / "diag.zip"
    ok, message = export_diagnostics(
        destination,
        steam_path=str(steam),
        log_dir=logs,
        data_dir=data,
        config_file=config_file,
    )
    assert ok, message
    assert destination.is_file()
    with zipfile.ZipFile(destination) as archive:
        names = set(archive.namelist())
        assert "summary.txt" in names
        assert "config/config.redacted.json" in names
        assert "config/opensteamtool.redacted.toml" in names
        assert "logs/2026-10-28.log" in names
        assert "dumps/steam.dmp" in names
        assert "secret-key" not in archive.read("config/config.redacted.json").decode("utf-8")
        assert "secret" not in archive.read("config/opensteamtool.redacted.toml").decode("utf-8")
        assert "***REDACTED***" in archive.read("config/opensteamtool.redacted.toml").decode("utf-8")
