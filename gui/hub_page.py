from __future__ import annotations

from typing import Optional

from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from qfluentwidgets import (
    Action,
    BodyLabel,
    DropDownPushButton,
    FluentIcon,
    RoundMenu,
    SegmentedWidget,
    TransparentToolButton,
)


class HubPage(QWidget):
    """把一组同域页面收进一个主导航入口，内部用分段控件与高级菜单切换。

    默认展示日常使用的页面；维护与审计页面收纳在「高级与维护」
    下拉菜单里，避免侧边栏主菜单堆叠。
    """

    def __init__(
        self,
        object_name: str,
        advanced_title: str = "高级与维护",
        parent: Optional[QWidget] = None,
    ):
        super().__init__(parent)
        self.setObjectName(object_name)

        self._advanced_title = advanced_title
        self._pages: dict[str, QWidget] = {}
        self._labels: dict[str, str] = {}
        self._actions: dict[str, Action] = {}
        self._advanced: set[str] = set()
        self._default_key = ""
        self._last_normal_key = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = QWidget(self)
        self._header.setObjectName(f"{object_name}Header")
        header_layout = QHBoxLayout(self._header)
        header_layout.setContentsMargins(28, 14, 28, 4)
        header_layout.setSpacing(10)

        self.segmented = SegmentedWidget(self._header)
        self.segmented.setObjectName(f"{object_name}Segmented")
        self.segmented.setItemFontSize(14)
        self.segmented.setIndicatorLength(20)
        header_layout.addWidget(self.segmented)

        self._advanced_hint = BodyLabel("", self._header)
        self._advanced_hint.setStyleSheet("color: #888; font-size: 13px; font-weight: bold;")
        self._advanced_hint.hide()
        header_layout.addWidget(self._advanced_hint)

        header_layout.addStretch(1)

        self._back_btn = TransparentToolButton(FluentIcon.LEFT_ARROW, self._header)
        self._back_btn.setFixedSize(32, 32)
        self._back_btn.setToolTip("返回常规页面")
        self._back_btn.clicked.connect(self._back_to_default)
        self._back_btn.hide()
        header_layout.addWidget(self._back_btn)

        self._advanced_btn = DropDownPushButton(
            FluentIcon.DEVELOPER_TOOLS, self._advanced_title, self._header
        )
        self._advanced_btn.hide()
        self._advanced_menu = RoundMenu(parent=self._advanced_btn)
        self._advanced_btn.setMenu(self._advanced_menu)
        header_layout.addWidget(self._advanced_btn)

        layout.addWidget(self._header)

        self.stack = QStackedWidget(self)
        self.stack.setObjectName(f"{object_name}Stack")
        layout.addWidget(self.stack, 1)

        self.segmented.currentItemChanged.connect(self._on_segment_changed)

    # ---- 构建 ----

    def add_page(
        self,
        page: QWidget,
        label: str,
        key: str,
        icon: FluentIcon | None = None,
    ) -> Action:
        self._register(page, label, key)
        self.stack.addWidget(page)
        self.segmented.addItem(key, label, icon=icon)
        if not self._default_key:
            self._default_key = key
            self._last_normal_key = key
        if self.stack.currentWidget() is None:
            self.stack.setCurrentWidget(page)

        action = Action(icon or FluentIcon.DOCUMENT, label, self)
        action.triggered.connect(lambda _checked=False, k=key: self.switch_to(k))
        self._actions[key] = action
        return action

    def add_advanced_page(
        self,
        page: QWidget,
        label: str,
        key: str,
        icon: FluentIcon | None = None,
    ) -> Action:
        self._register(page, label, key)
        self._advanced.add(key)
        self.stack.addWidget(page)

        action = Action(icon or FluentIcon.DEVELOPER_TOOLS, label, self._advanced_btn)
        action.triggered.connect(lambda _checked=False, k=key: self.switch_to(k))
        self._advanced_menu.addAction(action)
        self._actions[key] = action
        self._advanced_btn.show()
        return action

    def _register(self, page: QWidget, label: str, key: str) -> None:
        if key in self._pages:
            raise ValueError(f"duplicate hub page key: {key}")
        self._pages[key] = page
        self._labels[key] = label

    def set_default_page(self, key: str) -> None:
        if key not in self._pages:
            raise KeyError(key)
        self._default_key = key
        self._last_normal_key = key
        self.switch_to(key)

    # ---- 切换 ----

    def switch_to(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        if key in self._advanced:
            self._enter_advanced(key, page)
        else:
            self._leave_advanced()
            self._last_normal_key = key
            if self.segmented.currentRouteKey() != key:
                self.segmented.setCurrentItem(key)
            self.stack.setCurrentWidget(page)

    def current_key(self) -> str:
        current = self.stack.currentWidget()
        for key, page in self._pages.items():
            if page is current:
                return key
        return ""

    def has_sub_page(self, page: QWidget) -> bool:
        return page in self._pages.values()

    def show_sub_page(self, page: QWidget) -> None:
        for k, p in self._pages.items():
            if p is page:
                self.switch_to(k)
                return

    def active_sub_page(self) -> QWidget | None:
        return self.stack.currentWidget()

    def get_nav_item(self, key: str):
        if key in self._actions:
            return self._actions[key]
        if hasattr(self.segmented, "items") and key in self.segmented.items:
            return self.segmented.items[key]
        return None

    def _on_segment_changed(self, key: str) -> None:
        page = self._pages.get(key)
        if page is not None:
            self._last_normal_key = key
            self.stack.setCurrentWidget(page)

    def _enter_advanced(self, key: str, page: QWidget) -> None:
        self.segmented.hide()
        self._advanced_hint.setText(f"{self._advanced_title} · {self._labels[key]}")
        self._advanced_hint.show()
        self._back_btn.show()
        self.stack.setCurrentWidget(page)

    def _leave_advanced(self) -> None:
        if self.segmented.isHidden():
            self.segmented.show()
        self._advanced_hint.hide()
        self._back_btn.hide()

    def _back_to_default(self) -> None:
        target = self._last_normal_key or self._default_key
        if target:
            self.switch_to(target)

    # ---- 主题 ----

    def notify_theme_changed(self) -> None:
        self._header.update()
        self._header.repaint()
        self.segmented.update()
        self.segmented.repaint()
        for page in self._pages.values():
            if hasattr(page, "notify_theme_changed"):
                try:
                    page.notify_theme_changed()
                except Exception:
                    pass
