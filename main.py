"""OpenSteamToolDesktop 应用程序入口。

启动流程：
    1. 崩溃诊断初始化（faulthandler + Windows Dump）
    2. Qt 消息处理器安装
    3. 主题/配置加载
    4. 版本更新检查
    5. Steam Bridge + GameManager 初始化
    6. 主窗口创建与显示
"""
from __future__ import annotations

import io
import logging
import os
import sys
import traceback
from pathlib import Path

import faulthandler

# ── 崩溃诊断全开 ──────────────────────────────────────────────

from utils.path_manager import PathManager

# PyInstaller windowed 模式下 sys.stderr 为 None，faulthandler 需要先重定向
_logs_dir = PathManager.logs_dir()

if sys.stderr is None:
    _stderr_path = _logs_dir / "stderr.log"
    sys.stderr = open(str(_stderr_path), "a", encoding="utf-8")

faulthandler.enable(all_threads=True, file=sys.stderr)

# 崩溃日志处理器
_crash_handler: logging.FileHandler | None = None
from config import CRASH_LOG_ENABLED
if CRASH_LOG_ENABLED:
    _crash_handler = logging.FileHandler(
        str(PathManager.crash_log_path()), mode="a", encoding="utf-8"
    )
    _crash_handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] %(message)s"
    ))

# ── Qt 初始化 ─────────────────────────────────────────────────

from PyQt6.QtCore import QEvent, QObject, Qt, QtMsgType, qInstallMessageHandler
from PyQt6.QtWidgets import QApplication

# 屏蔽 QFluentWidgets Pro 推广提示
_old_stdout = sys.stdout
sys.stdout = io.StringIO()
from qfluentwidgets import setTheme, setThemeColor, Theme
sys.stdout = _old_stdout

from config import (
    APP_NAME, APP_VERSION, DEFAULT_THEME_COLOR, DEFAULT_THEME_MODE, LUA_DIR_RELATIVE,
)
from core.config_manager import ConfigManager
from core.game_manager import LuaGameManager
from core.steam_bridge import SteamBridge
from gui.main_window import MainWindow
from utils.logger import setup_logger


def _qt_message_handler(
    msg_type: QtMsgType,
    context: QtMsgType,  # type: ignore[override]
    msg: str,
) -> None:
    """捕获所有 Qt 内部日志 → Python logger + crash.log 文件"""
    qt_logger = logging.getLogger("Qt")
    type_map = {
        QtMsgType.QtDebugMsg: qt_logger.debug,
        QtMsgType.QtInfoMsg: qt_logger.info,
        QtMsgType.QtWarningMsg: qt_logger.warning,
        QtMsgType.QtCriticalMsg: qt_logger.critical,
        QtMsgType.QtFatalMsg: qt_logger.critical,
    }
    handler_fn = type_map.get(msg_type, qt_logger.warning)
    context_info = getattr(context, 'file', '') or ''
    line_info = getattr(context, 'line', 0) or 0
    func_info = getattr(context, 'function', '') or ''
    handler_fn("[Qt] %s (file=%s, line=%s, func=%s)", msg, context_info, line_info, func_info)

    if _crash_handler is not None:
        try:
            _crash_handler.emit(logging.LogRecord(
                name="Qt", level=logging.WARNING,
                pathname=context_info, lineno=line_info,
                msg=f"[Qt {msg_type.name}] {msg}", args=(),
                exc_info=None,
            ))
        except Exception:
            pass


class _SafeApplication(QApplication):
    """全局异常拦截：Qt 事件回调异常不传播到 Qt 层导致闪退"""

    def notify(self, receiver: QObject, event: QEvent) -> bool:
        try:
            return super().notify(receiver, event)
        except Exception as e:
            tb = traceback.format_exc()
            crash_logger = logging.getLogger("App.CrashGuard")
            crash_logger.critical(
                "Unhandled exception in Qt event loop: %s\n  receiver=%s\n  event=%s",
                e, receiver, event, exc_info=True,
            )
            if _crash_handler is not None:
                try:
                    _crash_handler.emit(logging.LogRecord(
                        name="App.CrashGuard", level=logging.CRITICAL,
                        pathname="Qt-EventLoop", lineno=0,
                        msg=f"{e}\n{tb}", args=(),
                        exc_info=None,
                    ))
                except Exception:
                    pass
            return False


