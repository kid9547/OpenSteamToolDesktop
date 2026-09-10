# Steam 下载“网络错误”排查与修正流程

> 目标：在另一台 Windows 电脑上修正 OpenSteamToolDesktop，使导入 Lua 后的 Steam 下载链路能够被明确诊断。本文以 AppID `1623730`（Palworld）为复现样本。

## 1. 结论

当前项目存在项目侧问题，但“Steam 下载网络错误”不能只归因于一个原因。此次本机实测已经把 Steam 的泛化提示还原成了更具体的错误：

```text
AppID 1623730 ... Failed to get manifest request code, 'Access Denied'
AppID 1623730 update canceled : Failed downloading 1 manifests (No connection)
```

同一时间，Steam 对共享 Depot `228989`、`228990` 使用 `cache10-tyo3.steamcontent.com` 返回了 `200 OK` 并正常下载。因此本次不是整机 Steam CDN 不通，而是 Palworld 主 Depot 的 Manifest 请求被拒绝；“No connection”只是 Steam UI 对该失败的泛化显示。

1. **Manifest 下载器没有接入入库流程。**  
   `core/manifest_downloader.py` 实现了 CDN 获取和写入 `depotcache`，但 `gui/search_page.py` 的 `_do_fetch_metadata()` 只调用 `MetadataFetcher.fetch_all()` 和 `add_game_with_metadata()`，没有导入或调用 `ManifestDownloader`。因此日志中的 `Lua + Manifest ready` 是误导性的，实际上只代表 Lua 写入完成。
2. **生成的 Lua 没有写入 `setManifestid`。**  
   `core/game_manager.py` 当前生成 `addappid`、Depot key 和 token，但明确不生成 `setManifestid`，依赖 DLL 运行时自动获取 Manifest。这个设计依赖 OpenSteamTool 的上游 Manifest API；上游 API、Steam 版本匹配文件或网络代理任一环节失败，都可能最终表现为 Steam 的“网络错误”。
3. **不同 HTTP 链路的代理行为不一致。**  
   `utils/http_client.py` 会读取 Windows 系统代理，但 `MetadataFetcher` 和 `ManifestDownloader` 直接创建 `httpx.Client`，没有传入代理。另一台电脑如果必须通过系统代理访问 Steam、GitHub 或 CDN，项目会出现“部分 API 可用、下载仍失败”的情况。
4. **`SSL_VERIFY=False` 不是网络修复。**  
   它只能绕过证书校验，不能解决 DNS、代理、连接超时、被防火墙拦截或上游服务不可达。长期建议恢复证书校验，并针对具体证书问题处理根证书或代理配置。
5. **当前已生成的 Palworld Lua 含有非法条目。**  
   SteamCMD 返回的 `depots` 对象同时包含 `branches`、`baselanguages` 等应用元数据；旧代码没有限制 Depot ID 必须为数字，实际生成了 `addappid(branches)`。该条目不是有效 AppID/Depot，必须删除并重新生成 Lua。

## 2. 已核对的代码证据

| 位置 | 现象 | 影响 |
|---|---|---|
| `gui/search_page.py:803-819` | 后台流程获取元数据后直接写 Lua，没有 `ManifestDownloader` | 项目自带 Manifest 下载器处于未使用状态 |
| `gui/search_page.py:831` | 输出 `Lua + Manifest ready` | 成功提示与实际状态不一致 |
| `core/manifest_downloader.py:114-214` | 有批量下载、重试和写入 `depotcache` 的实现 | 具备接入基础，但当前没有调用方 |
| `core/game_manager.py:498-574` | Lua 生成逻辑只生成 AppID、Depot key、token、DLC | 没有固定 Manifest 的 Lua 配置 |
| `core/metadata_fetcher.py:65-75` | 创建独立 `httpx.Client` | 不复用统一 HTTP 客户端的系统代理逻辑 |
| `core/manifest_downloader.py:93-104` | 创建独立 `httpx.Client` | Manifest CDN 请求同样不读取系统代理 |
| `utils/http_client.py:32-37, 168-174` | 只有统一 HTTP 客户端读取 `urllib.request.getproxies()` | 代理配置没有覆盖全部请求链路 |
| `config.py:68` | `SSL_VERIFY = False` | 隐藏证书问题，不能证明网络链路正常 |
| `core/metadata_fetcher.py:700-706` | 旧版本未过滤非数字 Depot ID | 生成 `addappid(branches)` 等非法 Lua |
| `core/manifest_downloader.py:382-396` | 旧版本只接受 `weighted_load == 130` | 当前 API 返回 160+ 时会错误降级到过期 CDN 列表 |

