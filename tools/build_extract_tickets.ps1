# 编译 D 加密票据提取工具（tools/extract_tickets/extract_tickets.exe）
#
# 源码来自上游 OpenSteam001/OpenSteamTool 的 tools/extract_tickets。
# 需要 MinGW-w64（x86_64-w64-mingw32-g++），例如 scoop install gcc / msys2。
[CmdletBinding()]
param(
    [string]$Output = "extract_tickets.exe",
    [switch]$Dynamic   # 动态链接 libstdc++/libgcc（体积更小，但需要 MinGW 运行库）
)

$ErrorActionPreference = "Stop"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$src = Join-Path $dir "extract_tickets.cpp"

if (-not (Test-Path $src)) {
    throw "找不到源码: $src"
}

$gxx = (Get-Command "g++" -ErrorAction SilentlyContinue)
if (-not $gxx) {
    throw "未找到 g++，请先安装 MinGW-w64（x86_64-w64-mingw32）"
}

$target = & $gxx.Source -dumpmachine
Write-Host "编译器: $($gxx.Source) [$target]"
if ($target -notmatch "x86_64") {
    throw "必须使用 64 位工具链编译（当前: $target），否则无法加载 steamclient64.dll"
}

$args = @("-O2", "-std=c++20", "-s")
if (-not $Dynamic) {
    $args += @("-static", "-static-libgcc", "-static-libstdc++")
}
$args += @("-o", (Join-Path $dir $Output), $src)

Write-Host "g++ $($args -join ' ')"
& $gxx.Source @args
if ($LASTEXITCODE -ne 0) {
    throw "编译失败，退出码 $LASTEXITCODE"
}

Get-Item (Join-Path $dir $Output) | Select-Object Length, FullName
Write-Host "编译完成。运行方式: .\$Output <appid>（需要 Steam 已运行并登录）"
