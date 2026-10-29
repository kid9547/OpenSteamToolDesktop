# 更新日志

本文件记录 OpenSteamToolDesktop 近期版本的更新内容，可直接用于 GitHub Release 说明。

---

## v1.2.3（2026-10-29）

### 修复

- 修复「组件状态」「诊断中心」「Lua 规则」「云存档」「AppID 查询」页面打开后空白/全黑的问题。根因是 `QScrollArea` 内容控件未启用自适应，实际尺寸为 0×0；现已统一设置 `setWidgetResizable(True)`。
- 新增 GUI 装配回归测试，确保上述页面内容区尺寸始终非零，防止问题复发。

### 文档

- 中文 `README.md` 与英文 `README_EN.md` 新增「功能参考与实现来源 / Reference Projects & Feature Provenance」表，逐项标注参考项目。
- 新增本更新日志。

### 发布包

- `OpenSteamToolDesktop-1.2.3.zip`（便携版，解压即用）
- SHA256：`5B8B1CC42A7EF6A177F64A77EB136154194445104E31B635C8AE72763125FFA0`

---

## v1.2.2（2026-10-29）

### 新增

- **诊断中心**：环境快照、按级别/关键词检索日志、崩溃转储扫描，一键导出脱敏诊断包（纯本地，不上传）。
- **AppID 查询与更新检测**：基于本地 `appmanifest` 与 `appinfo.vdf`（v39/v40/v41）查询 Build、Depot 清单完整性、Lua 规则和 `depotcache` 状态。

### 修复

- **D 加密票据**：强制校验 `AppTicket` + `ETicket`，修复 AppID 位于 2 字节对齐偏移时识别失败的问题；提取器继续随安装包内置。

---

## v1.2.1（2026-10-29）

### 新增

- **组件状态中心**：通过 Windows Toolhelp 检测三个内核 DLL 是否真正加载到 Steam 进程，区分「文件存在」与「真正生效」。
- **Manifest 版本固定**：为指定 Depot 固定清单 GID，重新生成 Lua 或一键补全时不会被新版本覆盖；固定记录保存在 `%LOCALAPPDATA%/OpenSteamToolDesktop/manifest_pins.json`。
- **Lua 规则管理**：同时扫描 `config/lua` 与 `config/stplug-in`，支持启停、冲突检测和规则详情。
- **云存档管理**：扫描并备份/还原 Steam `userdata` 云存档，支持多账号，删游戏或换号前先留档。

---

## v1.2.0（2026-10-29）

### 内核与兼容性

- 内置内核升级为 **ZSteamTool v1.2.1**，适配当前 Steam 客户端。
- 支持 `[denuvo] mode = "normal"/"compat"`、`seteticketurl`、`onlinefix`、`setStat`、`setAppTicket`、`setETicket`、`setManifestid`、`forcedenuvo` 等能力。
- 新增当前 Steam build 的 pattern/IPC TOML，并内置零种子解析器与离线 fallback。
- 内核部署支持 `kernel.json` 标记、旧内核识别、内置内核优先、离线安装和迁移报告。

### 配置

- 保留已有 `opensteamtool.toml`，自动补齐 `[lua]`、`[denuvo]`、`[onlinefix]`、`[cloud]`、`[remote]` 段。
- Lua 路径同时包含 `config/lua` 与 `config/stplug-in`。
- 设置页新增 OnlineFix 环境、云存档重定向、清单源、远程镜像等配置卡片。

### 票据与游戏库

- D 加密票据支持 `AppTicket` + `ETicket` 校验、本机提取、手动粘贴，注册表与 Lua 双写；提取器随包内置，输出到可写应用数据目录。
- 支持 SteamID `setStat`、DLC depot 去重、清单通道探测/回退、镜像 UI。

---

## 参考项目

各功能的参考项目与实现来源见 README：

- 中文：[功能参考与实现来源](README.md#功能参考与实现来源)
- English: [Reference Projects & Feature Provenance](README_EN.md#reference-projects--feature-provenance)
