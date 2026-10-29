"""
SettingsPage — 应用程序设置界面
================================

包含功能：
1. ManifestHub API Key 配置、获取指南与连通性验证
2. 多源清单下载与 GitHub 加速镜像审查
3. 本地磁盘全盘扫描与第三方工具兼容配置
4. 界面与主题偏好配置
"""
from __future__ import annotations

import os
import time
import httpx
from typing import Any

from PyQt6.QtCore import Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QDesktopServices
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QFrame,
    QDialog, QTableWidget, QTableWidgetItem, QAbstractItemView,
    QPlainTextEdit,
)

from qfluentwidgets import (
    ScrollArea, SubtitleLabel, CaptionLabel, BodyLabel, StrongBodyLabel,
    PrimaryPushButton, PushButton, LineEdit, SwitchButton, ComboBox,
    CardWidget, GroupHeaderCardWidget, InfoBar, InfoBarPosition,
    FluentIcon,
)

from config import (
    SSL_VERIFY, MANIFEST_GITHUB_REPOS, GITHUB_RAW_MIRRORS,
    MANIFESTHUB_API_URL, MANIFESTHUB_API_KEY, STEAM_PATTERN_URL_TEMPLATES,
)
from core.config_manager import ConfigManager
from core.manifest_downloader import fetch_manifesthub_upstream_info
from utils.async_worker import AsyncWorker
from utils.http_client import get_system_proxy
from utils.logger import setup_logger

logger = setup_logger(__name__)


