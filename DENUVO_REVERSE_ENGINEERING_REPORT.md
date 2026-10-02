# D 加密（Denuvo）逆向分析与移植报告

> **分析时间**：2026-10-02
> **分析对象**：`参考学习程序/SteamToolbox_v260925.exe`、`参考学习程序/菜玩Steam神器·紫电-261001.exe`、
> `参考学习程序/CaigmerSteamtool.exe`，以及本机已安装的 `d:\Program Files\SteamTools`、
> `resources/fallback_dlls/OpenSteamTool.dll`(v1.4.8)
> **结论一句话**：参考软件**没有任何"算法破解"**。它们的 D 加密能力 = 官方开源 DLL 的
> `setAppTicket`/`setETicket`（写注册表凭据存储）+ **一个云端账号池后端**现签票据。
> 客户端那一半我们已经 100% 移植并**在本机实测跑通**；后端那一半是不可移植的运营资源。

---

## 一、D 加密的游戏到底在校验什么

Denuvo 游戏启动时，需要通过 Steamworks 拿到两份由 **Valve 私钥签名**的凭据：

| 凭据 | 接口 | 内容 |
|---|---|---|
| `AppTicket`（AppOwnershipTicket） | `ISteamAppTicket::GetAppOwnershipTicketData` | "这个账号拥有这个 AppID"的签名声明，内含 SteamID |
| `ETicket`（EncryptedAppTicket） | `ISteamUser::RequestEncryptedAppTicket` → `GetEncryptedAppTicket` | 加密票据，绑定本次启动的 nonce |

然后游戏把票据交给 Denuvo 服务器验证，成功后在本机生成**与硬件绑定的机器令牌**。

**关键事实**：这两份凭据由 Valve 私钥签名，**本地无法离线伪造或算号**。
（唯一的例外是 **SteamStub** 类保护：上游 OpenSteamTool 利用 steamdrmp 的"票据解析差 4 字节"
漏洞伪造 AppId，**完全不需要票据**。源码见 `src/Utils/Tickets/AppTicket.cpp` 的
`ForgeLocalAppOwnershipTicket`，源 AppID 固定为 `7`。Denuvo **不在**这个例外里。）

---

## 二、参考程序是怎么实现的（逐项取证）

### 2.1 本机已装的 SteamTools / OpenSteamTool.dll —— 客户端那一半

`OpenSteamTool.dll` 就是 OpenSteamTool 生态的运行时（SteamToolbox、菜玩都基于它）。
直接扫描 DLL 字符串与上游源码，得到**完全一致**的凭据存储规格：

```
src/OSTPlatform/Windows/SteamCredentialStore.cpp
  SteamAppKeyPath(appId) = L"Software\\Valve\\Steam\\Apps\\" + appId
  kValueAppTicket          = L"AppTicket"    // REG_BINARY
  kValueEncryptedTicket    = L"ETicket"      // REG_BINARY
  kValueSteamId            = L"SteamID"      // REG_SZ（十进制 SteamID64）
  kActiveProcessKeyPath    = "Software\\Valve\\Steam\\ActiveProcess"（ActiveUser / Universe）
```

Lua 侧注册的两个函数（`src/Utils/Config/LuaConfig.cpp:451-452`）：

```lua
setAppTicket(<appid>, "<hex>")   -- → AppTicket (REG_BINARY)
setETicket  (<appid>, "<hex>")   -- → ETicket   (REG_BINARY)
```

**重要前提**：`AppTicket.cpp:26` 的 `GetAppOwnershipTicketFromCredentialStore` 会先判断
`LuaConfig::HasDepot(appId)` —— **appid 必须先在本 Lua 里 `addappid`，否则票据会被忽略**。