def main() -> None:
    """应用程序主入口"""
    # Windows 任务栏图标：设置 AppUserModelID，否则任务栏显示 Python 默认图标
    try:
        import ctypes
        app_user_model_id = f"OpenSteamTool.{APP_NAME}.{APP_VERSION}".replace(" ", "")
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_user_model_id)
    except Exception:
        pass

    qInstallMessageHandler(_qt_message_handler)
    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = _SafeApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # 设置应用程序图标（影响任务栏与主窗口）
    from pathlib import Path as _Path
    _icon_candidates = []
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        _base = _Path(sys._MEIPASS)
        _icon_candidates.extend([_base / "assets" / "icon.ico", _base / "gui" / "icon.ico"])
    _root = _Path(__file__).resolve().parent
    _icon_candidates.extend([_root / "assets" / "icon.ico", _root / "gui" / "icon.ico"])
    for _cand in _icon_candidates:
        if _cand.exists():
            from PyQt6.QtGui import QIcon
            app.setWindowIcon(QIcon(str(_cand)))
            break

    logger = setup_logger(__name__)
    logger.info("=" * 60)
    logger.info("Application starting... %s v%s", APP_NAME, APP_VERSION)
    logger.info("=" * 60)

    # 全局异常钩子
    def _global_excepthook(exc_type, exc_value, exc_tb):
        logger.critical("Unhandled exception:", exc_info=(exc_type, exc_value, exc_tb))
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _global_excepthook

    # ── 配置 ──
    logger.debug("Initializing ConfigManager...")
    config_manager = ConfigManager()
    logger.debug("Config loaded from: %s", config_manager.config_file)

    # ── 主题（固定暗色） ──
    logger.info("Setting theme mode: dark (forced)")
    setTheme(Theme.DARK)

    theme_color = config_manager.get("theme_color", DEFAULT_THEME_COLOR)
    logger.info("Setting theme color: %s", theme_color)
    setThemeColor(theme_color)

    # ── 核心模块 ──
    logger.debug("Initializing SteamBridge...")
    bridge = SteamBridge()
    logger.debug("Steam path detected: %s", bridge.get_steam_path() or 'Not found')
    logger.info("Using built-in DLL directory: %s", bridge.get_dll_path())

    lua_dir = ""
    steam_path = bridge.get_steam_path()
    if steam_path:
        lua_dir = os.path.join(steam_path, LUA_DIR_RELATIVE)
        logger.debug("Lua directory set to: %s", lua_dir)
    else:
        logger.warning("Steam path not found, Lua directory not set")
    game_manager = LuaGameManager(lua_dir)

    # ── 主窗口 ──
    logger.debug("Creating MainWindow...")
    window = MainWindow(bridge, game_manager, config_manager)

    window_effect = config_manager.get("window_effect", "none")
    logger.debug("Window effect: %s", window_effect)
    window.apply_window_effect(window_effect)

    screen = QApplication.primaryScreen()
    if screen:
        sg = screen.availableGeometry()
        window.move(
            (sg.width() - window.width()) // 2,
            (sg.height() - window.height()) // 2,
        )
        logger.debug("Window positioned at: (%d, %d)",
                     (sg.width() - window.width()) // 2,
                     (sg.height() - window.height()) // 2)

    window.show()
    logger.info("Application started successfully")

    # ── 版本检查（异步、静默，不打扰用户）────────
    # 原则：正常使用绝不弹窗。检查失败静默记日志（离线启动不应被干扰）；
    # 有新版本时用自动消失的非阻塞 InfoBar 轻提示，可在设置页手动检查更新。
    from utils.async_worker import AsyncWorker
    from core.version_checker import check_for_updates

    def _on_version_check_result(result):
        """版本检查完成（主线程回调，静默）"""
        release, error_msg = result
        if error_msg:
            logger.info("启动版本检查失败（静默跳过）: %s", error_msg)
            return
        if release is not None:
            try:
                from qfluentwidgets import InfoBar, InfoBarPosition

                InfoBar.info(
                    "发现新版本",
                    f"v{release.version} 已发布（当前 v{APP_VERSION}），可在「设置」中查看更新。",
                    parent=window,
                    position=InfoBarPosition.TOP,
                    duration=6000,
                )
            except Exception:  # noqa: BLE001 - 提示失败不应影响启动
                logger.info("发现新版本 v%s", release.version)

    def _on_version_check_error(e):
        logger.info("启动版本检查异常（静默跳过）: %s", e)

    # 持有运行中 worker 的强引用：QThread 对象一旦在函数返回后被垃圾回收，
    # Qt 会以 "QThread: Destroyed while thread is still running" 致命错误中止进程
    # （表现为启动数秒后闪退）。
    _vc_workers: list[AsyncWorker] = []

    def _run_version_check():
        """（重新）执行启动版本检查；静默策略下仅供启动调用。"""
        worker = AsyncWorker(check_for_updates)

        def _release(*_args) -> None:
            try:
                _vc_workers.remove(worker)
            except ValueError:
                pass

        worker.finished_with_result.connect(_on_version_check_result)
        worker.finished_with_result.connect(_release)
        worker.finished_with_error.connect(_on_version_check_error)
        worker.finished_with_error.connect(_release)
        _vc_workers.append(worker)
        worker.start()

    _run_version_check()

    exit_code = app.exec()

    # ── 清理 ──
    logger.info("Application shutting down...")
    try:
        window.shutdown()
    except Exception as e:
        logger.warning("Final shutdown error: %s", e)

    try:
        from gui.home_page import _executor
        _executor.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    # 刷新 404 缓存到磁盘
    try:
        from utils.http_client import save_404_cache_now
        save_404_cache_now()
    except Exception:
        pass

    logger.info("Application exited")
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
