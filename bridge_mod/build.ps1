param(
    [string]$GameDir = "C:\steam\steamapps\common\A Dance of Fire and Ice"
)

$ErrorActionPreference = "Stop"
$managed = Join-Path $GameDir "A Dance of Fire and Ice_Data\Managed"
$umm = Join-Path $managed "UnityModManager\UnityModManager.dll"
$csc = Join-Path $env:WINDIR "Microsoft.NET\Framework64\v4.0.30319\csc.exe"

if (-not (Test-Path $csc)) {
    throw "找不到 csc.exe：$csc"
}
if (-not (Test-Path $umm)) {
    throw "找不到 UnityModManager.dll：$umm"
}

$outDir = Join-Path $PSScriptRoot "bin"
New-Item -ItemType Directory -Force -Path $outDir | Out-Null

$refs = @(
    (Join-Path $managed "Assembly-CSharp.dll"),
    (Join-Path $managed "Assembly-CSharp-firstpass.dll"),
    (Join-Path $managed "UnityEngine.dll"),
    (Join-Path $managed "UnityEngine.CoreModule.dll"),
    (Join-Path $managed "UnityEngine.IMGUIModule.dll"),
    (Join-Path $managed "UnityEngine.UIModule.dll"),
    (Join-Path $managed "UnityEngine.UI.dll"),
    (Join-Path $managed "RDTools.dll"),
    (Join-Path $managed "netstandard.dll"),
    (Join-Path $managed "System.Runtime.dll"),
    $umm
) | ForEach-Object { "/r:$_" }

& $csc /nologo /target:library /platform:anycpu `
    "/out:$(Join-Path $outDir 'ADOFAIGOBridge.dll')" `
    $refs `
    (Join-Path $PSScriptRoot "ADOFAIGOBridge.cs")

if ($LASTEXITCODE -ne 0) {
    throw "编译失败，退出码 $LASTEXITCODE"
}

Copy-Item -LiteralPath (Join-Path $PSScriptRoot "Info.json") -Destination $outDir -Force
Copy-Item -LiteralPath (Join-Path $outDir "ADOFAIGOBridge.dll") -Destination $PSScriptRoot -Force
Write-Host "已生成：$outDir\ADOFAIGOBridge.dll"