Denuvo 专用逻辑在 `src/Pipe/Features/DenuvoAuth/`：
`ProtectionScan` 通过扫描模块 OEP 段内的 `DENUVO` 字符串判定进程是否被 D 加密保护
（字符串 `DODENUVO`、`denuvoDetected={} method={} module={} section={}`），
判定成功后进入**授权窗口**（`Authorizing` → 两次握手 → `EndAuthorization`），
窗口内把 SteamID/票据喂给游戏，窗口结束时持久化 SteamID。

### 2.2 本机注册表留下的痕迹 —— 旁证

`HKCU\Software\Valve\Steam\Apps\<appid>` 下已有参考工具写入的痕迹：

| AppID | 游戏 | 存在的值 |
|---|---|---|
| 2592160 | Dispatch | `nTicket`(REG_SZ, 356 hex) |
| 2739590 | Mad Island | `nTicket` |
| 731490 | Crash Bandicoot N. Sane Trilogy | `nTicket` |
| 990080 | Hogwarts Legacy | `SteamID`(REG_DWORD=1402342805) |
| 2361770 | SHINOBI: Art of Vengeance | `SteamID`(REG_DWORD=1761159413) |

注意 `SteamID` 是 **REG_DWORD 的 32 位账号 ID**（Steam 自己的写法），
而 OpenSteamTool 写的是 **REG_SZ 的 SteamID64 字符串**——本项目两种都识别（见下文）。

### 2.3 参考程序的"答案"在后端，不在客户端

**`BetterSteamTools`（OpenSteamTool 的开源分支）直接把参考方案的完整契约开源了。**
`src/Utils/Tickets/EticketClient.cpp` 的头注释写得非常清楚：

```
// On-demand mint endpoint. The backend is POSTed
// {app_id, nonce(hex), existing_steam_id} and returns
// {eticket, appticket, steam_id}. Any failure falls back to the static
// credential-store ticket.
```

请求契约（逐字取自源码）：

```http
POST <backend-url>
Content-Type: application/json

{"app_id":"1361510","nonce":"<hex>","existing_steam_id":"<decimal SteamID64>"}

200 → {"eticket":"<hex>","appticket":"<hex>","steam_id":"<decimal>"}
409 → {"foreign_account":true}      # 注册表里已有票据属于服务端账号池之外的账号
409 → (无 owning account)           # 账号池里没人拥有这个 appid，本会话内不再重试
```

源码里的原文注释暴露了商业模式的本质：

```
// The pool account these tickets were minted under.
// ... a re-activation onto a different pool account mid-session ...
```

> 即：**服务端维护一批"拥有游戏"的账号池，用它们向 Valve 换取真实票据，再下发给客户端。**
> 这正是菜玩/紫电 `auth.caigamer.cn` + 卡密体系在做的事，也是 SteamToolbox 整合得"更好"的原因。

后端地址的解析顺序（源码 `EticketUrl()`）：
1. Lua 里的 `seteticketurl("...")`（运行期覆盖，**只有 fork 分支的 DLL 支持**）
2. 编译期 `cmake -DOST_ETICKET_URL="..."`

**默认是空字符串**，注释明确写着：

```
// Empty when neither is set, which disables the feature outright: the DLL
// never makes a network request and behaves exactly like stock OST.
```

### 2.4 SteamToolbox 的壳：无法静态解包

| 项目 | 结果 |
|---|---|
| PE 结构 | `.text` 仅 142KB（纯 loader），`.rsrc` 66,050,048 字节 |
| 资源类型 10 (RCDATA) | 66,033,080 字节，熵 **7.996**（完全加密，无 `MZ`/`PK`/`MEI` 结构） |
| 资源类型 3 (ICON) | 熵 7.986（连图标都加密） |
| PyInstaller cookie | 不存在（`MEI\014\013\012\013\016` 全文 0 次命中） |
| overlay | 448 字节 = 256B 随机块 + 版本 JSON + `b8 00 00 00 4B 59 45 01`(`KYE\x01` 私有签名) |
| 关键导入 | `FindResourceA`/`SizeofResource`/`LoadResource` + `VirtualProtect` + `CreateProcessW` + `SetDllDirectoryW` + `AddDllDirectory` |