## 3. AppID 1623730 的核对结果

通过 `https://api.steamcmd.net/v1/info/1623730` 获取到：

- AppID：`1623730`
- 名称：`Palworld`
- Windows 主 Depot：`1623731`
- public 分支 Manifest GID：`868868087024202254`
- 下载大小字段：`35943136912`（约 33.4 GiB，十进制）
- 另有 `228989`、`228990` 两个共享安装 Depot，不能当作游戏主 Depot 下载。

这说明该样本至少存在有效的 Depot/Manifest 元数据；排查重点应放在：

1. Lua 是否被 DLL 实际加载；
2. Manifest 请求是否能访问上游；
3. Depot key、Access Token 与账号权限是否满足；
4. Steam/CDN/系统代理是否允许对应 HTTPS 请求。

## 4. 另一台电脑上的修正顺序

### 4.1 先确认不是 Steam 自身网络问题

在目标电脑 PowerShell 执行：

```powershell
Resolve-DnsName api.steampowered.com
Resolve-DnsName store.steampowered.com
Resolve-DnsName raw.githubusercontent.com
Test-NetConnection api.steampowered.com -Port 443
Test-NetConnection store.steampowered.com -Port 443
Test-NetConnection raw.githubusercontent.com -Port 443
curl.exe -I https://api.steampowered.com
curl.exe -I https://raw.githubusercontent.com
```

若 DNS 或 443 连接失败，先修复网络、代理、防火墙或 DNS；不要先改 Lua。若必须使用代理，记录代理地址，并确认 Steam 客户端和 Python/httpx 使用的是同一条出口。

### 4.2 检查注入和 Lua 实际路径

确认以下文件位于**同一个 Steam 根目录**：

```text
<Steam>\OpenSteamTool.dll
<Steam>\dwmapi.dll
<Steam>\xinput1_4.dll
<Steam>\config\lua\1623730.lua
```

关闭 Steam 后重新启动，再检查：

```text
<Steam>\opensteamtool\main.log
<Steam>\opensteamtool\manifest.log
<Steam>\opensteamtool\package.log
```

注意：开发版可能还有其他日志文件，以上文件不存在时应先确认 DLL 是否加载，而不是直接判断为 CDN 错误。

### 4.3 检查 Lua 内容

对 AppID `1623730`，至少应确认主 AppID、主 Depot 和所需密钥配置没有被截断或写入错误。不要把真实 token、Depot key、AppTicket 或 ETicket 上传到 Issue、报告或公共仓库。

如果采用固定 Manifest 方案，Lua 应按上游 OpenSteamTool 约定使用：

```lua
addappid(1623730)
addappid(1623731, 0, "<depot-key>")
setManifestid(1623731, "868868087024202254", 35943136912)
```

其中 `<depot-key>` 必须来自合法、可信的来源；示例中的占位符不能直接使用。固定 Manifest 只解决 Manifest 选择/绑定问题，不能替代 Steam/CDN 网络，也不能绕过账号权限。

当前电脑必须先删除旧 Lua 中的这类错误行，再重新生成：

```lua
addappid(branches)
```

修复后的程序已经在 `MetadataFetcher._parse_depots()` 中只接受数字 Depot ID；如果不重新入库，旧文件不会自动改变。可在另一台电脑上关闭 Steam 后删除 `config\lua\1623730.lua`，再用修复后的程序重新入库。

### 4.4 修正项目的 HTTP 代理一致性

建议抽取一个统一的 HTTP Client 工厂，至少让以下模块共享代理、超时、User-Agent 和 TLS 配置：

