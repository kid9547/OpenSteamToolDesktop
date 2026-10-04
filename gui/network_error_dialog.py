"""
网络错误弹窗 — 使用 qfluentwidgets 风格

只提供与故障本身相关的操作（打开项目主页、重试、退出），
不包含任何第三方推广内容。
"""
from __future__ import annotations

import webbrowser

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import QVBoxLayout, QHBoxLayout, QDialog, QLabel, QWidget

from qfluentwidgets import (
    PrimaryPushButton, PushButton, TitleLabel, BodyLabel,
    FluentIcon, isDarkTheme,
)

from config import GITHUB_REPO_URL


class NetworkErrorDialog(QDialog):
    """网络连接失败弹窗"""

    # 信号：用户选择退出应用
    exit_requested = pyqtSignal()
    # 信号：用户选择重试（由主窗口决定重试什么）
    retry_requested = pyqtSignal()

    def __init__(self, error_msg: str, parent=None):
        super().__init__(parent)
        self._error_msg = error_msg
        self.setWindowTitle("连接失败")
        self.setFixedSize(480, 280)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowTitleHint | Qt.WindowType.CustomizeWindowHint)
        self.setModal(True)

        self._init_ui()
        self._apply_theme()

    def _init_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(32, 24, 32, 24)
        layout.setSpacing(16)

        # 图标 + 标题行
        header = QHBoxLayout()
        header.setSpacing(12)

        icon_bg = QWidget()
        icon_bg.setFixedSize(48, 48)
        icon_bg.setStyleSheet("background-color: #ff9800; border-radius: 24px;")
        icon_inner = QVBoxLayout(icon_bg)
        icon_inner.setContentsMargins(0, 0, 0, 0)
        icon_label = QLabel()
        icon_label.setPixmap(FluentIcon.WIFI.icon().pixmap(28, 28))
        icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_inner.addWidget(icon_label)
        header.addWidget(icon_bg)

        title = TitleLabel("无法连接到服务器", self)
        header.addWidget(title)
        header.addStretch()
        layout.addLayout(header)

        # 错误信息
        msg = BodyLabel(self._error_msg, self)
        msg.setWordWrap(True)
        layout.addWidget(msg)

        # 建议
        hint = BodyLabel(
            "请检查本机网络连接；若网络需要代理，请先开启系统代理后重试。\n"
            "也可以到项目主页查看常见问题与最新版本。",
            self,
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #888888; font-size: 13px;")
        layout.addWidget(hint)

        layout.addStretch()

        # 按钮区：重试（主操作）+ 项目主页 + 退出
        button_row = QHBoxLayout()
        button_row.setSpacing(12)

        self.retry_btn = PrimaryPushButton(FluentIcon.SYNC, "重试", self)
        self.retry_btn.setMinimumWidth(110)
        self.retry_btn.clicked.connect(self._on_retry)

        self.github_btn = PushButton(FluentIcon.GITHUB, "前往项目主页", self)
        self.github_btn.setMinimumWidth(150)
        self.github_btn.clicked.connect(self._on_github)

        self.exit_btn = PushButton("退出", self)
        self.exit_btn.setMinimumWidth(80)
        self.exit_btn.clicked.connect(self._on_exit)

        button_row.addStretch()
        button_row.addWidget(self.retry_btn)
        button_row.addWidget(self.github_btn)
        button_row.addWidget(self.exit_btn)
        button_row.addStretch()

        layout.addLayout(button_row)

    def _apply_theme(self):
        dark = isDarkTheme()
        bg = "#2b2b2b" if dark else "#ffffff"
        fg = "#ffffff" if dark else "#000000"
        self.setStyleSheet(f"""
            QDialog {{
                background-color: {bg};
                color: {fg};
            }}
        """)

    def _on_github(self):
        webbrowser.open(GITHUB_REPO_URL)
        self.reject()

    def _on_retry(self):
        self.retry_requested.emit()
        self.reject()

    def _on_exit(self):
        """用户点击退出，通知主窗口退出应用"""
        self.exit_requested.emit()
        self.reject()
