<h1 align="center">OpenSteamTool-Desktop</h1>

<p align="center">
  基于 <a href="https://github.com/OpenSteam001/OpenSteamTool">OpenSteamTool</a> 的 Steam 游戏库管理桌面工具<br>
  Windows 11 Fluent Design 风格 GUI，一站式游戏解锁与管理体验
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue?logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/Steam-必需-1b2838?logo=steam" alt="Steam Required">
  <img src="https://img.shields.io/badge/GUI-Fluent%20Design-0078D4?logo=microsoft" alt="GUI">
  <img src="https://img.shields.io/github/v/release/kid9547/OpenSteamToolDesktop?color=green" alt="Release">
  <img src="https://img.shields.io/github/downloads/kid9547/OpenSteamToolDesktop/total" alt="Downloads">
</p>

<p align="center">
  <a href="README_EN.md">English</a>
</p>

---

> ⚠️ **仅支持 Windows 10 / Windows 11 64 位系统**，需要已安装 Steam 客户端并登录。不支持 macOS / Linux。

---

## 下载

前往 [Releases](https://github.com/kid9547/OpenSteamToolDesktop/releases) 页面下载最新版本。

---

## v1.2.3 修复

- 修复「组件状态」「诊断中心」「Lua 规则」「云存档」「AppID 查询」页面内容区为 0×0、打开后空白/全黑的问题。
- 为上述页面补充装配回归测试，确保 ScrollArea 内容区始终随窗口自适应。

完整更新记录见 [CHANGELOG.md](CHANGELOG.md)。

---

## v1.2.2 更新内容

- **诊断中心**：环境快照、按级别/关键词检索日志、崩溃转储扫描，一键导出脱敏诊断包。
- **AppID 查询与更新检测**：基于本地 `appmanifest` 与 `appinfo.vdf` 查询 Build、Depot 清单完整性和 Lua 规则状态。
- **D 加密票据**：强制校验 `AppTicket` + `ETicket`，修复 AppID 位于 2 字节对齐偏移时识别失败的问题；提取器继续随安装包内置。
- 保留并完善 1.2.1 的组件状态中心、Manifest 版本固定、云存档管理与 Lua 规则管理。

---

## 目录

- [功能概览](#功能概览)
- [OpenSteamTool 底层能力](#opentool-底层能力)
- [首页概览](#首页概览)
- [使用指南](#使用指南)
- [入库效果](#入库效果)
- [安全软件提示](#安全软件提示)
- [常见问题](#常见问题)
- [开发者指南](#开发者指南)
- [免责声明](#免责声明)
- [开源许可](#开源许可)
- [功能参考与实现来源](#功能参考与实现来源)

---

## 功能概览

| 功能             | 说明                                                  |
| -------------- | --------------------------------------------------- |
| **清单/Lua 导入**  | 支持点击选择或**全窗口拖拽**（`.manifest` / `.lua` / `.zip` 自动解压）批量导入，智能提取 AppID 并部署到双缓存目录 |
| **清单自动补全**   | 优先读取 AppID 分支归档并以归档内真实 GID 更新 Lua，兼容 CDN、社区仓库和历史清单版本，避免“文件已下载但仍显示缺失” |
| **清单缓存管理**   | 可扫描 `depotcache` 与 `config/depotcache`，识别损坏项、重复项和未被 Lua 引用的孤儿清单；不会未经确认自动删除文件 |
| **403 错误修复**   | **新增**：自动部署 `opensteamtool.toml` 与 `manifest.lua` 多源解析器（wudrm/steamrun），彻底解决 Steam 下载清单时 403 Access Denied 报错 |
| **现代青蛙图标**    | **新增**：高清反锯齿全透明现代青蛙设计，修复 Windows 任务栏应用白图标显示问题 |
| **注入管理**       | 一键部署/移除 OpenSteamTool DLL，三层验证确保注入状态准确，支持快捷重启 Steam |
| **游戏搜索入库**     | 按 AppID 或英文名称搜索 Steam 游戏，自动获取元数据并生成入库配置             |
| **DLC 全解锁**    | 自动识别游戏关联的 DLC 列表，一键全部入库                             |
| **Depot 解密密钥** | 自动获取每个加密 Depot 的专属解密密钥，确保游戏内容可正常下载                  |
| **游戏库管理**      | 卡片式浏览已入库游戏，支持排序、搜索、右键快捷操作（查看/复制/出库）                 |
| **组件状态中心**     | **新增**：确认三个内核 DLL 是否被 Steam 进程真正加载，区分「文件存在」与「真正生效」 |
| **Manifest 版本固定** | **新增**：为指定 Depot 固定清单 GID，重新生成 Lua 或一键补全时不会被新版本覆盖 |
| **云存档管理**      | **新增**：扫描并备份/还原 Steam `userdata` 云存档，支持多账号，删游戏或换号前先留档 |
| **Lua 规则管理**    | **新增**：同时扫描 `config/lua` 与 `config/stplug-in`，支持启停、冲突检测和规则详情 |
| **诊断中心**        | **新增**：环境快照、按级别/关键词检索日志、崩溃转储扫描，一键导出脱敏诊断包 |
| **AppID 查询与更新检测** | **新增**：基于本地 `appmanifest` 与 `appinfo.vdf` 查询 Build、清单完整性和 Lua 状态 |
| **D 加密票据**      | **新增**：`AppTicket` + `ETicket` 导入/本机提取/手动粘贴，注册表与 Lua 双写；提取器随安装包内置 |
| **版本自动更新**     | 启动时自动检测 GitHub Release 最新版本，确保始终使用最新版本              |

---

## OpenSteamTool 底层能力

OpenSteamTool 是通过 DLL 注入 Steam 客户端的 C++ 引擎，本工具为其提供图形化管理界面。注入后可实现：

| 能力               | 说明                                 |
| ---------------- | ---------------------------------- |
| **游戏解锁**         | 解锁未拥有的游戏及其全部 DLC                   |
| **Depot 解密**     | 自动注入 Depot 解密密钥，支持游戏文件正常下载         |
| **Manifest 下载**  | 自动下载 Depot Manifest，支持锁定特定版本防止自动更新 |
| **Access Token** | 支持需要访问令牌的受保护游戏/DLC                 |
| **统计与成就**        | 为未拥有游戏启用 Steam 统计和成就系统             |
| **热重载**          | Lua 配置文件修改后自动生效，无需重启 Steam         |

---

## 首页概览

启动后自动检测 Steam 安装状态、注入状态和游戏库概览：

![首页](assets/2026-06-03-13-38-03-image.png)

---

## 使用指南

### 第一步：注入 Steam

1. 进入「注入管理」页面
2. 点击 **「注入 Steam」** 按钮
3. 重启 Steam 客户端即可生效

> 如果 Steam 正在运行，程序会自动提示关闭后继续。

![注入管理](assets/2026-06-03-13-38-34-image.png)

### 第二步：搜索入库

1. 进入「搜索入库」页面
2. 输入游戏 **AppID**（如 `730` = CS2）或**英文游戏名**
3. 点击 **「入库」**，系统自动获取元数据、DLC 列表、解密密钥并生成配置

![搜索入库](assets/2026-06-03-13-38-55-image.png)

### 第三步：管理游戏库

1. 进入「游戏库」页面查看所有已入库游戏
2. 支持按名称/AppID 排序和搜索过滤
3. 右键游戏卡片可：复制 AppID、复制游戏名、在 Steam 中查看、出库

![游戏库](assets/2026-06-03-13-39-06-image.png)

---

## 入库效果

入库成功后，游戏将直接出现在你的 Steam 游戏库中：

![入库后](assets/2026-06-03-13-49-34-image.png)

---

## 安全软件提示

如果 Windows 安全中心对程序报毒（DLL 注入行为可能触发误报），请按以下步骤处理：

1. 打开 Windows 安全中心

![Windows 安全中心](assets/2026-06-03-14-16-28-image.png)

2. 点击「病毒和威胁防护」→「管理设置」

![管理设置](assets/2026-06-03-14-27-30-image.png)

3. 关闭「实时保护」。Windows 安全中心对 DLL 注入类工具容易产生误报，关闭后可避免程序被拦截影响运行。

![关闭实时保护](assets/2026-06-03-14-27-05-image.png)

---

## 常见问题

### 注入后 Steam 没有变化？

请确保已**重启 Steam**。DLL 注入只在 Steam 启动时生效。可在「注入管理」页面点击「验证注入」检查状态。

### 游戏入库后提示「内容仍处于加密状态」？

部分 Depot 缺少解密密钥时会触发此提示。系统已自动获取解密密钥，如果仍有问题，请检查网络连接是否正常。

### 如何卸载注入？

在「注入管理」页面点击「移除注入」，然后重启 Steam。也可以手动删除 Steam 根目录下的三个 DLL 文件。

### D 加密（Denuvo）游戏进不去 / 报错 88500005？

Denuvo 游戏需要**由 Valve 私钥签名的真实票据**（`AppTicket` + `ETicket`），本地无法伪造，因此：

1. 在游戏卡片右键菜单选择 **「D 加密授权（导入/提取票据）」**；
2. 三个来源任选其一：
   * **文件导入** —— 拖入 `tickets.txt` / `appticket.bin` / `eticket.bin` / `.lua` / `.json`；
   * **本机提取** —— 在**拥有该游戏的正版账号**机器上，Steam 已登录时点「开始提取」；
   * **手动粘贴** —— 直接粘贴十六进制票据串；
3. 程序会同时写入 **注册表凭据存储**
   （`HKCU\Software\Valve\Steam\Apps\<AppID>` 的 `AppTicket` / `ETicket`，`REG_BINARY`）
   和该游戏的 **Lua 配置**（`setAppTicket` / `setETicket`）；
4. **重启 Steam** 后生效。

注意事项：

* 票据有效期约 **30 分钟 ~ 数小时**，过期会报 `88500005`，需要重新导入；
* 票据必须属于 **当前登录的 Steam 账号**，否则 Denuvo 交叉校验会失败（错误码 `012`）；
* 尚未启动过该游戏的账号可能拿不到 `ETicket`（会返回 `EResult 15`）；
* 换硬件 / 重装系统后需要重新授权。

> 想了解底层原理（为什么不能离线破解、参考软件到底怎么做的）请看
> [DENUVO_REVERSE_ENGINEERING_REPORT.md](DENUVO_REVERSE_ENGINEERING_REPORT.md)。

### 提取工具提示 Steam 未运行或提取失败？

本机提取依赖 `tools/extract_tickets/extract_tickets.exe` 与已登录的 Steam 会话通信。
从 **1.2.1** 起该提取器已随安装包内置，无需单独编译。

提取失败通常有两类原因：

* **提示 `ConnectToGlobalUser failed`**：当前登录账号并不拥有该 AppID，Steam 不会为该账号建立会话；
* **只能拿到 `AppTicket`、`ETicket` 为 `null`（`EResult 15`）**：Denuvo 票据必须由**实际拥有该游戏且已启动过该游戏**的账号生成，家庭共享/借用授权无法替代。

若内置文件缺失，可用 `tools/build_extract_tickets.ps1` 重新编译（需要 MinGW-w64）。

---

## 开发者指南

本项目为开源项目，欢迎参与开发与调试。以下是本地搭建开发环境的步骤。

### 环境要求

| 依赖 | 版本要求 |
|------|----------|
| 操作系统 | Windows 10 / 11（64 位） |
| Python | **3.10+**（推荐 3.12 或 3.13） |
| Steam 客户端 | 已安装并登录 |
| Git | 任意版本 |

### 1. 克隆仓库

```bash
git clone https://github.com/kid9547/OpenSteamToolDesktop.git
cd OpenSteamToolDesktop
```

### 2. 创建虚拟环境

```bash
# 在项目根目录创建 .venv
python -m venv .venv

# 激活虚拟环境
.venv\Scripts\activate
```

> 激活后命令行前缀会显示 `(.venv)`，表示已进入虚拟环境。

### 3. 安装依赖

```bash
pip install -r requirements.txt
```

主要依赖一览：

| 包名 | 用途 |
|------|------|
| `PyQt6` | GUI 框架 |
| `PyQt6-Fluent-Widgets` | Windows 11 Fluent Design 组件库 |
| `httpx` | HTTP 客户端（API 请求、封面下载） |
| `requests` | 备用 HTTP 客户端 |
| `pytest` | 单元测试 |

### 4. 启动调试

```bash
# 直接运行
python main.py
```

程序启动后会自动：
- 检测 Steam 安装路径（注册表）
- 检查 DLL 版本并自动下载（如缺失）
- 检查应用版本更新
- 加载游戏库

**调试技巧：**

- 日志文件位于 `~/.OpenSteamToolDesktop/logs/main.log`，启动时实时查看可快速定位问题
- 配置文件位于 `~/.OpenSteamToolDesktop/config.json`
- DLL 缓存位于 `~/.OpenSteamToolDesktop/DLL/`
- Lua 配置文件位于 `~/.OpenSteamToolDesktop/config/lua/`

### 5. 使用 IDE 调试

<details>
<summary><b>VS Code</b></summary>

1. 安装 [Python 扩展](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
2. 打开项目根目录
3. 选择解释器：`Ctrl+Shift+P` → `Python: Select Interpreter` → 选择 `.venv` 中的 Python
4. 按 `F5` 启动调试，或在 `launch.json` 中添加配置：

```json
{
    "name": "OpenSteamToolDesktop",
    "type": "python",
    "request": "launch",
    "program": "${workspaceFolder}/main.py",
    "console": "integratedTerminal",
    "justMyCode": false
}
```

</details>

<details>
<summary><b>PyCharm</b></summary>

1. `File → Open` 打开项目根目录
2. `File → Settings → Project → Python Interpreter` → 选择 `.venv\Scripts\python.exe`
3. 右键 `main.py` → `Debug 'main'` 即可断点调试

</details>

### 6. 运行测试

```bash
# 运行全部测试
pytest

# 运行单个测试文件
pytest tests/test_game_manager.py

# 显示详细输出
pytest -v
```

### 7. 项目结构

```
OpenSteamToolDesktop/
├── main.py                  # 应用入口
├── config.py                # 全局配置常量（URL、颜色、HTTP 等）
├── requirements.txt         # 运行时依赖
├── build_exe.py             # 打包脚本（PyInstaller / Nuitka）
├── OpenSteamToolDesktop.spec # PyInstaller 配置
├── core/                    # 核心业务逻辑
│   ├── steam_bridge.py      #   Steam 桥接（DLL 管理、注入）
│   ├── game_manager.py      #   Lua 游戏配置生成与管理
│   ├── metadata_fetcher.py  #   Steam 元数据获取
│   ├── dll_manager.py       #   DLL 下载与版本管理
│   ├── dll_injector.py      #   DLL 注入与验证
│   ├── version_checker.py   #   应用版本检查
│   └── ...
├── gui/                     # 界面层
│   ├── main_window.py       #   主窗口
│   ├── home_page.py         #   首页
│   ├── search_page.py       #   搜索入库页
│   ├── library_page.py      #   游戏库页
│   ├── inject_page.py       #   注入管理页
│   ├── accelerate_page.py   #   科学加速页
│   └── widgets/             #   自定义控件
├── utils/                   # 工具模块
│   ├── http_client.py       #   统一 HTTP 客户端
│   ├── async_worker.py      #   QThread 异步任务封装
│   ├── logger.py            #   日志模块
│   └── ...
├── resources/               # 静态资源
│   └── fallback_dlls/       #   内置兜底 DLL
├── assets/                  # 截图、图标
└── tests/                   # 单元测试
```

### 8. 打包构建

```bash
# 安装打包工具
pip install pyinstaller

# PyInstaller 打包（默认，生成 dist/ 目录 + Zip 包）
python build_exe.py

# Nuitka 编译打包（C++ 编译，代码保护更强）
python build_exe.py --nuitka

# 仅清理构建产物
python build_exe.py --clean

# 打包但不生成 Zip
python build_exe.py --no-zip
```

打包产物位于 `dist/` 目录，同时会在项目根目录生成 `OpenSteamToolDesktop-{版本号}.zip`。

---

## 免责声明

本项目仅供**学习和研究**使用。请遵守以下原则：

- 本工具不提供游戏下载功能
- 请尊重游戏开发者的劳动成果，支持正版
- 使用本工具产生的任何后果由使用者自行承担
- 禁止将本工具用于任何商业用途

---

## 开源许可

本项目基于底层引擎 [OpenSteamTool](https://github.com/OpenSteam001/OpenSteamTool) 为开源项目。

---

## 功能参考与实现来源

本项目的部分功能参考了以下开源项目的交互方式与实现思路。**参考不等于直接复制**：除明确标注的内核 DLL 外，界面、校验逻辑与本地文件处理均由本项目独立实现。

| 功能 | 参考项目 | 参考内容与本项目的差异 |
| --- | --- | --- |
| **组件状态中心** | [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool)、[huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua) | 参考组件安装/加载状态与备用加载器说明；本项目使用 Windows Toolhelp 自行检测 Steam 进程模块，不依赖 `psutil` |
| **诊断中心** | [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool) | 参考日志筛选、崩溃转储与诊断收集；本项目增加环境快照、配置脱敏和一次性诊断包导出 |
| **Lua 规则管理** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua)、[G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool) | 参考 Lua 启停、批量导入与游戏配置管理；本项目同时扫描 `config/lua` 和 `config/stplug-in`，并增加冲突检测 |
| **云存档管理** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua) | 参考云存档备份/还原思路；本项目直接处理 Steam `userdata`，不依赖 CloudRedirect DLL |
| **Manifest 版本固定** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua)、[hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | 参考“固定到当前/最新清单”和 `setManifestid`；本项目用本地 `manifest_pins.json` 保存固定记录，补全时只补空不覆盖 |
| **D 加密票据** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua)、[hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | 参考票据提取/导入与 `[denuvo] mode`；本项目要求 `AppTicket` + `ETicket` 双票据，并同时写入注册表和 Lua |
| **AppID 查询与更新检测** | [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool)、[SteamDatabase/SteamAppInfo](https://github.com/SteamDatabase/SteamAppInfo)、[SteamDatabase/ValveKeyValue](https://github.com/SteamDatabase/ValveKeyValue) | 参考本地缓存查询与 `appinfo.vdf` 二进制解析；本项目增加 Build、Depot 清单、Lua 规则和固定记录的联合检测 |
| **签名表 / 数据通道** | [huanyuejue/steam-monitor](https://github.com/huanyuejue/steam-monitor)、[hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | 采用零种子 pattern/IPC 解析思路；本项目预生成当前 Steam 版本的 TOML 并随包兜底，镜像不可达时仍可启动 |
| **云存档重定向** | [Selectively11/CloudRedirect](https://github.com/Selectively11/CloudRedirect)（原仓库已归档）、[hazjy/CloudRedirect](https://github.com/hazjy/CloudRedirect) | 仅提供 `[cloud]` 配置、开关和 DLL 检测；本项目**不捆绑** `cloud_redirect.dll`，需要用户自行放置 |
| **内核 DLL** | [hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | 当前打包的是 ZSteamTool **v1.2.1** 内核；其上游为 [OpenSteam001/OpenSteamTool](https://github.com/OpenSteam001/OpenSteamTool)，并参考了 [BetterSteamTools](https://github.com/madoiscool/BetterSteamTools) 的改动 |
| **桌面端基础** | [yong0512/OpenSteamToolDesktop](https://github.com/yong0512/OpenSteamToolDesktop) | 本项目在其基础上进行功能二次开发与维护 |

> 说明：上述功能均已在真实 Steam 环境中做过页面渲染与数据读取验证；Lua 启停、云存档备份、Manifest 固定、D 加密票据等写入操作会尽量保留原文件或备份。D 加密真实授权仍要求使用**实际拥有该游戏**的账号，详见上方 FAQ。
>
> 打包的 ZSteamTool 内核按 **GPL-3.0** 分发，源码与许可证见其仓库；其余参考项目的版权归各自作者所有。

---

## 致谢与项目溯源

- 本项目由 [kid9547](https://github.com/kid9547/OpenSteamToolDesktop) 基于 [yong0512/OpenSteamToolDesktop](https://github.com/yong0512/OpenSteamToolDesktop) 进行功能二次开发与维护。
- 底层引擎基于 [OpenSteamTool](https://github.com/OpenSteam001/OpenSteamTool)。
- 界面组件采用 [QFluentWidgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets)。

---

## Star 走势

[![Star History Chart](https://api.star-history.com/svg?repos=kid9547/OpenSteamToolDesktop&type=Date)](https://star-history.com/#kid9547/OpenSteamToolDesktop&Date)

---

<p align="center">
  <sub>Made with ❤️ for the Steam community</sub>
</p>