- `utils/http_client.py`
- `core/metadata_fetcher.py`
- `core/manifest_downloader.py`

修正要求：

1. 从 Windows 系统代理或应用配置读取代理；
2. 显式记录“是否启用代理”和目标主机，不记录 token/key；
3. 连接超时、读取超时、HTTP 状态码分别记录；
4. 不使用无限重试；
5. 默认恢复 `SSL_VERIFY=True`，只有在用户明确配置时才允许关闭。

### 4.5 正确接入 Manifest 下载器

在 `gui/search_page.py` 的后台元数据流程中：

1. `fetch_all(app_id)` 成功后收集 `metadata.depots` 中有 `manifest_gid` 的条目；
2. 将 `(depot_id, manifest_gid, size)` 传给 `ManifestDownloader(steam_path).download_manifests(...)`；
3. 合并主游戏 Depot 和 DLC Depot，避免只下载主 Depot；
4. 关闭 downloader；
5. 只有在下载结果 `failed == 0` 或明确允许 DLL 自动下载时，才显示 “Manifest ready”；
6. 失败时把失败 Depot、HTTP 状态/异常类型和建议动作显示给用户。

重要：这一步不能简单地吞掉异常或继续显示成功。当前代码的 `except Exception` 和静默返回会让用户只看到 Steam 的泛化网络错误。

### 4.6 处理 OpenSteamTool 运行时的上游依赖

上游 OpenSteamTool README 说明：

