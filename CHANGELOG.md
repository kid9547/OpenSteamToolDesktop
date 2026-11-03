# 更新日志

本文件记录 OpenSteamToolDesktop 近期版本的更新内容，可直接用于 GitHub Release 说明。

---

## v1.3.1（2026-11-03）

### 修复

- **D 加密切号授权失败 / 低价区小号授权异常**：
  - 内置内核从 `hazjy/ZSteamTool v1.2.1` 升级为 `huanyuejue/OpenSteamTool 1.5.4`；
  - 1.5.4 把授权窗口从固定时间租约改为 **IPC 握手计数状态机**，只认第一个授权 pipe，避免多 pipe 抢写票据；
  - 切号后优先使用**当前登录账号名下**的 AppTicket/ETicket，授权结束时把当前账号 SteamID 写回凭据库，解决旧内核写错身份导致的 `012`、`88500012` 与部分 `error 54`；
  - 新增保护扫描的「高熵可写段」判定，修复换版本/加壳后识别不出 Denuvo、进而跳过切号授权的问题。
- **旧内核自动迁移**：启动检查发现 `kernel.json` 仓库不是 `huanyuejue/OpenSteamTool` 时，按旧内核处理并自动下载/切换 1.5.4，无需手动删版本目录。
- **失效开关降级**：1.5.4 已移除 `[denuvo] mode` 与 `seteticketurl`，D 加密页不再显示 normal/compat 开关，避免写入内核不认识、会中断 Lua 加载的指令。
- **强制 Denuvo 识别**：游戏「编辑」对话框新增「强制按 Denuvo 授权」选项，写入 `forcedenuvo(appid)`，用于保护扫描漏判的游戏；旧内核会自动禁用该选项。
- **升级提示修正**：内核下载完成后明确提示到「注入管理」重新部署 DLL，再重启 Steam。

---

## v1.3.0（2026-11-02）

### 优化与重构

- **导航栏瘦身重构（3+1 黄金架构）**：
  - 彻底精简重组原有 13 个拥挤的一级侧边栏，收敛为「首页」「游戏中心」「内核管理」3 个高频核心区与底部「设置」；
  - **游戏中心**：顶部分段无缝切换「我的游戏库」与「搜索入库」；
  - **游戏库「高级与维护」**：在游戏库工具栏内置下拉抽屉，统一收纳「散装清单与缓存维护」「Lua 规则与冲突审计」「云存档备份中心」「D 加密授权向导与提取」，将低频维护功能彻底回归游戏资产，日常使用界面极度清爽；
  - **内核管理**：将原「注入管理」与「组件状态」合并为一体化页面，支持一键切换与三内核 DLL 真实加载及 SHA256 冲突校验；
  - **设置页**：底部折叠收纳「诊断中心」排错日志导出与「AppID 探针」检测。

### 修复

- **修复 Steam 下载报错 Bug（Missing decryption key）**：
  - 深度排查并修复新游戏（如 4115450、5105310）入库后在 Steam 中无法下载、报错 `Missing decryption key` 的问题；
  - 为 `MetadataFetcher` 增加 24 小时本地缓存 TTL 失效与自动强制刷新机制，解决依赖过时静态缓存导致新游戏无法获取 Depot 解密密钥的缺陷；
  - 升级社区全量密钥库（当前包含 241,346 条实时密钥）；
  - 在 `CompleteManifestService` 与 `ManifestResolver` 清单补全链路中，为所有未配对密钥的 Depot 自动从社区密钥库匹配补充 Hex 解密密钥并幂等回写 Lua；
- **Lua 清单请求码解析与溢出加固**：
  - 加固 `manifest.lua`，解决 64 位无符号 GID 在 Lua 字符串拼接时被格式化为负数溢出的缺陷；
  - 接入 ManifestDeX（PR #200）解析优先级，并自动同步热更新至 `<Steam>/config/lua` 与 `<Steam>/config/stplug-in`。

### 发布包

- `OpenSteamToolDesktop-1.3.0.zip`（便携版，解压即用）
- SHA256：`6931FFFE0E42EDC6625B16E21D0CD5BFDF86A85521A8D41E7D77166F75111155`

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