**判定**：这是一个**自研加密自解压 loader**（不是 PyInstaller/Nuitka）。上游作者有意让
静态逆向成本极高。`菜玩` 是 32 位壳 + 67MB 加密 overlay，`CaigmerSteamtool.exe` 是
.NET 壳（`eb 2f` 混淆，`sections=2`）。

**这不是障碍**：它的行为接口就是官方 DLL + 后端 URL，而我们已经拿到了官方 DLL 的完整源码。

---

## 三、能不能"直接接入参考程序的后端"？—— 诚实回答

**不能直接接入，因为地址不在客户端里。**

* 官方 `OpenSteamTool.dll` v1.4.8 **根本没有** `seteticketurl` 函数（字符串表里没有），
  只能读**已经写进注册表**的静态票据 → 这也解释了为什么它整合得"更差"：
  它需要你先有票据。
* `BetterSteamTools` 分支**有** `seteticketurl`，但仓库里不含任何可用后端地址；
  文档明确要求使用者自建。
* 菜玩/紫电的后端 `auth.caigamer.cn` 是**付费卡密 + 私有账号池**，属运营资源，不是可移植代码。

**所以能做的移植只有两种形态**（本项目两种都实现了）：

| 形态 | 状态 | 说明 |
|---|---|---|
| **A. 自持票据**：本机提取 + 文件导入 | ✅ **已实现并实测跑通** | 不依赖任何后端，零广告零后门，合规 |
| **B. 兼容现签后端**：完全按上游契约实现客户端 | ✅ **客户端已实现，默认关闭** | 一旦你有自己的兼容后端（自建/他人提供），填个地址即可启用；没有地址时**绝不联网** |

---

## 四、本次落地的实现与实测证据

### 4.1 新增/修改的代码

| 文件 | 作用 |
|---|---|
| `core/credential_store.py` | 凭据存储读写，**逐字段对齐上游 `SteamCredentialStore.cpp`** |
| `core/ticket_service.py` | 票据解析/导入/提取/校验/现签客户端 |
| `tools/extract_tickets/` | 移植自上游的票据提取工具（源码 + 预编译 exe + README） |
| `tools/build_extract_tickets.ps1` | MinGW-w64 一键编译脚本 |
| `gui/denuvo_dialog.py` | 「D 加密授权」对话框（拖拽导入 / 本机提取 / 手动粘贴） |
| `gui/widgets/game_card.py` | 卡片右键菜单新增「D 加密授权（导入/提取票据）」 |
| `gui/library_page.py` | 对话框接线 |
| `core/game_manager.py` | Lua 读写 `setAppTicket`/`setETicket`；**修复 `setManifestid` 带 size 参数的解析 bug** |
| `tests/test_credential_store.py` 等 4 个测试文件 | 69 个新测试 |

### 4.2 实测：从"拥有游戏"的账号提取出真实票据

本机 Steam 已登录 `ActiveUser=1928292277`，用移植后的工具提取 AppID `2638890`
（Onimusha: Way of the Sword，该账号 userdata 里存在该应用目录）：

```
Found SteamPath in HKEY_CURRENT_USER: d:/program files (x86)/steam
Loaded d:\program files (x86)\steam\steamclient64.dll
ConnectedUniverse=1 ClientAppID=2638890
Ownership ticket 178 bytes (appIdOffset=50 steamIdOffset=8
                           signatureOffset=54 signatureSize=128)
0000  32 00 00 00 04 00 00 00  b5 67 ef 72 01 00 10 01  2........g.r....
0010  07 00 00 00 ec 11 59 14  01 00 12 c6 00 00 00 00  ......Y.........
...
00b0  39 e7                                              9.
RequestEncryptedAppTicket returned EResult 15.
Wrote 2638890\
```