- 默认通过 `opensteamtool` / `steamrun` / `wudrm` 上游 API 获取 Manifest；
- Steam 启动时还可能访问 `raw.githubusercontent.com`，失败时回退到 jsDelivr 或本地 pattern 缓存；
- 调试日志位于 `<Steam>\opensteamtool\`。

因此另一台电脑必须允许访问至少这些类型的地址：

- `api.steampowered.com`
- `store.steampowered.com`
- Steam CDN 主机
- `raw.githubusercontent.com` 或 jsDelivr
- 项目配置中使用的 token/key/Manifest 服务地址

若公司网络禁止 GitHub，优先按上游文档配置可访问的镜像或准备本地缓存；不要仅靠关闭 SSL 校验。

## 5. 建议的代码改动清单

按优先级执行：

### P0：必须修复

- [ ] 在 `search_page.py` 真正调用 `ManifestDownloader`，或明确移除本地下载器并完全依赖 DLL，不能两者都存在但没有实际调用。
- [ ] 修正成功提示：没有完成 Manifest 处理时不能输出 `Manifest ready`。
- [ ] 统一 `MetadataFetcher`、`ManifestDownloader` 与 `utils.http_client` 的代理行为。
- [ ] 增加失败结果展示，至少包含失败 Depot ID 和失败阶段。
- [x] 过滤 SteamCMD 返回的非数字元数据字段，避免生成 `addappid(branches)`。
- [x] 放宽 CDN 目录筛选，接受动态 `weighted_load`，避免使用过期 fallback 域名。
- [x] 入库完成后提示缺少 Access Token，避免把“Lua 写入成功”误报成“下载授权已完成”。

### P1：强烈建议

- [ ] 为 `ManifestDownloader` 添加单元测试：成功下载、404、超时、全部 CDN 失败、ZIP payload 提取、目标文件写入。
- [ ] 为 AppID `1623730` 添加脱敏的集成测试输入，验证 `1623731` 的 GID、size 和 Lua 生成结果。
- [ ] 将 CDN 列表和 Manifest API 设为可配置，不要把可能过期的域名永久写死。
- [ ] 默认启用 TLS 证书校验。
- [ ] 增加“网络诊断”按钮，分别测试 Steam API、GitHub/jsDelivr、Manifest 服务和 CDN。

## 6. 验证闭环

修正后必须按以下顺序验证：

1. **单元测试**

   ```powershell
   python -m pytest tests\test_http_client.py tests\test_game_manager.py -q
   ```

2. **AppID 元数据验证**

   确认日志中出现 `1623731`、`868868087024202254`，并且 size 为 `35943136912` 或服务端返回的最新值。

3. **Manifest 文件验证**

   ```powershell
   Test-Path "<Steam>\depotcache\1623731_868868087024202254.manifest"
   Get-Item "<Steam>\depotcache\1623731_868868087024202254.manifest" |
     Select-Object FullName,Length
   ```

   文件必须存在且长度大于 0。

4. **Steam 下载验证**

   关闭并重新启动 Steam，开始下载 AppID `1623730`，记录 Steam 下载页面和 `<Steam>\opensteamtool\manifest.log` 的时间点。

5. **失败分类**

   - DNS/连接超时：网络、代理或防火墙；
   - HTTP 403/401：账号权限、token/key 或服务端策略；
   - HTTP 404：Manifest GID、Depot、分支或 CDN URL 过期；
   - ZIP/格式错误：Manifest 下载协议或响应内容处理错误；
   - Lua 未加载：DLL 部署路径、架构、Steam 重启或安全软件拦截；
   - pattern 获取失败：Steam 版本兼容文件上游不可达或未命中缓存。
   - `Failed to get manifest request code, 'Access Denied'`：Manifest 请求已经到达 Steam/上游，但当前账号、App Access Token、Depot key 或上游授权条件不满足；这不是普通 DNS/下载速率问题。需要检查 Token API 是否为该 AppID 提供 token，以及 OpenSteamTool DLL 的 Manifest API 配置/版本。

## 7. 当前环境验证边界

本次检查已在用户本机读取 Steam `content_log.txt` 并确认：共享 Depot 下载正常，Palworld 主 Depot 在获取 Manifest request code 时收到 `Access Denied`；同时读取到项目实际写出的 `addappid(branches)`。代码已修复两个确定性问题，但当前项目仍没有在本轮中实现 DLL 内部 Manifest API 的替代授权，因此如果重新生成干净 Lua 后仍出现同一条 `Access Denied`，剩余问题属于 OpenSteamTool 运行时授权/Token/账号条件，不应继续按普通网络错误排查。

本机访问项目配置的 Access Token 数据源后，没有查到 AppID `1623730` 的 token；因此当前 Lua 中没有 `addtoken(1623730, "...")`。这与 Steam 日志中的 `Access Denied` 相互吻合。不能凭空生成 token，也不应把其他 AppID 的 token 复用到 Palworld；需要使用合法账号/可信数据源提供的有效授权，或等待/升级支持该 AppID 的 OpenSteamTool 运行时。

## 8. 最新修正：Steam 版本签名文件

OpenSteamTool `1.4.8` 曾报告：

```text
Unsupported Steam Version
signature file not found for steamclient64.dll
Hooks that depend on steamclient64.dll are disabled for this session
```

这不是 Palworld Lua 格式错误。OpenSteamTool 会按当前 `steamclient64.dll` 和 `steamui.dll` 的 SHA-256 查找：

```text
<Steam>\opensteamtool\pattern\steamclient\<sha256>.toml
<Steam>\opensteamtool\pattern\steamui\<sha256>.toml
```

项目现在在 DLL 部署流程中预同步这两个文件，下载顺序为 GitHub Raw、jsDelivr，并复用 Windows 系统代理；写入前会检查 HTTP 200 和非空响应。`verify_injection()` 也不再只检查 DLL 是否存在，而会明确报告当前 hash 对应的签名文件缺失。

当前本机已验证并缓存：

- `steamclient64.dll`：`caba4826aa3501039d095aee1843a6bfb270fb43a3ab4455b2d6733223579fee.toml`
- `steamui.dll`：`cb387adefbbac64a3c1490d4275d00daf3a1e0219b4580726ef7681db7429278.toml`

使用修复版程序时，应先点击 DLL 部署/注入，再完全退出 Steam 并重新启动。若仍出现相同弹窗，检查上述两个文件是否存在且大小大于 0；若签名已存在但 Palworld 仍显示 `Access Denied`，则回到上一节的账号授权、Access Token、Depot key 和 Manifest runtime 诊断，不能通过伪造 token 解决。

最新截图显示的弹窗是另一类文件缺失：

```text
OpenSteamTool: IPC spec file not found.
<Steam>\opensteamtool\ipc\steamclient\<steamclient64.dll sha256>.toml
```

项目现在还会从 `steam-monitor` 的 `ipc` 分支同步该文件，并写入 `opensteamtool\ipc\steamclient\`。这个 IPC spec 与加密 App Ticket/用户接口相关；只同步普通 `pattern` 不足以启用完整 Hook。

如果 IPC spec 已存在，而 Steam 商店仍显示“购买”、下载日志仍为 `Failed to get manifest request code, 'Access Denied'`，这表示当前 Steam 账号没有通过合法授权获得该 App/Depot 的下载票据。程序不能也不会通过伪造 token、AppTicket 或购买状态绕过 Steam 授权；此时只能使用拥有许可的账号、合法的 Depot key/token，或等待运行时/上游服务提供该 App 的授权支持。

## 9. 参考资料

- 项目代码：`gui/search_page.py`、`core/manifest_downloader.py`、`core/metadata_fetcher.py`、`core/game_manager.py`、`utils/http_client.py`
- AppID 元数据：<https://api.steamcmd.net/v1/info/1623730>
- OpenSteamTool 上游 README：<https://github.com/OpenSteam001/OpenSteamTool/blob/main/README.md>
- Steam Store API：<https://store.steampowered.com/api/appdetails?appids=1623730&l=english>

## 10. 对照 OpenSteamTool 上游后的最终结论

上游当前 README 和源码确认：

- 默认 Lua 目录仍然是 `<Steam>\config\lua`，本项目使用的目录没有因 Steam 更新而改变；
- `setManifestid(depot_id, manifest_gid, size)` 仍然是受支持的功能，用于固定 depot 清单；
- Steam 运行时支持 `opensteamtool`、`steamrun`、`wudrm` 三种 Manifest request code 上游；
- Lua 文件修改通过 watcher 热加载，但上游 Issue #102 明确记录：已绑定的 Manifest 在 Steam 已运行时可能不会立即解除或切换，必须完整重启 Steam；
- Issue #169 记录 Steam 1784669098 下的“网络连接问题”可通过删除 `opensteamtool` 目录并重启恢复，说明 stale runtime cache 也可能参与故障；
- Issue #177 记录 Steam 更新后游戏恢复“购买”状态，与当前 `Unsupported Steam Version` / IPC spec 缺失现象一致。

本项目此前生成的 Palworld Lua 没有 `setManifestid`，这是已确认的项目侧缺陷；现在生成器会对所有带 `manifest_gid` 的 depot 写入绑定行，当前本机文件已补充：

```lua
setManifestid(1623731, "868868087024202254", 35943136912)
```

验证时必须**完全退出 Steam（包括后台 Steam 进程）后再启动**，不能只关闭下载窗口或只重新打开库页面。若重启后仍出现：

```text
Failed to get manifest request code, 'Access Denied'
```

则清单路径/版本绑定已经不是主要阻塞点，剩余是 Steam 账号许可、App Access Token、Depot key 或 AppTicket 授权条件。上游 README 也明确要求 AppTicket 从真正拥有目标游戏的账号提取；本项目不会伪造这些授权数据。

## 11. 交给其他 AI 的完整工作记录

本节按实际执行顺序记录本次调试尝试，避免后续重复走已经验证过的方向。

### 11.1 已检查并排除的方向

1. **Steam CDN 并非整体断网。**  
   Steam 日志显示共享 Depot `228989`、`228990` 可以通过 `cache10-tyo3.steamcontent.com` 返回 `200 OK` 并下载。故不能把所有失败归类为 DNS、代理或 CDN 故障。
2. **Lua 目录位置没有因 Steam 更新改变。**  
   OpenSteamTool 当前上游 README 和 `LuaConfig`/`LuaFileWatcher` 源码仍使用 `<Steam>\config\lua`，并且当前机器的文件确实位于：
   `D:\Program Files (x86)\Steam\config\lua\1623730.lua`。
3. **非法 `addappid(branches)` 已确认并修复。**  
   SteamCMD 返回的 `branches`、`baselanguages` 是顶层元数据，不是 Depot。旧解析器把它们写进 Lua；现在 `_parse_depots()` 只接受数字 ID。
4. **CDN `weighted_load == 130` 的旧筛选已修复。**  
   当前 Steam CDN API 返回的有效节点权重已不是固定 130；代码现在接受 API 返回的动态节点，避免错误回退到旧 CDN 列表。
5. **Steam 版本普通 pattern 缺失已修复。**  
   根据 DLL SHA-256 下载并缓存：
   - `opensteamtool\pattern\steamclient\caba4826aa3501039d095aee1843a6bfb270fb43a3ab4455b2d6733223579fee.toml`
   - `opensteamtool\pattern\steamui\cb387adefbbac64a3c1490d4275d00daf3a1e0219b4580726ef7681db7429278.toml`
6. **IPC spec 缺失已修复。**  
   根据同一个 `steamclient64.dll` SHA-256 下载并缓存：
   `opensteamtool\ipc\steamclient\caba4826aa3501039d095aee1843a6bfb270fb43a3ab4455b2d6733223579fee.toml`。
   `SteamPatternSync.missing_patterns()` 在本机返回空列表。

### 11.2 已修改的源码文件

| 文件 | 修改内容 | 结果 |
|---|---|---|
| `core/metadata_fetcher.py` | `_parse_depots()` 跳过非数字字段 | 不再生成 `addappid(branches)` |
| `core/manifest_downloader.py` | 放宽 CDN 动态节点筛选 | 不再因权重变化直接使用过期 fallback |
| `gui/search_page.py` | 入库结果显示 Manifest 数量、Depot key 数量、Access Token 状态；缺 token 时给出授权提示 | 减少“Lua 写入成功=可以下载”的误导 |
| `core/game_manager.py` | 生成带 `manifest_gid` 的 `setManifestid(depot, gid[, size])` | Lua 现在可固定清单版本 |
| `core/steam_pattern_sync.py` | 新增普通 pattern 和 IPC spec 的下载、代理复用、HTTP/空响应检查、缓存检查 | 自动准备 OpenSteamTool 运行时签名 |
| `core/steam_bridge.py` | 部署 DLL 时同步签名；验证时检查签名是否真的存在 | 不再只按 DLL 文件存在误报“注入成功” |
| `config.py` | 新增 `steam-monitor` 的 `pattern`、`ipc` Raw/jsDelivr URL 模板 | 支持上游签名来源 |
| `tests/test_game_manager.py` | 增加非数字 Depot 字段过滤测试和 `setManifestid` 生成测试 | 相关测试通过 |
| `tests/test_constants.py` | 增加签名 URL 模板测试 | URL 配置有基本覆盖 |
| `STEAM_DOWNLOAD_NETWORK_ERROR_REPORT.md` | 持续补充日志证据、上游对照、验证流程和当前边界 | 本文档 |

### 11.3 已生成的当前 Lua

当前机器的 Palworld 文件为：

```lua
-- Palworld (由 OpenSteamToolDesktop 管理)
addappid(1623730, 0, "<app-level-key>")
addappid(1623731, 0, "<depot-key>")
addappid(228989, 0, "<depot-key>")
addappid(228990, 0, "<depot-key>")
setManifestid(1623731, "868868087024202254", 35943136912)
addappid(2771110)
addappid(2771111, 0, "<depot-key>")
addappid(2771112, 0, "<depot-key>")
```

文档中使用占位符是为了避免泄露真实 key；本机文件中曾包含真实 key，不应上传到公共 Issue、仓库或发送给其他 AI。

### 11.4 验证结果

- `tests\test_game_manager.py`：`26 passed`
- 修改模块 `py_compile`：通过
- OpenSteamToolDesktop PyInstaller 构建：通过
- 最新 EXE 启动冒烟：进程正常存活
- 最新交付包：
  - EXE：`E:\toolbox\OpenSteamToolDesktop-1.0.3\dist\OpenSteamToolDesktop\OpenSteamToolDesktop.exe`
  - ZIP：`E:\toolbox\OpenSteamToolDesktop-1.0.3\OpenSteamToolDesktop-1.0.3-fixed.zip`

此前一次更宽的测试运行中，`tests\test_dll_injector.py` 有 3 个旧的 `verify_injection()` 语义用例失败；这些失败与本次 `setManifestid` 和 pattern/IPC 下载逻辑无关，未作为本次修复的证据。

### 11.5 仍未解决的实际症状

即使签名和 IPC 文件已存在，Steam 最新日志仍反复出现：

```text
AppID 1623730 ... Failed to get manifest request code, 'Access Denied'
AppID 1623730 update canceled : Failed downloading 1 manifests (No connection)
```

这两行发生在请求 `Depot 1623731 / Manifest 868868087024202254` 时。`No connection` 是 Steam 更新器对前一个 Manifest request code 失败的泛化状态，不等价于系统完全断网。

同时，Steam 界面一度显示“购买”，随后又出现可下载但网络错误。这与 OpenSteamTool 上游 Issue #177 中 Steam 更新后恢复“购买”状态、Issue #169 中删除 `opensteamtool` 目录后暂时恢复下载的现象相似；它提示运行时缓存/Hook 状态和授权票据都可能参与，但不能仅凭 UI 判断根因。

### 11.6 其他 AI 的推荐验证顺序

1. 完全退出 Steam，确认后台没有 `steam.exe`、`steamwebhelper.exe`、`OpenSteamToolDesktop.exe` 锁定旧文件。
2. 备份后检查并清理旧的 `<Steam>\opensteamtool\` 运行时缓存；不要删除 `config\lua\1623730.lua`，先确认它包含 `setManifestid`。
3. 使用最新 ZIP 部署 DLL，重新执行签名同步。
4. 确认以下三类文件都存在且长度大于 0：
   - `opensteamtool\pattern\steamclient\<sha>.toml`
   - `opensteamtool\pattern\steamui\<sha>.toml`
   - `opensteamtool\ipc\steamclient\<sha>.toml`
5. 完全重启 Steam 后，先观察 `opensteamtool\main.log`、`ipc.log`、`manifest.log`、`pics.log`、`package.log`，再尝试下载。
6. 如果不再出现 `Unsupported Steam Version` 或 `IPC spec file not found`，但仍出现 `Access Denied`，检查：
   - 当前 Steam 账号是否真实拥有 AppID `1623730`；
   - 是否有合法的 App Access Token；
   - Depot key 是否对应当前 Depot；
   - 是否需要由真实拥有游戏的账号提取 AppTicket/ETicket；
   - `opensteamtool.toml` 的 `[manifest] url` 是否能访问。上游支持 `opensteamtool`、`wudrm`、`steamrun`，但切换上游不能替代 Steam 账号授权。
7. 只有当 `content_log.txt` 不再出现 `Failed to get manifest request code, 'Access Denied'`，并出现实际 Manifest 获取/下载记录时，才算验证成功。

### 11.7 明确不能做的“修复”

- 不能伪造或随机生成 App Access Token。
- 不能把其他 AppID 的 token、Depot key、AppTicket 复制给 `1623730`。
- 不能通过修改 Lua 把 Steam UI 的“购买”状态强行改成合法拥有。
- 不能把共享 Depot 下载成功当成主 Depot 已获授权。
- 不能仅凭 EXE 构建成功或 Steam 库中出现游戏，就宣布下载链路已修复。

### 11.8 当前最终判断

本项目侧已确认并修复的确定性缺陷包括：非法 Depot 元数据进入 Lua、CDN 节点筛选过时、普通 pattern/IPC spec 缺失时的误报、以及没有生成 `setManifestid`。但在这些修复完成后，当前机器对 Palworld 主 Depot 的失败仍是 Steam 返回的 `Access Denied`。因此交给其他 AI 时，应优先读取 OpenSteamTool 的 `manifest.log`、`pics.log`、`ipc.log` 和 Steam `content_log.txt`，确认是运行时授权票据、Manifest request code 上游、还是 Steam 更新后的缓存状态；不要再从“Lua 文件放错目录”开始重复排查。
