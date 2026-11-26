$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

# PowerShell 5.1 会把外部程序写到 stderr 的普通日志当成致命错误（NativeCommandError），
# 而 pip / PyInstaller 都会往 stderr 打 INFO 日志。所有原生调用统一走这个包装：
# 临时放宽 ErrorActionPreference，再用退出码判断成败。
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Arguments
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        & $FilePath @Arguments
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previous
    }
    if ($code -ne 0) {
        throw "命令失败（退出码 $code）：$FilePath $($Arguments -join ' ')"
    }
}

# 本机依赖装在仓库根的 .venv（系统 Python 装不了包），存在就优先用它。
$venvPython = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
$python = if (Test-Path -LiteralPath $venvPython) { $venvPython } else { (Get-Command python).Source }
Write-Host "Using Python: $python"

Write-Host "Installing dependencies..."
Invoke-Native $python -m pip install -r requirements-dev.txt

Write-Host "Running tests..."
Invoke-Native $python -m pytest -q

$pythonDir = Split-Path $python -Parent
$safePath = @(
    (Join-Path $env:SystemRoot "System32"),
    $env:SystemRoot,
    (Join-Path $env:SystemRoot "System32\Wbem"),
    (Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0"),
    $pythonDir,
    (Join-Path $pythonDir "Scripts")
) | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -Unique
$env:PATH = $safePath -join ";"
Write-Host "Using clean build PATH to avoid tool-runtime DLL contamination."

Write-Host "Building single-file exe..."

$version = (& $python -c "from adofai_publisher import __version__; print(__version__)").Trim()
$parts = $version.Split(".")
while ($parts.Count -lt 4) { $parts += "0" }
$fileVersion = ($parts[0..3] -join ",")
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers=($fileVersion),
    prodvers=($fileVersion),
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable(
        u'080404b0',
        [StringStruct(u'CompanyName', u'DeanClock'),
         StringStruct(u'FileDescription', u'ADOFAI GO'),
         StringStruct(u'FileVersion', u'$version'),
         StringStruct(u'InternalName', u'ADOFAI GO'),
         StringStruct(u'OriginalFilename', u'ADOFAI GO.exe'),
         StringStruct(u'ProductName', u'ADOFAI GO'),
         StringStruct(u'ProductVersion', u'$version Beta'),
         StringStruct(u'LegalCopyright', u'© 2026 DeanClock'),
         StringStruct(u'Comments', u'作者：DeanClock')])
    ]),
    VarFileInfo([VarStruct(u'Translation', [2052, 1200])])
  ]
)
"@
$versionFile = Join-Path $PWD "build\version_info.txt"
New-Item -ItemType Directory -Force -Path (Split-Path $versionFile) | Out-Null
[System.IO.File]::WriteAllText($versionFile, $versionInfo, (New-Object System.Text.UTF8Encoding($false)))
$env:ADOFAI_BUILD_VERSION_FILE = $versionFile

Invoke-Native $python -m PyInstaller --noconfirm --clean build.spec

$toc = Join-Path $PWD "build\build\Analysis-00.toc"
if (-not (Test-Path -LiteralPath $toc)) {
    throw "没有找到 PyInstaller 分析结果：$toc"
}
$pollution = Select-String -LiteralPath $toc -Pattern 'codex-runtimes|\\poppler\\|\\libheif\\|\\jxrlib\\'
if ($pollution) {
    throw "打包结果混入了外部工具链 DLL，请检查构建环境后再试。"
}
$ucrt = Select-String -LiteralPath $toc -Pattern "'ucrtbase\.dll'"
if ($ucrt) {
    throw "打包结果包含 ucrtbase.dll，可能与系统运行库冲突。"
}

$plain = Join-Path $PWD "dist\ADOFAI GO.exe"
if (-not (Test-Path -LiteralPath $plain)) {
    throw "没有生成 exe：$plain"
}

$env:ADOFAI_NO_AUTOLOGIN = "1"
$env:ADOFAI_SELFTEST_OUT = Join-Path $PWD "build\selftest.log"
if (Test-Path -LiteralPath $env:ADOFAI_SELFTEST_OUT) {
    Remove-Item -LiteralPath $env:ADOFAI_SELFTEST_OUT -Force
}
Write-Host "Running packaged self-test..."
$selftest = Start-Process -FilePath $plain -ArgumentList "--selftest" -PassThru
$finished = $true
try {
    Wait-Process -Id $selftest.Id -Timeout 180 -ErrorAction Stop
} catch {
    $finished = $false
}
if (-not $finished) {
    & taskkill /PID $selftest.Id /T /F | Out-Null
    throw "打包自检超时，exe 可能卡在错误对话框。"
}
if (-not (Test-Path -LiteralPath $env:ADOFAI_SELFTEST_OUT)) {
    throw "打包自检没有生成日志。"
}
$selftestText = Get-Content -LiteralPath $env:ADOFAI_SELFTEST_OUT -Raw -Encoding UTF8
if ($selftestText -match "selftest failed" -or $selftestText -notmatch "qt ok:" -or $selftestText -notmatch "lookup ok:" -or $selftestText -notmatch "browser ok:") {
    throw "打包自检失败：`n$selftestText"
}

$versioned = Join-Path $PWD "dist\ADOFAI GO_v$version.exe"
$temporary = Join-Path $PWD ("dist\.ADOFAI GO_v$version.exe.tmp")
if (Test-Path -LiteralPath $temporary) {
    Remove-Item -LiteralPath $temporary -Force
}
Copy-Item -LiteralPath $plain -Destination $temporary -Force
$plainHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $plain).Hash
$temporaryHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $temporary).Hash
if ($plainHash -ne $temporaryHash) {
    Remove-Item -LiteralPath $temporary -Force
    throw "版本副本校验失败，已取消生成。"
}
Move-Item -LiteralPath $temporary -Destination $versioned -Force
$hash = $temporaryHash
$shaLine = "$hash  $(Split-Path $versioned -Leaf)`n"
$shaPath = "$versioned.sha256"
$legacyShaPath = [System.IO.Path]::ChangeExtension($versioned, ".sha256")
if (Test-Path -LiteralPath $legacyShaPath) {
    Remove-Item -LiteralPath $legacyShaPath -Force
}
[System.IO.File]::WriteAllText(
    $shaPath,
    $shaLine,
    (New-Object System.Text.UTF8Encoding($false))
)

Write-Host "Done."
Write-Host "Output: $plain"
Write-Host "Versioned: $versioned"
Write-Host "SHA256: $hash"
