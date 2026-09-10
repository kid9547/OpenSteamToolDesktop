"""
steam_bridge — Steam 与 OpenSteamTool 的桥梁类

正确注入机制（基于 OpenSteamTool README）：
1. 将 3 个 DLL（OpenSteamTool.dll, dwmapi.dll, xinput1_4.dll）
   复制到 Steam 根目录
2. 创建 Lua 目录（<steam>/config/lua）并放入 Lua 配置
3. 正常启动 Steam，DLL 通过搜索顺序劫持自动加载

本模块负责：DLL 部署、注入状态验证、Lua 目录管理。
"""
import os
import sys
import subprocess
from pathlib import Path

from core.steam_detector import SteamDetector, SteamStatus
from core.dll_injector import DLLInjector, InjectStatus, InjectResult
from core.dll_manager import DLLManager
from utils.logger import setup_logger
from core.steam_pattern_sync import SteamPatternSync

logger = setup_logger(__name__)


class SteamBridge:
    """Steam 与 OpenSteamTool 的桥梁类

    负责：
    1. 检测 Steam 安装路径
    2. 管理 DLL 部署状态
    3. 验证注入是否成功（通过日志文件）
    """

    def __init__(self):
        self._steam_path = ""
        self._lua_dir = ""
        # 使用 DLLManager 管理 DLL 路径
        self._dll_manager = DLLManager()
        self._dll_source_dir = ""
        self._detector = SteamDetector()
        self._injector = DLLInjector()
        logger.debug("SteamBridge initializing...")
        self._detect_steam()
        # 自动设置 DLL 源目录（优先使用 DLLManager 管理的路径）
        self._update_dll_source_from_manager()

    def _update_dll_source_from_manager(self):
        """从 DLLManager 获取 DLL 路径并更新注入器

        优先级：
        1. DLLManager 管理的路径（用户下载的最新版本）
        2. 内置的 resources/fallback_dlls 目录
        """
        dll_path = self._dll_manager.get_dll_path()
        if dll_path is not None and dll_path.exists() and dll_path != Path():
            self._dll_source_dir = str(dll_path)
            self._injector.set_dll_source_dir(self._dll_source_dir)
            logger.info(f"Using DLLManager path: {self._dll_source_dir}")
        else:
            # 回退：使用内置的 resources/fallback_dlls 目录
            fallback_path = self._get_default_dll_source_dir()
            if fallback_path and os.path.isdir(fallback_path):
                # 验证 fallback 目录是否包含所需的 DLL
                if self._check_fallback_dlls(fallback_path):
                    self._dll_source_dir = fallback_path
                    self._injector.set_dll_source_dir(self._dll_source_dir)
                    logger.info(f"Using fallback DLL path: {self._dll_source_dir}")
                else:
                    logger.error(f"Fallback DLL path missing required DLLs: {fallback_path}")
                    self._dll_source_dir = ""
            else:
                logger.warning(f"No valid DLL path found, fallback path not found: {fallback_path}")
                self._dll_source_dir = ""

    def _check_fallback_dlls(self, fallback_path: str) -> bool:
        """检查 fallback 目录是否包含所需的 DLL 文件"""
        for dll_name in DLLInjector.ALL_DLLS:
            if not os.path.isfile(os.path.join(fallback_path, dll_name)):
                logger.warning(f"Missing DLL in fallback: {dll_name}")
                return False
        return True

    def _get_default_dll_source_dir(self) -> str:
        """获取默认的 DLL 源目录（内置的 resources/fallback_dlls 目录）

        兼容三种运行环境：
        - 开发模式：基于 __file__ 向上推导项目根目录
        - PyInstaller 打包模式：基于 sys._MEIPASS
        - Nuitka 打包模式：基于 __file__ 或 sys.executable
        """
        # PyInstaller 打包后，sys._MEIPASS 指向解压目录
        if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
            base_dir = sys._MEIPASS
            dll_dir = os.path.join(base_dir, "resources/fallback_dlls")
            if os.path.isdir(dll_dir):
                return dll_dir

        # Nuitka 打包后（standalone 或 onefile）
        if hasattr(sys, '__compiled__'):
            # onefile 模式：__file__ 指向临时解压目录中的 .pyd 文件
            base_dir = os.path.dirname(os.path.abspath(__file__))
            dll_dir = os.path.join(base_dir, "resources/fallback_dlls")
            if os.path.isdir(dll_dir):
                return dll_dir
            # standalone 模式：数据文件在 exe 同级目录
            exe_dir = os.path.dirname(sys.executable)
            dll_dir = os.path.join(exe_dir, "resources/fallback_dlls")
            if os.path.isdir(dll_dir):
                return dll_dir

        # 开发模式：基于当前文件位置推导
        current_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(current_dir)
        dll_dir = os.path.join(project_root, "resources/fallback_dlls")
        return dll_dir

    def _detect_steam(self):
        logger.debug("Detecting Steam installation...")
        # detector.detect() 内部已优先读取 app_state，回退注册表
        result = self._detector.detect()
        if result.status == SteamStatus.INSTALLED:
            self._steam_path = result.path
            self._lua_dir = os.path.join(result.path, "config", "lua")
            self._injector.set_steam_path(result.path)
            logger.info(f"Steam path: {result.path}")
            logger.debug(f"Lua directory: {self._lua_dir}")
            # 同步到全局状态
            from core.app_state import app_state, STEAM_PATH
            app_state.set(STEAM_PATH, self._steam_path)
        else:
            logger.warning(f"Steam not found: {result.message}")

    def set_dll_source_dir(self, path: str):
        """设置 OpenSteamTool DLL 源目录（包含 3 个 DLL 的目录）"""
        logger.info(f"Setting DLL source directory: {path}")
        if not os.path.isdir(path):
            logger.warning(f"DLL source directory not found: {path}")
        self._dll_source_dir = path
        self._injector.set_dll_source_dir(path)

    # 保留别名以兼容现有调用
    def set_dll_path(self, path: str):
        logger.debug(f"set_dll_path called (alias for set_dll_source_dir): {path}")
        self.set_dll_source_dir(path)

    def get_dll_path(self) -> str:
        """获取当前 DLL 源目录路径"""
        return self._dll_source_dir

    def get_dll_source_dir(self) -> str:
        """获取 DLL 源目录路径（get_dll_path 的别名）"""
        return self._dll_source_dir

    def inject(self) -> tuple[bool, str]:
        """部署 DLL 到 Steam 目录（正确的注入方式）

        步骤：
        1. 将 3 个 DLL 复制到 Steam 根目录
        2. 创建 <steam>/config/lua 目录
        3. 提示用户重启 Steam

        Returns:
            (成功状态, 消息)
        """
        if not self._dll_source_dir or not os.path.isdir(self._dll_source_dir):
            logger.error("Cannot inject: DLL source directory not set or not found")
            return False, (
                f"内置注入源目录未找到：{self._dll_source_dir}。"
                "请确保 resources/fallback_dlls 目录存在且包含所需的文件。"
            )

        logger.info(f"Starting DLL injection from: {self._dll_source_dir}")
        self._injector.set_dll_source_dir(self._dll_source_dir)
        result = self._injector.deploy_dlls()
        logger.debug(f"Deploy result: {result.status}, {result.message}")

        if result.status == InjectStatus.SUCCESS or result.status == InjectStatus.DLL_ALREADY_DEPLOYED:
            logger.debug("Creating Lua directory...")
            lua_result = self._injector.create_lua_dir()
            pattern_ok, pattern_message = SteamPatternSync(self._steam_path).sync()
            if pattern_ok:
                logger.info("Steam signatures ready: %s", pattern_message)
            else:
                logger.warning("Steam signatures are not ready: %s", pattern_message)
            self._injector.deploy_opensteamtool_config()
            self._injector.deploy_manifest_lua()
            message = result.message + "；" + lua_result.message + "；" + pattern_message + "；运行时配置(wudrm)已就绪"
            logger.info("Injection completed: %s", message)
            return True, message

        logger.error(f"Injection failed: {result.message}")
        return False, result.message

    def verify_injection(self) -> tuple[bool, str]:
        """验证注入状态（检查 <steam>/opensteamtool/ 日志文件）

        Returns:
            (已激活, 消息)
        """
        logger.debug("Verifying injection status...")
        result = self._injector.verify_injection()
        logger.debug(f"Verification result: {result.status}, {result.message}")

        if result.status == InjectStatus.SUCCESS:
            missing_patterns = SteamPatternSync(self._steam_path).missing_patterns()
            if missing_patterns:
                message = "DLL 已部署，但 Steam 签名文件缺失：" + "；".join(missing_patterns)
                logger.warning(message)
                return False, message
            # 自动补全运行时配置与 manifest 解析器
            self._injector.deploy_opensteamtool_config()
            self._injector.deploy_manifest_lua()
            return True, result.message
        else:
            return False, result.message

    def disconnect(self) -> tuple[bool, str]:
        """完全移除注入：DLL + 日志 + Lua 配置文件

        Returns:
            (成功状态, 消息)
        """
        logger.info("Starting full uninstallation...")
        msg_parts = []

        # 1. 卸载 DLL
        result = self._injector.uninstall_dlls()
        msg_parts.append(result.message)
        logger.debug(f"Uninstall DLLs: {result.status}")

        # 2. 清理日志
        log_result = self._injector.clean_logs()
        if log_result.status == InjectStatus.SUCCESS:
            msg_parts.append("日志已清理")
        else:
            msg_parts.append(f"日志清理: {log_result.message}")

        # 3. 清理 Lua 配置
        lua_result = self._injector.clean_lua_configs()
        if lua_result.status == InjectStatus.SUCCESS:
            msg_parts.append("游戏配置已清理")
        else:
            msg_parts.append(f"配置清理: {lua_result.message}")

        msg = "；".join(msg_parts)
        if result.status == InjectStatus.SUCCESS:
            logger.info(f"Disconnect successful: {msg}")
            return True, msg

        logger.error(f"Disconnect failed: {msg}")
        return False, msg

    def is_connected(self) -> bool:
        """检查是否已注入激活（通过日志验证）"""
        result = self._injector.verify_injection()
        return result.status == InjectStatus.SUCCESS

    def is_deployed(self) -> bool:
        """检查 DLL 是否已部署到 Steam 目录"""
        if not self._steam_path:
            return False
        all_deployed, _ = self._injector.check_dlls_deployed()
        return all_deployed

    def set_steam_path(self, path: str) -> None:
        """设置 Steam 安装路径并同步到 injector 与全局状态"""
        self._steam_path = path
        if path:
            self._lua_dir = os.path.join(path, "config", "lua")
            self._injector.set_steam_path(path)
            from core.app_state import app_state, STEAM_PATH
            app_state.set(STEAM_PATH, path)

    def get_steam_path(self) -> str:
        """获取 Steam 安装路径（优先本地缓存，回退全局状态）"""
        if self._steam_path:
            return self._steam_path
        from core.app_state import app_state, STEAM_PATH
        return str(app_state.get(STEAM_PATH, ""))

    def get_opensteamtool_log_dir(self) -> str:
        """获取 OpenSteamTool 日志目录"""
        if not self._steam_path:
            return ""
        return os.path.join(self._steam_path, "opensteamtool")

    def get_default_lua_dir(self) -> str:
        """获取默认 Lua 配置目录（README 指定：<steam>/config/lua）"""
        if not self._steam_path:
            return ""
        return os.path.join(self._steam_path, "config", "lua")

    def get_lua_dir(self) -> str:
        """获取当前使用的 Lua 配置目录（优先使用存在的 stplug-in，否则回退 config/lua）"""
        if self._steam_path:
            stplugin = os.path.join(self._steam_path, "config", "stplug-in")
            if os.path.isdir(stplugin):
                return stplugin
            return os.path.join(self._steam_path, "config", "lua")
        return self._lua_dir or ""

    def redetect_steam(self):
        """重新检测 Steam"""
        self._detect_steam()

    def check_dll_version_mismatch(self) -> tuple[bool, list[str]]:
        """检查 DLL 版本是否匹配（封装 DLLInjector 的方法）

        Returns:
            (是否存在版本不匹配, 不匹配的 DLL 列表)
        """
        if not self._injector:
            return False, ["DLL 注入器未初始化"]
        return self._injector.check_dll_version_mismatch()

    def check_for_dll_updates(self) -> tuple[bool, str, dict | None]:
        """检查是否有 DLL 更新可用

        Returns:
            (有更新, 消息, 版本信息)
        """
        if not self._dll_manager:
            return False, "DLL 管理器未初始化", None
        return self._dll_manager.check_for_updates()

    def download_and_install_latest_dll(self) -> tuple[bool, str]:
        """下载并安装最新版本的 DLL

        Returns:
            (是否成功, 消息)
        """
        if not self._dll_manager:
            return False, "DLL 管理器未初始化"

        # 检查更新
        update_available, msg, version_info = self._dll_manager.check_for_updates()
        if not update_available:
            return False, msg

        if not version_info:
            return False, "未获取到版本信息"

        # 下载并安装
        success, install_msg = self._dll_manager.download_and_install(version_info)
        if success:
            # 更新 DLL 源目录
            self._update_dll_source_from_manager()
            return True, install_msg
        else:
            return False, install_msg

    def start_steam(self) -> tuple[bool, str]:
        """启动 Steam 客户端

        Returns:
            (成功状态, 消息)
        """
        if not self._steam_path:
            logger.error("Cannot start Steam: path not set")
            return False, "Steam 路径未设置"

        steam_exe = os.path.join(self._steam_path, self._detector.STEAM_EXE)
        if not os.path.isfile(steam_exe):
            logger.error(f"Steam.exe not found: {steam_exe}")
            return False, f"Steam.exe 未找到：{steam_exe}"

        try:
            logger.info(f"Starting Steam: {steam_exe}")
            # 使用 subprocess.Popen 启动 Steam（不等待）
            subprocess.Popen([steam_exe], shell=False)
            return True, "Steam 正在启动..."

        except Exception as e:
            logger.error(f"Failed to start Steam: {e}")
            return False, f"启动 Steam 失败：{str(e)}"

    def is_steam_running(self) -> bool:
        """检查 Steam 是否正在运行"""
        if not self._detector:
            return False
        return self._detector.is_steam_running()

    def kill_steam(self) -> tuple[bool, str]:
        """强制关闭 Steam 进程"""
        if not self._detector:
            return False, "Steam 检测器未初始化"
        return self._detector.kill_steam()

    def get_dll_manager(self) -> DLLManager:
        """获取 DLL 管理器实例"""
        return self._dll_manager

    # ── 便捷目录与维护工具（参考 OpenSteam-Kitten）──

    def open_directory(self, target: str) -> tuple[bool, str]:
        """在文件资源管理器中打开指定目录"""
        if not target:
            return False, "目录路径为空"
        if not os.path.exists(target):
            try:
                os.makedirs(target, exist_ok=True)
            except Exception as e:
                return False, f"目录不存在且无法创建: {e}"

        try:
            if sys.platform == "win32":
                os.startfile(os.path.normpath(target))
            else:
                subprocess.Popen(["xdg-open", target])
            return True, f"已打开目录: {target}"
        except Exception as e:
            logger.error(f"Failed to open directory {target}: {e}")
            return False, f"打开目录失败: {e}"

    def get_depotcache_dir(self) -> str:
        """获取 Steam/depotcache 目录"""
        return os.path.join(self._steam_path, "depotcache") if self._steam_path else ""

    def get_stplugin_dir(self) -> str:
        """获取 Steam/config/stplug-in 目录"""
        return os.path.join(self._steam_path, "config", "stplug-in") if self._steam_path else ""

    def get_downloading_dir(self) -> str:
        """获取 Steam/steamapps/downloading 目录"""
        return os.path.join(self._steam_path, "steamapps", "downloading") if self._steam_path else ""

    def clean_download_cache(self, app_id: str | None = None) -> tuple[bool, str]:
        """一键清理 Steam 异常下载残留缓存与损坏状态（参考 OpenSteam-Kitten）"""
        if not self._steam_path or not os.path.isdir(self._steam_path):
            return False, "Steam 路径未设置或不存在"

        import shutil
        steamapps = os.path.join(self._steam_path, "steamapps")
        dl_dir = os.path.join(steamapps, "downloading")
        temp_dir = os.path.join(steamapps, "temp")

        removed_count = 0
        total_freed_bytes = 0

        def _safe_remove(path: str):
            nonlocal removed_count, total_freed_bytes
            try:
                if os.path.isfile(path):
                    sz = os.path.getsize(path)
                    os.remove(path)
                    total_freed_bytes += sz
                    removed_count += 1
                elif os.path.isdir(path):
                    for root, _, files in os.walk(path):
                        for f in files:
                            fp = os.path.join(root, f)
                            try:
                                total_freed_bytes += os.path.getsize(fp)
                                os.remove(fp)
                                removed_count += 1
                            except OSError:
                                pass
                    shutil.rmtree(path, ignore_errors=True)
            except Exception as e:
                logger.debug(f"Failed to remove {path}: {e}")

        if app_id:
            target_dl = os.path.join(dl_dir, str(app_id))
            target_temp = os.path.join(temp_dir, str(app_id))
            if os.path.exists(target_dl):
                _safe_remove(target_dl)
            if os.path.exists(target_temp):
                _safe_remove(target_temp)

            acf_path = os.path.join(steamapps, f"appmanifest_{app_id}.acf")
            if os.path.isfile(acf_path):
                try:
                    with open(acf_path, "r", encoding="utf-8", errors="replace") as f:
                        acf_txt = f.read()
                    if '"StateFlags"\t\t"1026"' in acf_txt or '"StateFlags"\t\t"1024"' in acf_txt:
                        os.remove(acf_path)
                        removed_count += 1
                        logger.info(f"Removed corrupt appmanifest for AppID {app_id}")
                except Exception as e:
                    logger.debug(f"Error checking ACF {acf_path}: {e}")
            freed_mb = total_freed_bytes / (1024 * 1024)
            return True, f"已清理 AppID {app_id} 下载残留（释放 {freed_mb:.1f} MB，共 {removed_count} 个文件）"
        else:
            if os.path.isdir(dl_dir):
                for item in os.listdir(dl_dir):
                    _safe_remove(os.path.join(dl_dir, item))
            if os.path.isdir(temp_dir):
                for item in os.listdir(temp_dir):
                    _safe_remove(os.path.join(temp_dir, item))

            if os.path.isdir(steamapps):
                for item in os.listdir(steamapps):
                    if item.startswith("appmanifest_") and item.endswith(".acf"):
                        acf_p = os.path.join(steamapps, item)
                        try:
                            with open(acf_p, "r", encoding="utf-8", errors="replace") as f:
                                txt = f.read()
                            if '"StateFlags"\t\t"1026"' in txt:
                                os.remove(acf_p)
                                removed_count += 1
                        except Exception:
                            pass

            freed_mb = total_freed_bytes / (1024 * 1024)
            return True, f"已成功清理 Steam 异常下载缓存（释放 {freed_mb:.1f} MB，共清理 {removed_count} 个项目）"
