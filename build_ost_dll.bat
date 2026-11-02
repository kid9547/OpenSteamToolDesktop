@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

REM ============================================================================
REM  build_ost_dll.bat — 从源码编译 OpenSteamTool.dll（含 CNetPacket 布局兼容补丁）
REM
REM  产出（Release）：
REM    _re_analysis\ost_repo\OpenSteamTool-main\build\Release\OpenSteamTool.dll
REM    _re_analysis\ost_repo\OpenSteamTool-main\build\Release\dwmapi.dll
REM    _re_analysis\ost_repo\OpenSteamTool-main\build\Release\xinput1_4.dll
REM
REM  可选：--install  额外把三个 DLL 复制到 resources\fallback_dlls\
REM                （仅当本脚本自己编译的产物；脚本会写入 version.txt 标记）
REM
REM  ============================ 重要提示 =============================
REM  本脚本属于【参考资料】，当前项目【未启用】该补丁。
REM  修复工作等待上游官方更新（PR #212 未合并）。
REM
REM  若只是要打包发行版，请【不要】运行本脚本，
REM  否则 --install 会把未经测试的自编译 DLL 装进 fallback 目录，
REM  影响现有使用流程。
REM
REM  详见：ost_patch\README.md
REM  ========================================================================


set "REPO=%~dp0_re_analysis\ost_repo\OpenSteamTool-main"
set "FALLBACK=%~dp0resources\fallback_dlls"
set "DO_INSTALL=0"
for %%A in (%*) do (
    if /I "%%~A"=="--install" set "DO_INSTALL=1"
)

echo ============================================================
echo  OpenSteamTool.dll 自编译（含 Steam 1791918634 兼容补丁）
echo ============================================================
echo  仓库: %REPO%

if not exist "%REPO%\src\CMakeLists.txt" (
    echo [ERROR] 找不到源码: %REPO%\src\CMakeLists.txt
    echo         请先克隆 OpenSteamTool 源码到 _re_analysis\ost_repo\OpenSteamTool-main
    exit /b 1
)

REM ---- 确认补丁已应用 -------------------------------------------------
findstr /C:"PacketFields" "%REPO%\src\Hook\Hooks_NetPacket.cpp" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 检测到补丁未应用：Hooks_NetPacket.cpp 中没有 PacketFields
    echo         请先执行：git -C "%REPO%" apply "%~dp0ost_patch\CNetPacket_layout_fix.patch"
    exit /b 1
)
echo [OK]   CNetPacket 兼容补丁已应用

REM ---- 定位工具链 -----------------------------------------------------
where cmake >nul 2>nul
if errorlevel 1 (
    echo [ERROR] 找不到 cmake。请安装：
    echo          winget install Kitware.CMake
    echo          scoop install cmake
    exit /b 1
)

REM ---- 定位 MSVC ------------------------------------------------------
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" (
    echo [ERROR] 找不到 vswhere，无法定位 Visual Studio。
    echo         请安装：winget install Microsoft.VisualStudio.2022.BuildTools
    echo         或完整版 VS 2022（勾选「使用 C++ 的桌面开发」）
    exit /b 1
)

set "VCVARS="
for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -products * -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath`) do (
    if exist "%%i\VC\Auxiliary\Build\vcvars64.bat" set "VCVARS=%%i\VC\Auxiliary\Build\vcvars64.bat"
)

if "!VCVARS!"=="" (
    echo [ERROR] 找到了 Visual Studio，但缺少 C++ 工具链组件。
    echo         请在 VS Installer 里勾选「使用 C++ 的桌面开发」
    exit /b 1
)
echo [OK]   MSVC: !VCVARS!

REM ---- 进入 MSVC 环境 -------------------------------------------------
call "!VCVARS!" >nul
if errorlevel 1 (
    echo [ERROR] vcvars64.bat 执行失败
    exit /b 1
)

REM ---- 配置 + 构建 ----------------------------------------------------
pushd "%REPO%"

where ninja >nul 2>nul
if not errorlevel 1 (
    set "GEN=Ninja Multi-Config"
) else (
    set "GEN=Visual Studio 17 2022"
)
echo [INFO] Generator: !GEN!

REM 首次构建会 FetchContent 拉取 Lua/Detours/spdlog/protobuf/tomlplusplus，
REM 需要网络（Clash 保持开启）。
echo [INFO] 配置中（首次会下载依赖，请确保网络通畅）...
cmake -S src -B build -G "!GEN!"
if errorlevel 1 goto :fail

echo [INFO] 编译 Release ...
cmake --build build --config Release
if errorlevel 1 goto :fail

popd

set "OUT=%REPO%\build\Release"
if not exist "%OUT%\OpenSteamTool.dll" (
    echo [ERROR] 未找到产物: %OUT%\OpenSteamTool.dll
    exit /b 1
)

echo.
echo [OK]   编译完成
dir /b "%OUT%\*.dll"

REM ---- 可选：安装到 fallback -----------------------------------------
if "%DO_INSTALL%"=="1" (
    echo.
    echo [INFO] 安装到 %FALLBACK%
    if not exist "%FALLBACK%" mkdir "%FALLBACK%"
    copy /Y "%OUT%\OpenSteamTool.dll" "%FALLBACK%\" >nul
    copy /Y "%OUT%\dwmapi.dll"        "%FALLBACK%\" >nul
    copy /Y "%OUT%\xinput1_4.dll"     "%FALLBACK%\" >nul
    REM 标记来源，避免被 DLLManager 的远程版本检查误判
    > "%FALLBACK%\version.txt" echo v1.1.0-local-netpacket-fix
    echo [OK]   已安装并写入 version.txt
    echo        注意：下次 DLLManager 检测到「远程有更新」时可能重新下载官方版覆盖此处。
)

echo.
echo ============================================================
echo  完成。下一步：
echo    1. 把 build\Release 下三个 DLL 放到 Steam 根目录
echo    2. 重启 Steam，确认不再报 signature file not found
echo    3. 签名文件由 OpenSteamToolDesktop 自动同步（见 ost_patch\README.md）
echo ============================================================
exit /b 0

:fail
popd
echo [ERROR] 构建失败
exit /b 1