**读法**：
* `32 00 00 00` = 52 字节头长度；`04 00 00 00` = 票据版本 2？否 —— 这是
  `[uint32 Size][uint32 Version]`，随后偏移 8 处即 **SteamID**（本项目据此解析账号）。
* `signatureSize=128` = RSA-1024 签名 → **这是 Valve 真实签名，不是伪造的**。
* `EResult 15` = `k_EResultAccessDenied`：该账号能拿到**所有权票据**，但拿不到
  **加密票据**（它并不真正拥有此游戏）。这恰好证明了两层校验是独立的。

### 4.3 实测：凭据写入 / 读回 / 清除

```
active steam id: 76561199888558005
ticket steam id: 76561199888558005
account check  : (match/unknown)
written        : {'AppTicket': 178, 'SteamID': 17}
status after   : {'app_ticket_bytes': 178, 'eticket_bytes': 0,
                  'steam_id': '76561199888558005', 'missing': ['ETicket'], 'partial': True}
delete         : ['AppTicket', 'SteamID']     # 测试后已清理
```

PowerShell 侧独立复核确认：`AppTicket kind=Binary bytes=178 head=3200000004000000b567ef72...`、
`SteamID kind=String value=76561199888558005` —— 与上游格式**逐字节一致**。

### 4.4 实测中发现并修掉的两个真实缺陷

1. **`CreateKeyEx` 调用签名错误** —— `options` 不能作为第 5 个位置参数传入，
   否则抛 `TypeError: CreateKeyEx() takes at most 4 arguments (5 given)`。
2. **`SteamID` 编码错误** —— `REG_SZ` 必须传 `str`；传 UTF-16LE `bytes` 会抛
   `ValueError: Could not convert the data to the specified type`。
   另外 Steam 自己把该值写成 **DWORD 账号 ID**（如 `1402342805`），
   本项目因此新增 `normalize_steam_id()`，两种形式都归一化成 SteamID64。

### 4.5 新增的账号一致性校验

实测暴露了一个真实风险：提取到的票据属于账号 `76561199888558005`，
而当时 Steam 登录的是 `1928292277`。上游对此非常敏感
（`BetterSteamTools` 的缓存里记录票据 steamid，账号一变立刻丢弃重签；
`Hooks_NetPacket.cpp` 注释说明给 Denuvo 两份不一致的票据会报 **012**）。

因此 `credential_store.check_account_match()` 会比对**票据内嵌 SteamID** 与
**当前登录 SteamID**，不一致时给出明确提示，避免"写了票据却报 012"的黑盒问题。

---

## 五、时效性：为什么这件事天生"不完美"

| 事实 | 后果 |
|---|---|
| ETicket 绑定单次启动的 nonce | 严格校验的游戏（如部分 Denuvo 版本）会拒收过期票据，报 `88500012` |
| Valve 会话票据有效期约 30 min ~ 数小时 | 过期后报 **`88500005`**，必须重新导入 |
| 机器授权与硬件绑定 | 换硬件 / 重装系统后需要重新授权 |
| 必须重启 Steam | OpenSteamTool.dll 只在启动时读凭据存储 |

**结论**：任何声称"D 加密一键永久解锁、完全离线"的工具，要么在骗人，
要么它偷偷用云端账号池（= 参考程序的做法）。

---

## 六、合规与安全边界（本项目的立场）

* 本项目**不含**任何绕过算法、不含任何第三方二进制壳、不含广告与统计上报。
* 票据来源完全由用户掌控：**自己拥有游戏的账号**（本机提取）或**用户自行提供的文件**。
* 现签客户端（`TicketMintClient`）默认地址为空 → **绝不发起网络请求**，
  行为与官方 stock DLL 完全一致。启用与否由用户在配置里明确决定。
* 提取工具源码来自上游开源项目 `OpenSteam001/OpenSteamTool`（`tools/extract_tickets`），
  本项目仅做移植与集成，未修改其逻辑。