class SettingsPage(ScrollArea):
    """设置页面"""

    scan_requested = pyqtSignal()  # 请求重新全盘扫描信号

    def __init__(self, config_manager: ConfigManager, bridge=None, game_manager=None, parent=None):
        super().__init__(parent)
        self._config = config_manager
        self._bridge = bridge
        self._game_manager = game_manager
        self._workers: list[AsyncWorker] = []

        self.setObjectName("settingsPage")
        self.setWidgetResizable(True)

        self._container = QWidget()
        self._container.setObjectName("settingsContainer")
        self.setWidget(self._container)

        self._main_layout = QVBoxLayout(self._container)
        self._main_layout.setContentsMargins(30, 30, 30, 30)
        self._main_layout.setSpacing(20)

        self._init_ui()
        self._load_settings()

        self.setStyleSheet("SettingsPage { background: transparent; }")
        self._container.setStyleSheet("QWidget#settingsContainer { background: transparent; }")

    def _init_ui(self):
        self._main_layout.addWidget(SubtitleLabel("设置", self))

        # 1. ManifestHub API 配置卡片
        self._build_manifesthub_card()

        # 2. 多源下载体系审查与测速卡片
        self._build_sources_card()

        # 3. 内核功能开关（OnlineFix / 云存档重定向）
        self._build_kernel_features_card()

        # 4. Manifest 解析服务探测与回退
        self._build_manifest_router_card()

        # 5. 签名镜像与本地缓存
        self._build_pattern_mirror_card()

        # 6. 本地全盘扫描与第三方工具兼容卡片
        self._build_scan_card()

        self._main_layout.addStretch(1)

    # ── 1. ManifestHub API 配置与动态同步 ────────────────────────────

    def _build_manifesthub_card(self):
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(FluentIcon.CLOUD.icon().pixmap(24, 24))
        header_layout.addWidget(icon_lbl)
        title_lbl = StrongBodyLabel("ManifestHub API 接口与授权配置", card)
        header_layout.addWidget(title_lbl)
        header_layout.addStretch(1)
        card_layout.addLayout(header_layout)

        # 动态地址展示行
        dynamic_row = QHBoxLayout()
        current_api = self._config.get("manifesthub_api_url", MANIFESTHUB_API_URL)
        current_web = self._config.get("manifesthub_web_url", "https://manifesthub2.filegear-sg.me")

        self.lbl_api_url = CaptionLabel(f"当前官方 API 接口：{current_api}", card)
        self.lbl_api_url.setStyleSheet("color: #888888;")
        dynamic_row.addWidget(self.lbl_api_url, 1)

        self.btn_sync_upstream = PushButton(FluentIcon.SYNC, "从官方 GitHub 动态同步最新地址", card)
        self.btn_sync_upstream.setFixedHeight(30)
        self.btn_sync_upstream.clicked.connect(self._on_sync_upstream_info)
        dynamic_row.addWidget(self.btn_sync_upstream)
        card_layout.addLayout(dynamic_row)

        # 指引说明
        desc_text = (
            "ManifestHub 是 SteamAutoCracks 生态维护的公共清单服务，覆盖海量受保护 Depot 清单。<br>"
            "<b>💡 如何获取免费专属 API Key？</b><br>"
            "1. 点击下方『打开官方网页获取 Key』按钮，在浏览器中打开官网；<br>"
            "2. 官网配备了 Cloudflare 人机验证，因此无法全自动后台静默获取，需由您在浏览器中<b>手动完成人机验证</b>；<br>"
            "3. 验证通过后系统将颁发免费 24 小时 API Key，复制并粘贴到下方输入框，点击『保存 Key』即可；<br>"
            "<i>注：即使未配置 Key，系统依然会自动通过内置的 GitHub 社区源、国内加速链路与 Steam 官方 CDN 容灾拉取清单。</i>"
        )
        desc_lbl = BodyLabel(desc_text, card)
        desc_lbl.setWordWrap(True)
        desc_lbl.setTextFormat(Qt.TextFormat.RichText)
        card_layout.addWidget(desc_lbl)

        # 输入与操作栏
        input_layout = QHBoxLayout()
        self.api_key_input = LineEdit(card)
        self.api_key_input.setPlaceholderText("请输入您的 ManifestHub API Key（可选）...")
        self.api_key_input.setClearButtonEnabled(True)
        self.api_key_input.setFixedHeight(34)
        input_layout.addWidget(self.api_key_input, 1)

        self.btn_save_key = PrimaryPushButton(FluentIcon.SAVE, "保存 Key", card)
        self.btn_save_key.setFixedHeight(34)
        self.btn_save_key.clicked.connect(self._on_save_api_key)
        input_layout.addWidget(self.btn_save_key)

        self.btn_test_key = PushButton(FluentIcon.ACCEPT, "测试鉴权", card)
        self.btn_test_key.setFixedHeight(34)
        self.btn_test_key.clicked.connect(self._on_test_api_key)
        input_layout.addWidget(self.btn_test_key)

        self.btn_open_web = PushButton(FluentIcon.GLOBE, "打开官方网页获取 Key", card)
        self.btn_open_web.setFixedHeight(34)
        self.btn_open_web.clicked.connect(self._on_open_manifesthub_web)
        input_layout.addWidget(self.btn_open_web)

        card_layout.addLayout(input_layout)
        self._main_layout.addWidget(card)

    def _on_sync_upstream_info(self):
        self.btn_sync_upstream.setEnabled(False)
        InfoBar.info("同步中", "正在通过国内加速镜像读取上游 GitHub README 获取最新 API 配置...", parent=self, position=InfoBarPosition.TOP, duration=2500)

        def _worker():
            return fetch_manifesthub_upstream_info(timeout=8.0)

        def _on_done(info: dict):
            self.btn_sync_upstream.setEnabled(True)
            if info and info.get("api_url"):
                self._config.set("manifesthub_api_url", info["api_url"])
                self._config.set("manifesthub_web_url", info["web_url"])
                self.lbl_api_url.setText(f"当前官方 API 接口：{info['api_url']}")
                InfoBar.success(
                    "同步成功",
                    f"已成功获取官方最新接口！\nAPI: {info['api_url']}\nPortal: {info['web_url']}",
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=5000,
                )
            else:
                InfoBar.warning("同步完成", "未检测到地址变动，继续沿用当前配置", parent=self, position=InfoBarPosition.TOP)

        worker = AsyncWorker(_worker)
        worker.finished_with_result.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._workers.append(worker)

    def _on_save_api_key(self):
        key = self.api_key_input.text().strip()
        self._config.set("manifesthub_api_key", key)
        InfoBar.success(
            "保存成功",
            "ManifestHub API Key 配置已更新并持久化保存",
            parent=self,
            position=InfoBarPosition.TOP,
            duration=3000,
        )

    def _on_test_api_key(self):
        key = self.api_key_input.text().strip()
        if not key:
            InfoBar.warning(
                "提示",
                "请先输入要测试的 ManifestHub API Key",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=3500,
            )
            return

        self.btn_test_key.setEnabled(False)
        InfoBar.info("测试中", "正在向 ManifestHub API 发起鉴权握手测试...", parent=self, position=InfoBarPosition.TOP, duration=2500)

        api_url = self._config.get("manifesthub_api_url", MANIFESTHUB_API_URL)

        def _worker_test():
            url = f"{api_url}?apikey={key}&depotid=731&manifestid=test"
            try:
                with httpx.Client(proxy=get_system_proxy(), timeout=12.0, verify=SSL_VERIFY) as client:
                    resp = client.get(url)
                    return resp.status_code, resp.text
            except Exception as e:
                return -1, str(e)

        worker = AsyncWorker(_worker_test)
        worker.finished_with_result.connect(self._on_api_test_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._workers.append(worker)

    def _on_api_test_finished(self, result: tuple[int, str]):
        self.btn_test_key.setEnabled(True)
        status_code, body = result

        if status_code in (401, 403) or "invalid api key" in body.lower():
            InfoBar.error(
                "验证失败",
                f"API Key 无效或未通过鉴权 ({body[:100]})，请在浏览器中重新获取有效 Key",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )
        elif status_code == 200 or "manifest not found" in body.lower() or "not found" in body.lower() or status_code in (400, 404):
            InfoBar.success(
                "验证成功",
                "ManifestHub API 握手成功！您的 API Key 有效，可正常使用高级接口加速下载。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
        else:
            InfoBar.warning(
                "网络异常",
                f"测试请求返回异常 (HTTP {status_code}): {body[:120]}，请检查系统网络或代理设置",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=6000,
            )

    def _on_open_manifesthub_web(self):
        web_url = self._config.get("manifesthub_web_url", "https://manifesthub2.filegear-sg.me")
        QDesktopServices.openUrl(QUrl(web_url))

    # ── 2. 多源下载体系 4 级优先级状态审查与测速 ────────────────────

    def _build_sources_card(self):
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(FluentIcon.DOWNLOAD.icon().pixmap(24, 24))
        header_layout.addWidget(icon_lbl)
        title_lbl = StrongBodyLabel("多源清单下载体系状态与逐级测速", card)
        header_layout.addWidget(title_lbl)
        header_layout.addStretch(1)
        card_layout.addLayout(header_layout)

        desc = BodyLabel(
            "为确保入库游戏时清单 100% 自动解析补全，软件内置了 4 级层级容灾引擎。系统下载清单时将严格按如下优先级依次尝试：",
            card,
        )
        desc.setWordWrap(True)
        card_layout.addWidget(desc)

        # 4 级优先级状态容器
        status_box = QFrame(card)
        status_box.setStyleSheet("background: rgba(0, 0, 0, 0.04); border-radius: 6px; padding: 6px;")
        sb_layout = QVBoxLayout(status_box)
        sb_layout.setSpacing(10)
        sb_layout.setContentsMargins(12, 10, 12, 10)

        # Tier 1
        t1_row = QHBoxLayout()
        t1_title = BodyLabel("<b>[第 1 优先级] AppID 分支归档</b>（ManifestAutoUpdate 整包：config.json + Key.vdf + 真实 {depot}_{gid}.manifest，自动解包 Raw DEFLATE）", status_box)
        t1_title.setTextFormat(Qt.TextFormat.RichText)
        self.t1_status_lbl = CaptionLabel("未测试", status_box)
        self.t1_status_lbl.setStyleSheet("color: #888888; font-weight: bold;")
        t1_row.addWidget(t1_title, 1)
        t1_row.addWidget(self.t1_status_lbl)
        sb_layout.addLayout(t1_row)

        # Tier 2
        t2_row = QHBoxLayout()
        t2_title = BodyLabel("<b>[第 2 优先级] 国内高速加速镜像</b>（自动轮询 ghfast.top、ghproxy.net 等加速链路）", status_box)
        t2_title.setTextFormat(Qt.TextFormat.RichText)
        self.t2_status_lbl = CaptionLabel("未测试", status_box)
        self.t2_status_lbl.setStyleSheet("color: #888888; font-weight: bold;")
        t2_row.addWidget(t2_title, 1)
        t2_row.addWidget(self.t2_status_lbl)
        sb_layout.addLayout(t2_row)

        # Tier 3
        t3_row = QHBoxLayout()
        t3_title = BodyLabel("<b>[第 3 优先级] ManifestHub API</b>（配置 API Key 生效，覆盖海量受保护 Depot）", status_box)
        t3_title.setTextFormat(Qt.TextFormat.RichText)
        self.t3_status_lbl = CaptionLabel("未测试", status_box)
        self.t3_status_lbl.setStyleSheet("color: #888888; font-weight: bold;")
        t3_row.addWidget(t3_title, 1)
        t3_row.addWidget(self.t3_status_lbl)
        sb_layout.addLayout(t3_row)

        # Tier 4
        t4_row = QHBoxLayout()
        t4_title = BodyLabel("<b>[第 4 优先级] Valve 官方 CDN</b>（通过 Manifest Request Code 内容码直接拉取，最后回退免锁 Depot 直连）", status_box)
        t4_title.setTextFormat(Qt.TextFormat.RichText)
        self.t4_status_lbl = CaptionLabel("未测试", status_box)
        self.t4_status_lbl.setStyleSheet("color: #888888; font-weight: bold;")
        t4_row.addWidget(t4_title, 1)
        t4_row.addWidget(self.t4_status_lbl)
        sb_layout.addLayout(t4_row)

        card_layout.addWidget(status_box)

        # 测试按钮
        btn_row = QHBoxLayout()
        self.btn_test_mirrors = PrimaryPushButton(FluentIcon.SPEED_HIGH, "测试 4 级多源下载状态与延迟", card)
        self.btn_test_mirrors.clicked.connect(self._on_test_four_tiers)
        btn_row.addWidget(self.btn_test_mirrors)
        btn_row.addStretch(1)
        card_layout.addLayout(btn_row)

        self._main_layout.addWidget(card)

    def _on_test_four_tiers(self):
        self.btn_test_mirrors.setEnabled(False)
        self.t1_status_lbl.setText("正在测速...")
        self.t1_status_lbl.setStyleSheet("color: #108ee9; font-weight: bold;")
        self.t2_status_lbl.setText("正在测速...")
        self.t2_status_lbl.setStyleSheet("color: #108ee9; font-weight: bold;")
        self.t3_status_lbl.setText("正在测速...")
        self.t3_status_lbl.setStyleSheet("color: #108ee9; font-weight: bold;")
        self.t4_status_lbl.setText("正在测速...")
        self.t4_status_lbl.setStyleSheet("color: #108ee9; font-weight: bold;")

        api_url = self._config.get("manifesthub_api_url", MANIFESTHUB_API_URL)
        api_key = self._config.get("manifesthub_api_key", "").strip()

        def _test_fn():
            proxy = get_system_proxy()
            results = {}

            # Tier 1: GitHub 官方 Raw
            t0 = time.time()
            try:
                with httpx.Client(proxy=proxy, timeout=5.0, verify=SSL_VERIFY, follow_redirects=True) as client:
                    resp = client.head("https://raw.githubusercontent.com")
                    ms = int((time.time() - t0) * 1000)
                    results["p1"] = (True, ms, f"连通正常 ({ms}ms)")
            except Exception:
                results["p1"] = (False, -1, "未连通 / 无法直连（国内无代理时正常，自动切入优先级2）")

            # Tier 2: 国内加速镜像
            t0 = time.time()
            mirror_ok = False
            best_ms = 9999
            for mirror in ["https://ghfast.top", "https://ghproxy.net"]:
                try:
                    mt0 = time.time()
                    with httpx.Client(proxy=proxy, timeout=5.0, verify=SSL_VERIFY, follow_redirects=True) as client:
                        resp = client.get(mirror)
                        if resp.status_code < 500:
                            mms = int((time.time() - mt0) * 1000)
                            best_ms = min(best_ms, mms)
                            mirror_ok = True
                except Exception:
                    pass
            if mirror_ok:
                results["p2"] = (True, best_ms, f"连通正常 ({best_ms}ms，加速就绪)")
            else:
                results["p2"] = (False, -1, "未连通 / 镜像网络异常")

            # Tier 3: ManifestHub API
            t0 = time.time()
            try:
                if api_key:
                    test_url = f"{api_url}?apikey={api_key}&depotid=731&manifestid=test"
                else:
                    test_url = api_url
                with httpx.Client(proxy=proxy, timeout=6.0, verify=SSL_VERIFY) as client:
                    resp = client.get(test_url)
                    ms = int((time.time() - t0) * 1000)
                    if api_key:
                        if resp.status_code in (200, 400, 404):
                            results["p3"] = (True, ms, f"连通且已鉴权 ({ms}ms，高级源已就绪)")
                        elif resp.status_code in (401, 403):
                            results["p3"] = (False, ms, f"已连通 ({ms}ms)，但 Key 已失效需重新获取")
                        else:
                            results["p3"] = (True, ms, f"连通响应 ({ms}ms，HTTP {resp.status_code})")
                    else:
                        results["p3"] = (True, ms, f"接口连通 ({ms}ms，未配置 API Key，填入后启用)")
            except Exception:
                results["p3"] = (False, -1, "未连通 / 接口离线或超时")

            # Tier 4: Steam 官方 CDN
            t0 = time.time()
            try:
                with httpx.Client(proxy=proxy, timeout=5.0, verify=SSL_VERIFY) as client:
                    resp = client.get("https://cdn.akamai.steamstatic.com")
                    ms = int((time.time() - t0) * 1000)
                    results["p4"] = (True, ms, f"连通正常 ({ms}ms，官方公开分发可用)")
            except Exception:
                results["p4"] = (False, -1, "未连通 / 官方 CDN 受限")

            return results

        def _on_test_finished(res: dict):
            self.btn_test_mirrors.setEnabled(True)

            def _apply_lbl(lbl: CaptionLabel, res_tuple: tuple[bool, int, str]):
                ok, ms, text = res_tuple
                lbl.setText(text)
                if ok and "未配置" in text:
                    lbl.setStyleSheet("color: #fa8c16; font-weight: bold;")
                elif ok:
                    lbl.setStyleSheet("color: #52c41a; font-weight: bold;")
                else:
                    lbl.setStyleSheet("color: #f5222d; font-weight: bold;")

            if "p1" in res:
                _apply_lbl(self.t1_status_lbl, res["p1"])
            if "p2" in res:
                _apply_lbl(self.t2_status_lbl, res["p2"])
            if "p3" in res:
                _apply_lbl(self.t3_status_lbl, res["p3"])
            if "p4" in res:
                _apply_lbl(self.t4_status_lbl, res["p4"])

        worker = AsyncWorker(_test_fn)
        worker.finished_with_result.connect(_on_test_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._workers.append(worker)

    # ── 3. 内核功能开关（OnlineFix / 云存档重定向）─────────────────

    def _steam_root(self) -> str:
        if not self._bridge:
            return ""
        try:
            return str(self._bridge.get_steam_path() or "")
        except Exception:  # noqa: BLE001
            return ""

    def _build_kernel_features_card(self):
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(FluentIcon.DEVELOPER_TOOLS.icon().pixmap(24, 24))
        header_layout.addWidget(icon_lbl)
        header_layout.addWidget(StrongBodyLabel("内核功能开关（ZSteamTool v1.2.x）", card))
        header_layout.addStretch(1)
        card_layout.addLayout(header_layout)

        note = CaptionLabel(
            "以下选项写入 <Steam>/opensteamtool.toml，重启 Steam 后生效；已有用户配置会被保留。",
            card,
        )
        note.setStyleSheet("color: #888888;")
        card_layout.addWidget(note)

        # OnlineFix 会话环境
        row1 = QHBoxLayout()
        vbox1 = QVBoxLayout()
        vbox1.addWidget(BodyLabel("OnlineFix 会话环境（[onlinefix] env_session）", card))
        sub1 = CaptionLabel(
            "为 OnlineFix 联机游戏注入会话环境变量。使用 OnlineFix 补丁时开启；纯单机可关闭。", card
        )
        sub1.setStyleSheet("color: #888888;")
        vbox1.addWidget(sub1)
        self.switch_onlinefix = SwitchButton(card)
        self.switch_onlinefix.checkedChanged.connect(self._on_onlinefix_changed)
        row1.addLayout(vbox1, 1)
        row1.addWidget(self.switch_onlinefix)
        card_layout.addLayout(row1)

        # 云存档重定向
        row2 = QHBoxLayout()
        vbox2 = QVBoxLayout()
        vbox2.addWidget(BodyLabel("云存档重定向（[cloud] enabled）", card))
        sub2 = CaptionLabel(
            "启用后内核会加载 cloud_redirect.dll，把 Steam 云存档改写到本地目录。"
            "该 DLL 未随本项目分发，需自行获取并放入 Steam 根目录。", card
        )
        sub2.setStyleSheet("color: #888888;")
        vbox2.addWidget(sub2)
        self.switch_cloud = SwitchButton(card)
        self.switch_cloud.checkedChanged.connect(self._on_cloud_toggled)
        row2.addLayout(vbox2, 1)
        row2.addWidget(self.switch_cloud)
        card_layout.addLayout(row2)

        lib_row = QHBoxLayout()
        lib_row.addWidget(BodyLabel("DLL 文件名：", card))
        self.cloud_library_input = LineEdit(card)
        self.cloud_library_input.setPlaceholderText("cloud_redirect.dll")
        self.cloud_library_input.setFixedHeight(32)
        lib_row.addWidget(self.cloud_library_input, 1)
        btn_detect = PushButton(FluentIcon.SYNC, "重新检测", card)
        btn_detect.clicked.connect(self._refresh_cloud_status)
        lib_row.addWidget(btn_detect)
        btn_save_cloud = PrimaryPushButton(FluentIcon.SAVE, "保存云存档设置", card)
        btn_save_cloud.clicked.connect(self._on_save_cloud)
        lib_row.addWidget(btn_save_cloud)
        card_layout.addLayout(lib_row)

        self.cloud_status_lbl = CaptionLabel("未检测", card)
        self.cloud_status_lbl.setWordWrap(True)
        self.cloud_status_lbl.setStyleSheet("color: #888888;")
        card_layout.addWidget(self.cloud_status_lbl)

        self._main_layout.addWidget(card)

    def _on_onlinefix_changed(self, checked: bool):
        if not self._bridge:
            InfoBar.warning("不可用", "未检测到 Steam，无法写入内核配置", parent=self, position=InfoBarPosition.TOP)
            return
        ok, message = self._bridge.update_kernel_config({"onlinefix": {"env_session": bool(checked)}})
        if ok:
            InfoBar.success("已保存", f"OnlineFix 会话环境已{'开启' if checked else '关闭'}", parent=self, position=InfoBarPosition.TOP, duration=2500)
        else:
            InfoBar.error("保存失败", message, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def _on_cloud_toggled(self, checked: bool):
        # 仅记录到内存；点“保存云存档设置”后写入 TOML，避免只改一半。
        self._refresh_cloud_status()

    def _on_save_cloud(self):
        if not self._bridge:
            InfoBar.warning("不可用", "未检测到 Steam，无法写入内核配置", parent=self, position=InfoBarPosition.TOP)
            return
        library = self.cloud_library_input.text().strip() or "cloud_redirect.dll"
        ok, message = self._bridge.update_kernel_config(
            {"cloud": {"enabled": self.switch_cloud.isChecked(), "library": library}}
        )
        if ok:
            InfoBar.success("已保存", "云存档重定向设置已写入 opensteamtool.toml", parent=self, position=InfoBarPosition.TOP, duration=3000)
        else:
            InfoBar.error("保存失败", message, parent=self, position=InfoBarPosition.TOP, duration=5000)
        self._refresh_cloud_status()

    def _refresh_cloud_status(self):
        steam = self._steam_root()
        library = self.cloud_library_input.text().strip() or "cloud_redirect.dll"
        if not steam:
            self.cloud_status_lbl.setText("⚠️ 未检测到 Steam 安装目录")
            self.cloud_status_lbl.setStyleSheet("color: #fa8c16;")
            return
        path = os.path.join(steam, library)
        if os.path.isfile(path):
            self.cloud_status_lbl.setText(f"✅ 已检测到 {path}")
            self.cloud_status_lbl.setStyleSheet("color: #52c41a;")
        else:
            self.cloud_status_lbl.setText(f"⚠️ 未找到 {path}，启用后云存档重定向不会生效")
            self.cloud_status_lbl.setStyleSheet("color: #fa8c16;")

    # ── 4. Manifest 解析服务探测与回退 ────────────────────────────

    _MANIFEST_PROVIDERS = ("wudrm", "steamrun", "opensteamtool")
    _MANIFEST_PROBE_URLS = {
        "wudrm": "http://gmrc.wudrm.com/manifest/3938499722445376447",
        "steamrun": "https://manifest.steam.run/api/manifest/3938499722445376447",
        "opensteamtool": "https://manifest.opensteamtool.com/3938499722445376447",
    }

    def _build_manifest_router_card(self):
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(FluentIcon.CONNECT.icon().pixmap(24, 24))
        header_layout.addWidget(icon_lbl)
        header_layout.addWidget(StrongBodyLabel("Manifest 解析服务与自动回退", card))
        header_layout.addStretch(1)
        card_layout.addLayout(header_layout)

        desc = CaptionLabel(
            "内核通过 [manifest] url 选择清单请求码服务。wudrm 国内可用；steamrun 作为回退；"
            "opensteamtool 官方接口可能被 Cloudflare 拦截。config/lua/manifest.lua 中的 "
            "fetch_manifest_code() 会优先尝试 wudrm，再回退 steamrun。",
            card,
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #888888;")
        card_layout.addWidget(desc)

        row = QHBoxLayout()
        row.addWidget(BodyLabel("当前服务：", card))
        self.manifest_combo = ComboBox(card)
        self.manifest_combo.addItems(list(self._MANIFEST_PROVIDERS))
        self.manifest_combo.setFixedWidth(180)
        row.addWidget(self.manifest_combo)
        btn_save = PrimaryPushButton(FluentIcon.SAVE, "保存服务", card)
        btn_save.clicked.connect(self._on_save_manifest_provider)
        row.addWidget(btn_save)
        btn_probe = PushButton(FluentIcon.SPEED_HIGH, "探测并自动选择", card)
        btn_probe.clicked.connect(self._on_probe_manifest)
        row.addWidget(btn_probe)
        row.addStretch(1)
        card_layout.addLayout(row)

        self.manifest_status_lbl = CaptionLabel("尚未探测", card)
        self.manifest_status_lbl.setWordWrap(True)
        self.manifest_status_lbl.setStyleSheet("color: #888888;")
        card_layout.addWidget(self.manifest_status_lbl)

        self._main_layout.addWidget(card)

    def _on_save_manifest_provider(self):
        if not self._bridge:
            InfoBar.warning("不可用", "未检测到 Steam，无法写入内核配置", parent=self, position=InfoBarPosition.TOP)
            return
        provider = self.manifest_combo.currentText().strip()
        ok, message = self._bridge.update_kernel_config({"manifest": {"url": provider}})
        if ok:
            InfoBar.success("已保存", f"Manifest 服务已切换为 {provider}", parent=self, position=InfoBarPosition.TOP, duration=2500)
        else:
            InfoBar.error("保存失败", message, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def _on_probe_manifest(self):
        if not self._bridge:
            InfoBar.warning("不可用", "未检测到 Steam", parent=self, position=InfoBarPosition.TOP)
            return
        self.manifest_status_lbl.setText("正在探测 wudrm / steamrun / opensteamtool …")
        self.manifest_status_lbl.setStyleSheet("color: #108ee9;")

        def _probe():
            proxy = get_system_proxy()
            results: list[tuple[str, bool, str]] = []
            for name in self._MANIFEST_PROVIDERS:
                url = self._MANIFEST_PROBE_URLS[name]
                t0 = time.time()
                try:
                    with httpx.Client(proxy=proxy, timeout=8.0, verify=SSL_VERIFY, follow_redirects=True) as client:
                        resp = client.get(url)
                    ms = int((time.time() - t0) * 1000)
                    usable = resp.status_code in (200, 204, 400, 404)
                    results.append((name, usable, f"{resp.status_code} ({ms}ms)"))
                except Exception as exc:  # noqa: BLE001
                    results.append((name, False, f"连接失败: {type(exc).__name__}"))
            return results

        def _done(results: list[tuple[str, bool, str]]):
            text = "；".join(f"{name} {'✅' if ok else '❌'} {detail}" for name, ok, detail in results)
            self.manifest_status_lbl.setText(text)
            picked = next((name for name, ok, _ in results if ok), "")
            if picked:
                self.manifest_combo.setCurrentText(picked)
                ok, message = self._bridge.update_kernel_config({"manifest": {"url": picked}})
                self.manifest_status_lbl.setStyleSheet("color: #52c41a;")
                if ok:
                    InfoBar.success("探测完成", f"已自动选择可用服务：{picked}", parent=self, position=InfoBarPosition.TOP, duration=4000)
                else:
                    InfoBar.warning("探测完成", f"可用服务：{picked}，但写入配置失败：{message}", parent=self, position=InfoBarPosition.TOP, duration=5000)
            else:
                self.manifest_status_lbl.setStyleSheet("color: #f5222d;")
                InfoBar.warning(
                    "探测失败",
                    "三个服务均未返回可用响应，保留当前配置；可稍后重试或检查网络/代理。",
                    parent=self,
                    position=InfoBarPosition.TOP,
                    duration=6000,
                )

        worker = AsyncWorker(_probe)
        worker.finished_with_result.connect(_done, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._workers.append(worker)

    # ── 5. 签名镜像与本地缓存 ────────────────────────────────────

    def _build_pattern_mirror_card(self):
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(FluentIcon.GLOBE.icon().pixmap(24, 24))
        header_layout.addWidget(icon_lbl)
        header_layout.addWidget(StrongBodyLabel("签名镜像与本地缓存（[remote]）", card))
        header_layout.addStretch(1)
        card_layout.addLayout(header_layout)

        desc = CaptionLabel(
            "当内置/本地生成的 Steam 签名失效时，内核会按顺序从下列镜像下载。"
            "每行一个 URL 模板，必须包含 {channel}、{component}、{sha256} 占位符。",
            card,
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #888888;")
        card_layout.addWidget(desc)

        self.remote_templates_edit = QPlainTextEdit(card)
        self.remote_templates_edit.setPlaceholderText(
            "https://raw.githubusercontent.com/<owner>/<repo>/main/{channel}/{component}/{sha256}.toml"
        )
        self.remote_templates_edit.setFixedHeight(96)
        card_layout.addWidget(self.remote_templates_edit)

        row = QHBoxLayout()
        self.switch_force_refresh = SwitchButton(card)
        self.switch_force_refresh.setOnText("强制刷新")
        self.switch_force_refresh.setOffText("按需刷新")
        row.addWidget(BodyLabel("缓存策略：", card))
        row.addWidget(self.switch_force_refresh)
        row.addSpacing(12)
        btn_save = PrimaryPushButton(FluentIcon.SAVE, "保存镜像设置", card)
        btn_save.clicked.connect(self._on_save_remote_settings)
        row.addWidget(btn_save)
        btn_sync = PushButton(FluentIcon.SYNC, "立即刷新签名缓存", card)
        btn_sync.clicked.connect(self._on_sync_patterns)
        row.addWidget(btn_sync)
        btn_clear = PushButton(FluentIcon.DELETE, "清除本地签名缓存", card)
        btn_clear.clicked.connect(self._on_clear_pattern_cache)
        row.addWidget(btn_clear)
        row.addStretch(1)
        card_layout.addLayout(row)

        self.remote_status_lbl = CaptionLabel("", card)
        self.remote_status_lbl.setWordWrap(True)
        self.remote_status_lbl.setStyleSheet("color: #888888;")
        card_layout.addWidget(self.remote_status_lbl)

        self._main_layout.addWidget(card)

    def _on_save_remote_settings(self):
        if not self._bridge:
            InfoBar.warning("不可用", "未检测到 Steam，无法写入内核配置", parent=self, position=InfoBarPosition.TOP)
            return
        raw = self.remote_templates_edit.toPlainText().splitlines()
        templates = [line.strip() for line in raw if line.strip()]
        if not templates:
            InfoBar.warning("不能为空", "至少保留一个镜像 URL 模板", parent=self, position=InfoBarPosition.TOP)
            return
        for template in templates:
            if not all(token in template for token in ("{channel}", "{component}", "{sha256}")):
                InfoBar.error("模板无效", f"缺少占位符：{template}", parent=self, position=InfoBarPosition.TOP, duration=5000)
                return
        ok, message = self._bridge.update_kernel_config(
            {
                "remote": {
                    "url_template": templates,
                    "force_refresh": self.switch_force_refresh.isChecked(),
                }
            }
        )
        if ok:
            InfoBar.success("已保存", "签名镜像设置已写入 opensteamtool.toml", parent=self, position=InfoBarPosition.TOP, duration=3000)
        else:
            InfoBar.error("保存失败", message, parent=self, position=InfoBarPosition.TOP, duration=5000)

    def _on_sync_patterns(self):
        steam = self._steam_root()
        if not steam:
            InfoBar.warning("不可用", "未检测到 Steam 安装目录", parent=self, position=InfoBarPosition.TOP)
            return
        self.remote_status_lbl.setText("正在解析 Steam 签名（首次可能需要几十秒）…")
        self.remote_status_lbl.setStyleSheet("color: #108ee9;")

        def _sync():
            from core.steam_pattern_sync import SteamPatternSync

            return SteamPatternSync(steam, force=True).sync()

        def _done(result: tuple[bool, str]):
            ok, message = result
            self.remote_status_lbl.setText(message)
            self.remote_status_lbl.setStyleSheet("color: #52c41a;" if ok else "color: #f5222d;")
            if ok:
                InfoBar.success("签名缓存就绪", message, parent=self, position=InfoBarPosition.TOP, duration=5000)
            else:
                InfoBar.warning("签名解析未完成", message, parent=self, position=InfoBarPosition.TOP, duration=7000)

        worker = AsyncWorker(_sync)
        worker.finished_with_result.connect(_done, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._workers.append(worker)

    def _on_clear_pattern_cache(self):
        steam = self._steam_root()
        if not steam:
            InfoBar.warning("不可用", "未检测到 Steam 安装目录", parent=self, position=InfoBarPosition.TOP)
            return
        try:
            from core.local_pattern_generator import LocalPatternGenerator

            removed = LocalPatternGenerator(steam).clear_cache()
            self.remote_status_lbl.setText(f"已清除 {removed} 个本地签名缓存文件，下次启动会重新生成")
            self.remote_status_lbl.setStyleSheet("color: #52c41a;")
            InfoBar.success("已清除", f"共删除 {removed} 个缓存文件", parent=self, position=InfoBarPosition.TOP, duration=3000)
        except Exception as exc:  # noqa: BLE001
            self.remote_status_lbl.setText(f"清除失败：{exc}")
            self.remote_status_lbl.setStyleSheet("color: #f5222d;")

    # ── 6. 本地磁盘 Steam 库扫描与第三方工具兼容 ─────────────────────

    def _build_scan_card(self):
        card = CardWidget(self)
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(20, 20, 20, 20)
        card_layout.setSpacing(14)

        header_layout = QHBoxLayout()
        icon_lbl = QLabel(self)
        icon_lbl.setPixmap(FluentIcon.FOLDER.icon().pixmap(24, 24))
        header_layout.addWidget(icon_lbl)
        title_lbl = StrongBodyLabel("本地磁盘 Steam 库扫描与管理范围", card)
        header_layout.addWidget(title_lbl)
        header_layout.addStretch(1)
        card_layout.addLayout(header_layout)

        lbl = BodyLabel(
            "配置本地游戏扫描范围。默认自动精准扫描当前 Steam 目录，杜绝全盘扫描造成的误导与意外导入。",
            card,
        )
        lbl.setWordWrap(True)
        card_layout.addWidget(lbl)

        # 开关项 1: 当前 Steam 库扫描
        row1 = QHBoxLayout()
        vbox1 = QVBoxLayout()
        r1_title = BodyLabel("自动扫描当前 Steam 库游戏（steamapps/appmanifest_*.acf）", card)
        r1_sub = CaptionLabel("默认开启。严格收敛在检测到的当前 Steam 安装目录及其关联库中，绝不擅自检索外部无关目录。", card)
        r1_sub.setStyleSheet("color: #888888;")
        vbox1.addWidget(r1_title)
        vbox1.addWidget(r1_sub)
        self.switch_scan_installed = SwitchButton(card)
        self.switch_scan_installed.setChecked(self._config.get("scan_installed_games", True))
        self.switch_scan_installed.checkedChanged.connect(lambda val: self._config.set("scan_installed_games", val))
        row1.addLayout(vbox1, 1)
        row1.addWidget(self.switch_scan_installed)
        card_layout.addLayout(row1)

        # 开关项 2: 全盘驱动器深度挖掘扫描 (C-Z盘)
        row2 = QHBoxLayout()
        vbox2 = QVBoxLayout()
        r2_title = BodyLabel("跨驱动器全盘深度搜索（遍历所有磁盘驱动器寻找其他 Steam 目录）", card)
        r2_sub = CaptionLabel("默认关闭。一般游戏都在 Steam 文件夹下，无需全盘大搜查。仅当其他磁盘有未关联到 Steam 的独立旧库时才建议开启，防止误导导入。", card)
        r2_sub.setStyleSheet("color: #888888;")
        vbox2.addWidget(r2_title)
        vbox2.addWidget(r2_sub)
        self.switch_scan_all_drives = SwitchButton(card)
        self.switch_scan_all_drives.setChecked(self._config.get("scan_all_drives", False))
        self.switch_scan_all_drives.checkedChanged.connect(lambda val: self._config.set("scan_all_drives", val))
        row2.addLayout(vbox2, 1)
        row2.addWidget(self.switch_scan_all_drives)
        card_layout.addLayout(row2)

        # 开关项 3: 第三方工具兼容
        row3 = QHBoxLayout()
        vbox3 = QVBoxLayout()
        r3_title = BodyLabel("兼容扫描第三方工具入库配置（Steam/config/st.json 等）", card)
        r3_sub = CaptionLabel("自动检测并收录 SteamTools 等第三方入库工具生成的配置记录，支持一键接管与彻底出库。", card)
        r3_sub.setStyleSheet("color: #888888;")
        vbox3.addWidget(r3_title)
        vbox3.addWidget(r3_sub)
        self.switch_scan_thirdparty = SwitchButton(card)
        self.switch_scan_thirdparty.setChecked(self._config.get("scan_third_party_tools", True))
        self.switch_scan_thirdparty.checkedChanged.connect(lambda val: self._config.set("scan_third_party_tools", val))
        row3.addLayout(vbox3, 1)
        row3.addWidget(self.switch_scan_thirdparty)
        card_layout.addLayout(row3)

        # 操作按钮
        action_layout = QHBoxLayout()
        self.btn_scan_current = PrimaryPushButton(FluentIcon.SEARCH, "扫描当前 Steam 库", card)
        self.btn_scan_current.clicked.connect(lambda: self._on_execute_scan(scan_all_drives=False))
        action_layout.addWidget(self.btn_scan_current)

        self.btn_deep_scan = PushButton(FluentIcon.FOLDER, "执行全盘跨驱动器深度扫描", card)
        self.btn_deep_scan.clicked.connect(lambda: self._on_execute_scan(scan_all_drives=True))
        action_layout.addWidget(self.btn_deep_scan)

        self.btn_manage_hidden = PushButton(FluentIcon.HIDE, "管理已隐藏/已出库黑名单", card)
        self.btn_manage_hidden.clicked.connect(self._on_manage_hidden_blacklist)
        action_layout.addWidget(self.btn_manage_hidden)

        action_layout.addStretch(1)
        card_layout.addLayout(action_layout)

        self._main_layout.addWidget(card)

    def _on_execute_scan(self, scan_all_drives: bool = False):
        if not self._game_manager:
            return
        btn = self.btn_deep_scan if scan_all_drives else self.btn_scan_current
        btn.setEnabled(False)
        scan_title = "跨驱动器全盘深度扫描" if scan_all_drives else "当前 Steam 库扫描"
        InfoBar.info("扫描中", f"正在执行{scan_title}...", parent=self, position=InfoBarPosition.TOP, duration=2500)

        def _scan_worker():
            steam_path = self._bridge.get_steam_path() if self._bridge else ""
            if steam_path:
                self._game_manager.set_steam_path(steam_path)
            games = self._game_manager.refresh(scan_local=True, scan_all_drives=scan_all_drives)
            return len(games)

        def _on_done(count: int):
            btn.setEnabled(True)
            InfoBar.success(
                "扫描完成",
                f"{scan_title}完成！当前共收录 {count} 款游戏。可前往「游戏库」查看并统一管理。",
                parent=self,
                position=InfoBarPosition.TOP,
                duration=5000,
            )
            self.scan_requested.emit()

        worker = AsyncWorker(_scan_worker)
        worker.finished_with_result.connect(_on_done, Qt.ConnectionType.QueuedConnection)
        worker.start()
        self._workers.append(worker)

    def _on_manage_hidden_blacklist(self):
        """打开隐藏/已出库黑名单管理窗口"""
        if not self._game_manager:
            return
        hidden_ids = sorted(list(self._game_manager.get_hidden_app_ids()))
        if not hidden_ids:
            InfoBar.info("提示", "当前没有被隐藏或被忽略的游戏", parent=self, position=InfoBarPosition.TOP, duration=3000)
            return

        dialog = QDialog(self)
        dialog.setWindowTitle("已隐藏/已出库游戏管理")
        dialog.setMinimumSize(460, 400)
        vlayout = QVBoxLayout(dialog)
        vlayout.setContentsMargins(20, 20, 20, 20)
        vlayout.setSpacing(12)

        vlayout.addWidget(StrongBodyLabel(f"已隐藏/忽略游戏列表 (共 {len(hidden_ids)} 款)", dialog))
        vlayout.addWidget(CaptionLabel("这些游戏在出库或手动隐藏后被加入忽略黑名单，不会在刷新时重新显示。点击『恢复显示』可重新恢复纳入管理：", dialog))

        table = QTableWidget(len(hidden_ids), 2, dialog)
        table.setHorizontalHeaderLabels(["AppID", "操作"])
        table.horizontalHeader().setStretchLastSection(True)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)

        for row, aid in enumerate(hidden_ids):
            table.setItem(row, 0, QTableWidgetItem(aid))
            btn_restore = PushButton(FluentIcon.SYNC, "恢复显示", dialog)
            def _restore_action(app_id=aid):
                self._game_manager.unhide_game(app_id)
                dialog.accept()
                InfoBar.success("已恢复", f"游戏 {app_id} 已移出隐藏黑名单，请刷新游戏库", parent=self, position=InfoBarPosition.TOP)
                self.scan_requested.emit()
            btn_restore.clicked.connect(_restore_action)
            table.setCellWidget(row, 1, btn_restore)

        vlayout.addWidget(table, 1)

        btn_row = QHBoxLayout()
        btn_unhide_all = PushButton(FluentIcon.ACCEPT, "全部恢复", dialog)
        def _unhide_all():
            for aid in hidden_ids:
                self._game_manager.unhide_game(aid)
            dialog.accept()
            InfoBar.success("已全部恢复", "所有隐藏游戏已恢复，请刷新游戏库", parent=self, position=InfoBarPosition.TOP)
            self.scan_requested.emit()
        btn_unhide_all.clicked.connect(_unhide_all)
        btn_row.addWidget(btn_unhide_all)

        btn_close = PrimaryPushButton("关闭", dialog)
        btn_close.clicked.connect(dialog.reject)
        btn_row.addStretch(1)
        btn_row.addWidget(btn_close)
        vlayout.addLayout(btn_row)

        dialog.exec()

    def _load_settings(self):
        saved_key = self._config.get("manifesthub_api_key", MANIFESTHUB_API_KEY)
        if saved_key:
            self.api_key_input.setText(saved_key)
        self._load_kernel_settings()

    def showEvent(self, event):  # noqa: N802 - Qt 接口
        super().showEvent(event)
        self._load_kernel_settings()

    def _load_kernel_settings(self):
        """从 opensteamtool.toml 载入内核开关（保留用户手工配置）。"""
        steam = self._steam_root()
        available = bool(steam)
        for widget in (
            self.switch_onlinefix,
            self.switch_cloud,
            self.cloud_library_input,
            self.manifest_combo,
            self.remote_templates_edit,
            self.switch_force_refresh,
        ):
            widget.setEnabled(available)

        if not available:
            self.cloud_status_lbl.setText("⚠️ 未检测到 Steam 安装目录，无法读取内核配置")
            self.cloud_status_lbl.setStyleSheet("color: #fa8c16;")
            self.remote_status_lbl.setText("未检测到 Steam")
            return

        try:
            onlinefix = bool(self._bridge.get_kernel_config("onlinefix", "env_session", True))
            cloud_enabled = bool(self._bridge.get_kernel_config("cloud", "enabled", False))
            library = str(self._bridge.get_kernel_config("cloud", "library", "cloud_redirect.dll") or "cloud_redirect.dll")
            provider = str(self._bridge.get_kernel_config("manifest", "url", "wudrm") or "wudrm")
            templates = self._bridge.get_kernel_config("remote", "url_template", list(STEAM_PATTERN_URL_TEMPLATES))
            force_refresh = bool(self._bridge.get_kernel_config("remote", "force_refresh", False))
        except Exception as exc:  # noqa: BLE001
            logger.warning("读取 opensteamtool.toml 失败: %s", exc)
            self.remote_status_lbl.setText(f"读取内核配置失败：{exc}")
            return

        if isinstance(templates, str):
            templates = [templates]
        if not isinstance(templates, list) or not templates:
            templates = list(STEAM_PATTERN_URL_TEMPLATES)

        for switch, value in (
            (self.switch_onlinefix, onlinefix),
            (self.switch_cloud, cloud_enabled),
            (self.switch_force_refresh, force_refresh),
        ):
            switch.blockSignals(True)
            switch.setChecked(value)
            switch.blockSignals(False)

        self.cloud_library_input.setText(library)
        self.remote_templates_edit.setPlainText("\n".join(str(item) for item in templates))

        self.manifest_combo.blockSignals(True)
        self.manifest_combo.setCurrentText(provider)
        if self.manifest_combo.currentText() != provider:
            self.manifest_combo.setCurrentIndex(0)
        self.manifest_combo.blockSignals(False)

        self._refresh_cloud_status()
        self.remote_status_lbl.setText(
            "缓存策略：" + ("强制刷新（每次启动重新解析）" if force_refresh else "按需刷新（命中缓存即复用）")
        )
        self.remote_status_lbl.setStyleSheet("color: #888888;")

