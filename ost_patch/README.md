# Steam 1791918634 崩溃补丁（CNetPacket 布局兼容）

> 面向 OpenSteamToolDesktop 二次开发项目 · 2026-10-16

---

## ⚠️ 状态：参考资料，**当前未启用**

**本目录不参与本项目的任何构建与运行流程。** 经核实：

| 检查项 | 结论 |
|---|---|
| 是否被项目代码引用 | ❌ 无任何 Python 代码引用 `ost_patch/` |
| 是否被 `build_exe.py` 打包 | ❌ 打包脚本只收 `resources/fallback_dlls`，不含本目录 |
| 是否影响注入/移除流程 | ❌ 完全不影响 |
| `_re_analysis/` 源码副本是否进版本库 | ❌ 被 `.gitignore` 第 35 行忽略 |

**当前的处置决定（2026-10-16）**：

1. 本补丁**暂不启用**，仅作为技术资料存档。
2. 修复工作**等待上游官方更新**（PR #212 至今未合并，官方 release 仍停在 1.4.8）。
3. 现有使用流程保持原样，**零改动**。

**何时再考虑启用**：等官方发布含此修复的版本后，直接替换
`resources/fallback_dlls/` 里的三个 DLL 即可，本目录的资料可用来核对官方版本
是否真的带上了这个修复（见文末「如何验证官方版是否已修复」）。

> 补丁本身已应用到 `_re_analysis/ost_repo/` 的源码副本中，但该目录是分析用的临时区，
> 不参与打包，也不会被用户接触到。

---

## 交付物

| 文件 | 说明 |
|---|---|
| `ost_patch/CNetPacket_layout_fix.patch` | 规范 diff，**LF 行尾**，可对纯净上游源码干净应用（已实测 `git apply` 返回 0） |
| `ost_patch/README.md` | 本文档：根因、方案对比、编译与验证步骤 |
| `build_ost_dll.bat` | 一键编译脚本，自动定位 MSVC 并校验补丁已应用 |

补丁对应的 git blob 哈希为 `5fe24be..ea431e5`，与上游 PR #212 完全一致
（由 `git diff` 从上游原文件与本项目已打补丁的文件生成，可交叉核对）。

### 重新应用补丁

```powershell
# 若 _re_analysis 源码副本被清理过，需要重新克隆后：
git -C _re_analysis\ost_repo\OpenSteamTool-main apply ..\..\ost_patch\CNetPacket_layout_fix.patch
```


## 一句话结论

采用**上游 PR #212 的运行时自适应方案**，自己编译，不回退 Steam，不锁定 build 号。
本补丁**同时兼容新旧 Steam 构建**。

---

## 1. 出了什么事

Steam 于 2026-10-14 把 stable 通道更新到 **Build 1791918634**，OpenSteamTool 1.4.8 在
Steam 启动时崩溃（登录阶段 `0xC0000005`，faulting module = `OpenSteamTool.dll`）。

