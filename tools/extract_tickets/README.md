# extract_tickets —— D 加密（Denuvo）票据提取工具

本目录是从上游开源项目 **OpenSteamTool** 移植的票据提取器，用于在**拥有该游戏的
正版账号**机器上导出 D 加密游戏所需的真实票据。

* 源码来源：`OpenSteam001/OpenSteamTool` → `tools/extract_tickets/`
  （`extract_tickets.cpp`、`steam.h`，MIT/开源协议随上游）
* 用途：为 `OpenSteamToolDesktop` 的「D 加密授权」功能提供**本地提取**能力

## 它做什么

1. 从注册表 `HKCU\Software\Valve\Steam\SteamPath` 读取 Steam 安装路径
2. `LoadLibraryEx` 加载 `steamclient64.dll`
3. `CreateInterface("SteamClient023")` → `CreateSteamPipe()` → `ConnectToGlobalUser()`
4. 通过 `ISteamAppTicket::GetAppOwnershipTicketData(appid, ...)` 取**应用所有权票据**
5. 通过 `ISteamUser::RequestEncryptedAppTicket` + `GetEncryptedAppTicket` 取**加密应用票据**
6. 在 `./<appid>/` 下写出：
   * `appticket.bin` —— 原始所有权票据（二进制）
   * `eticket.bin` —— 原始加密票据（二进制）
   * `tickets.txt` —— 十六进制摘要，可直接粘贴进 Lua

`tickets.txt` 示例：

```
appid:1361510
appticket(184 bytes):14000000...
eticket(143 bytes):...
```

## 前置条件

* Windows **64 位**（工具必须与 `steamclient64.dll` 同架构）
* **Steam 正在运行且已登录**到拥有目标游戏的账号
* 该账号至少启动过一次目标游戏（否则所有权票据不在本地缓存）

## 使用

```powershell
.\extract_tickets.exe 1361510
```

不带参数运行时会提示输入 AppID。运行结束按回车退出。

## 自行编译

Windows 上使用 MinGW-w64（`x86_64-w64-mingw32`）：

```powershell
g++ -O2 -std=c++20 -s -static -o extract_tickets.exe extract_tickets.cpp
```

或直接运行仓库根目录的 `tools/build_extract_tickets.ps1`。

## 为什么必须"提取/导入"而不能"破解"

`AppTicket` / `ETicket` 是 **Valve 用私钥签名的真实凭据**，本地无法离线伪造：

* 仅 SteamStub 保护的游戏可利用 steamdrmp 票据解析漏洞伪造 AppId（上游
  OpenSteamTool 已内置该回退路径，无需票据）；
* **Denuvo 保护的游戏必须提供真实票据**，票据有效期约 30 分钟 ~ 数小时，
  过期或更换硬件后会报 Denuvo 错误码 `88500005`。

因此本工具是合规的自持票据导出手段，不包含任何绕过算法。
