"""
科学加速页面 — 提供网络加速方案入口

只推荐免费、可公开获取的工具（Watt Toolkit），
不包含任何付费推广或返利链接。
"""
from __future__ import annotations

import webbrowser
from typing import Optional

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QVBoxLayout, QHBoxLayout, QLabel, QWidget,
)

from qfluentwidgets import (
    PushButton, TitleLabel, BodyLabel,
    FluentIcon,
    CardWidget, StrongBodyLabel,
)
from utils.logger import setup_logger


logger = setup_logger(__name__)

# Watt Toolkit 商店链接
WATT_TOOLKIT_STORE_URL: str = "ms-windows-store://pdp/?productid=9MTCFHS560NG"


class AcceleratePage(QWidget):
    """科学加速页面

    提供免费的网络加速方案，解决 Steam 社区、GitHub 等网站访问问题。
    """

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._init_ui()
        self._apply_theme()

    def _init_ui(self):
        """初始化界面"""
        # 主布局
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(40, 32, 40, 32)
        main_layout.setSpacing(24)

        # ── 页面标题区 ──────────────────────────────────
        header = self._create_header()
        main_layout.addWidget(header)

        # ── 方案卡片区 ──────────────────────────────────
        cards_layout = QHBoxLayout()
        cards_layout.setSpacing(20)

        # Watt Toolkit（免费开源社区加速工具）
        watt_card = self._create_watt_card()
        cards_layout.addWidget(watt_card)

        main_layout.addLayout(cards_layout)

        # ── 底部说明 ──────────────────────────────────
        main_layout.addStretch()

        footer = self._create_footer()
        main_layout.addWidget(footer)

    def _create_header(self) -> QWidget:
        """创建页面标题区"""
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # 标题
        title = TitleLabel("科学加速", self)
        layout.addWidget(title)

        # 副标题
        subtitle = BodyLabel("选择合适的网络加速方案，解决 Steam 社区、GitHub 等网站访问问题", self)
        subtitle.setStyleSheet("color: #888888; font-size: 14px;")
        layout.addWidget(subtitle)

        return widget

    def _create_watt_card(self) -> CardWidget:
        """创建 Watt Toolkit 方案卡片"""
        card = CardWidget(self)
        card.setFixedSize(320, 310)

        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(10)

        # 图标
        icon_label = QLabel(card)
        icon_label.setPixmap(FluentIcon.DOWNLOAD.icon().pixmap(40, 40))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(icon_label)

        # 标题
        title = StrongBodyLabel("Watt Toolkit", card)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        # 免费标签
        price = QLabel("免费", card)
        price.setAlignment(Qt.AlignmentFlag.AlignCenter)
        price.setStyleSheet(
            "font-size: 22px; font-weight: bold; color: #1890ff;"
        )
        layout.addWidget(price)

        # 描述
        desc = BodyLabel(
            "Steam 社区加速工具\n"
            "支持商店、社区、创意工坊\n"
            "微软商店免费下载",
            card,
        )
        desc.setWordWrap(True)
        desc.setAlignment(Qt.AlignmentFlag.AlignCenter)
        desc.setStyleSheet("color: #999999; font-size: 12px; line-height: 1.4;")
        layout.addWidget(desc)

        layout.addStretch()

        # 按钮
        btn = PushButton("安装 Watt Toolkit", card)
        btn.setIcon(FluentIcon.DOWNLOAD)
        btn.setFixedHeight(36)
        btn.clicked.connect(self._on_watt_install)
        layout.addWidget(btn)

        return card

    def _create_footer(self) -> QWidget:
        """创建底部说明区"""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)

        # 说明
        hint = BodyLabel("以上方案仅供参考，请根据实际需求选择", self)
        hint.setStyleSheet("color: #888888; font-size: 12px;")
        layout.addWidget(hint)

        layout.addStretch()

        return widget

    def _apply_theme(self):
        """应用主题样式（仅影响本页面）"""
        # 不设置任何样式表，让 MSFluentWindow 统一管理主题
        # 避免全局 QWidget 样式污染其他页面
        pass

    def _on_watt_install(self):
        """打开 Watt Toolkit 安装页面"""
        logger.info(f"Opening Watt Toolkit store page: {WATT_TOOLKIT_STORE_URL}")
        webbrowser.open(WATT_TOOLKIT_STORE_URL)