相关 issue：[#208](https://github.com/OpenSteam001/OpenSteamTool/issues/208)、
[#209](https://github.com/OpenSteam001/OpenSteamTool/issues/209)、
[#211](https://github.com/OpenSteam001/OpenSteamTool/issues/211)、
[#213](https://github.com/OpenSteam001/OpenSteamTool/issues/213)、
[#214](https://github.com/OpenSteam001/OpenSteamTool/issues/214)

### 一个重要的坑

初期所有人都以为是「签名文件缺失」，去补 TOML 就行。**结果补完 Steam 反而崩得更厉害。**

`@Darksoan` 在 [#209](https://github.com/OpenSteam001/OpenSteamTool/issues/209) 里做了
minidump 逆向，才找到真因：**TOML 签名数据是对的，错的是代码里的硬编码结构体偏移。**

---

## 2. 根因

Steam 改了 `CNetPacket` 结构体，在 `m_hConnection` 之后**插入了 8 字节新字段**，
其后所有字段整体后移 +8：

| 字段 | 旧偏移 | 新偏移 |
|---|---|---|
| `m_pubData` | +0x08 | **+0x10** |
| `m_cubData` | +0x10 | **+0x18** |
| `m_cRef` | +0x14 | +0x1C |
| `m_pubNetworkBuffer` | +0x18 | +0x20 |
| `m_pNext` | +0x20 | +0x28 |

`Hooks_NetPacket.cpp` 里的 `RecvPkt` hook 仍按旧偏移读包，`+0x08` 处现在是垃圾值
（实测 `0x00000000ffffffff` 之类）而非指针，解引用即崩溃。

这解释了为什么「TOML 校验全过，Steam 照样崩」——**签名是对的，代码是旧的。**

---

## 3. 为什么选这个方案（对比）

| 方案 | 风险 | 旧版兼容 | 结论 |
|---|---|---|---|
| **A. 自编译 + PR #212**（本项目采用） | 低 | ✅ 自动兼容 | ✅ **推荐** |
| B. 直接用社区编译好的 zip | **高** | 未知 | ❌ 未签名第三方二进制注入 Steam |
| C. 回退 Steam 版本 | 中 | — | ❌ 治标，Steam 会再更新 |
| D. 把偏移写死成新布局 | 中 | ❌ 旧版全崩 | ❌ 不可取 |
| E. 禁用 RecvPkt hook 躲过崩溃 | 低 | ✅ | ⚠️ 会失去清单请求码注入、云存档、成就等能力 |

### 选 A 的三个理由

1. **你要求「没有风险」**：社区 zip 是未签名产物，直接注入游戏进程，来源不可控。
   自己从上游源码编译，代码来源可审计。
2. **自动兼容新旧版本**（见下节），这是本方案最大的价值。
3. **上游 PR 有实测结论**：作者在 1791918634 上验证 Steam 干净启动、加清单、下载正常。

---

## 4. 关键设计：为什么不用改死偏移

这是本补丁最重要的部分，也是它能同时兼容新旧 Steam 的原因。

```
判别方式：检查 CNetPacket+0x08 处的 qword 是否为「可读内存」
  ├── 可读   → 旧布局（该 qword 就是数据指针）→ bias = 0
  └── 不可读 → 新布局（该 qword 是新头部的垃圾）→ bias = 8
```

代码里 `RecvPkt` 收到原始指针后，先经 `PacketFields()` 得到「字段视图」，
交还给 Steam 时再用 `PacketRaw()` 还原成原始指针。

**再加一层兜底**：如果 `m_pubData` 指向不可读内存，整个 hook 原样透传给 Steam，
绝不解引用不可信的指针。也就是说——

> **认不出来就放行，而不是猜。** 猜错了就是崩溃。

---

## 5. 关于「Steam 是否强制更新」

你问的这点，实践下来是：**Steam 没有真正的「强制更新」开关，但会自动更新，
且回退需要每次手动做。** 社区里确实有人靠 `beta`/`stable` 通道切换来回退
（[#205](https://github.com/OpenSteam001/OpenSteamTool/issues/205) 里 ztkedou 的做法），
但这不可靠，而且**下次更新又会被打回去**。

更关键的是——本补丁**根本不需要考虑这个问题**：

- 布局判别是**运行时**做的，不是编译时写死的；
- Steam 以后再改布局，只要还是「在 `m_hConnection` 之后插入字段」这一类，
  插件能自动适配；
- 万一出现完全未知的布局，兜底逻辑会**跳过 hook** 而不是崩溃。

所以**不必回退 Steam，也不必锁 build 号**。

---

## 6. 本项目已完成的改动

补丁已应用到源码副本 `_re_analysis/ost_repo/OpenSteamTool-main`（上游 main @ `2a08b0bf`）：

| 文件 | 改动 |
|---|---|
| `src/OSTPlatform/include/Memory.h` | 新增 `IsReadable` 声明 |
| `src/OSTPlatform/Windows/Memory.cpp` | 新增 `IsReadable` 实现（VirtualQuery） |
| `src/Hook/Hooks_NetPacket.cpp` | 新增 `PacketFields`/`PacketRaw`，改写 `RecvPkt` |
| `src/Steam/Structs.h` | 注释记录偏移事实（无功能改动） |

> 注：`_re_analysis/` 已被项目 `.gitignore` 忽略，所以这份源码副本的改动不会进版本库。
> 补丁本体另存为 `ost_patch/CNetPacket_layout_fix.patch`，需要时可重新应用。

---

## 7. 怎么编译（重要）

### 前置条件

本机**目前没有安装任何 C++ 工具链**（已实测：`cmake`、`ninja`、`cl`、`msbuild`、
Visual Studio、LLVM 均不存在）。首次编译前需要安装：

```powershell
# 方式一：Visual Studio Build Tools（体积较小，推荐）
winget install Microsoft.VisualStudio.2022.BuildTools
# 安装时务必勾选「使用 C++ 的桌面开发」工作负载

# 方式二：完整版 VS 2022
winget install Microsoft.VisualStudio.2022

# 再装 cmake
winget install Kitware.CMake
```

> `cl` 是 MSVC 编译器，**无法用 scoop 里的 MinGW gcc 替代**——
> 上游代码大量使用 MSVC 专有扩展（Detours、`__forceinline`、`#pragma comment` 等）。

### 编译

```powershell
# 保持 Clash 开启（首次构建要 FetchContent 下载 Lua/Detours/spdlog/protobuf）
.\build_ost_dll.bat
```

脚本会自动：定位 MSVC → 选 Ninja 或 VS 生成器 → 配置 → 编译 Release →
**检查补丁是否已应用**（没打补丁会直接报错中止，避免编出崩溃的 DLL）。

### 产物

```
_re_analysis\ost_repo\OpenSteamTool-main\build\Release\
  ├─ OpenSteamTool.dll
  ├─ dwmapi.dll
  └─ xinput1_4.dll
```

把三个 DLL 复制到 **Steam 根目录**（右键 Steam 快捷方式 → 打开文件所在的位置）。

### 装进本项目（可选）

```powershell
.\build_ost_dll.bat --install
```

会复制到 `resources/fallback_dlls/` 并写入 `version.txt` 标记为
`v1.1.0-local-netpacket-fix`。

---

## 8. 还需要单独处理：签名文件

**这是最容易被忽略的一点。** 崩溃修好之后，如果不补签名文件，
你只会看到「修好了不崩了，但游戏还是不出现在库里」。

DLL 依赖按 DLL 的 SHA256 命名的签名文件：
`Steam/opensteamtool/pattern/steamclient/<sha256>.toml`、`pattern/steamui/<sha256>.toml`、
`ipc/steamclient/<sha256>.toml`。

针对当前 Build 1791918634 需要的哈希：

| DLL | SHA256 |
|---|---|
| `steamclient64.dll` | `e9045710ec7ee47d7b7488d1c5dda2d933dec7e251e965967a5c48f4df68eb4e` |
| `steamui.dll` | `1b9457943205dc3af0bd53463ba331cbd2a381388699705c02271aac7a2b1252` |

### 现状（截至 2026-10-16 实测）

* 官方 `OpenSteam001/steam-monitor` 的 `pattern` 分支最后更新停在 2026-09-06，
  `ipc` 分支停在 2026-09-26，**上述哈希的 raw URL 返回 404，官方还没补**。
* 本项目已有 `core/steam_pattern_sync.py`，会自动从 steam-monitor 拉签名，
  拉不到会报「缺少 Steam 签名文件」。**在官方补齐之前，这个步骤会失败。**

### 临时方案（按可信度排序）

1. **等官方补齐**（最稳）。官方 release 仍是 1.4.8（2026-06-13），
   PR #212 至今未合并，暂无明确时间表。
2. **从 [issue #208](https://github.com/OpenSteam001/OpenSteamTool/issues/208) 取社区 TOML**。
   `@matheeshapathirana` 给了完整的 24 条 steamclient + 9 条 steamui + IPC 文件，
   并说明如何生成、每条 sig 都对照过自己的 DLL 字节。
   **注意**：加到官方 1.4.8 DLL 上会崩（因为布局变了），**必须配合本补丁编译的 DLL**。
3. `RecvPkt` 那条 sig 要单独加，且 RVA 有分歧（`0x5BC610` vs `0x5BC990`），
   加之前最好按自己 DLL 的字节核对。

> ⚠️ 社区 TOML 是**未签名的第三方数据**，加载进 Steam 进程。
> 请自行核对条目，不要盲目信任。

---

## 9. 验证清单

编译部署后按顺序确认：

- [ ] Steam 启动时**不再**报 `signature file not found` / `IPC spec file missing`
- [ ] Steam **不再崩溃**（之前登录阶段会连崩三次）
- [ ] 已入库的游戏出现在库里
- [ ] 能新增一个 AppID 并正常下载
- [ ] 存档备份 / WebDAV / Denuvo 页等依赖其他 hook 的功能仍正常
- [ ] 云存档、在线状态（PR 作者未测试，建议重点回归）

如需回滚：删掉 Steam 根目录下自己放的三个 DLL 即可，
Steam 会回到未注入状态，不影响游戏本体的购买与下载。

---

## 10. 上游进展跟踪

| 项目 | 状态（2026-10-16） |
|---|---|
| PR [#212](https://github.com/OpenSteam001/OpenSteamTool/pull/212) | **open，未合并** |
| main 分支最后提交 | `2a08b0bf`（2026-07-06） |
| 最新 release | 1.4.8（2026-06-13） |
| steam-monitor `pattern` 分支 | 停在 2026-09-06 |
| steam-monitor `ipc` 分支 | 停在 2026-09-26 |

建议定期复查。上游一旦发新版（含此修复），可以删掉本地补丁，
直接用官方 DLL —— 那时本项目只需把 `resources/fallback_dlls/` 里的
三个文件替换成官方版本即可。

### 如何验证官方版是否已修复

拿到新版 DLL 后，用二进制搜索确认 `RecvPkt` 是否带布局判别逻辑：

```powershell
# 检查是否含 PacketFields（补丁引入的函数）
Select-String -Path .\resources\fallback_dlls\OpenSteamTool.dll -Pattern 'PacketFields' -Encoding Default
```

- **搜到** → 官方版已含此修复，直接用。
- **搜不到** → 官方版仍是旧偏移，遇到 Build 1791918634 及以后的版本仍会崩。

更可靠的间接判断：`RecvPkt` 崩溃时是否还产生 minidump。
官方修复前，Steam 登录阶段会连崩三次。

---

## 附：为什么「只换注入文件」是安全的

本项目的注入/移除机制本来就是**文件搬运**，不涉及任何配置解析：

```python
# core/dll_injector.py
ALL_DLLS = ("OpenSteamTool.dll", "dwmapi.dll", "xinput1_4.dll")

deploy_dlls():    shutil.copy2(源目录/xxx.dll → Steam根目录/xxx.dll)
uninstall_dlls(): os.remove(Steam根目录/xxx.dll)
```

所以未来替换成任何版本的 DLL，用户操作流程完全不变。

⚠️ **但有一个坑要注意**：本机存在

```
~/.OpenSteamToolDesktop/DLL/v1.4.8/   ← DLLManager 管理的版本
~/.OpenSteamToolDesktop/DLL/current_version.json
```

而 `core/steam_bridge.py` 的来源优先级是：

1. `DLLManager` 管理的路径 ← **会赢**
2. `resources/fallback_dlls/`（fallback）

也就是说，**仅把新 DLL 放进 `resources/fallback_dlls/` 可能不生效**，
因为 `dll_manager.check_for_updates()` 会检测到"官方有新版本"并下载覆盖。

若将来要启用自编译版，需要先处理这个优先级问题（例如删除
`~/.OpenSteamToolDesktop/DLL/` 下的版本目录，或加一个"优先本地"的开关）。
本次存档不做此改动。

---

## 附：参考

- 根因分析（含 minidump 反汇编）：[#209](https://github.com/OpenSteam001/OpenSteamTool/issues/209)
- 补丁 PR（含偏移对照表与测试记录）：[#212](https://github.com/OpenSteam001/OpenSteamTool/pull/212)
- 社区 TOML 全文：[#208](https://github.com/OpenSteam001/OpenSteamTool/issues/208)
- 现象上报：[#213](https://github.com/OpenSteam001/OpenSteamTool/issues/213) / [#214](https://github.com/OpenSteam001/OpenSteamTool/issues/214)
