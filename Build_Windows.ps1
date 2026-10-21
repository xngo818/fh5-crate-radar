$ErrorActionPreference = 'Stop'
Push-Location -LiteralPath $PSScriptRoot
try {
    python -c "import struct, tkinter, PyInstaller; assert struct.calcsize('P') == 8, '64-bit Python is required'"
    if ($LASTEXITCODE -ne 0) {
        throw 'Install 64-bit Python with Tk, then run: python -m pip install -r requirements-dev.txt'
    }
    $taskDataPath = Join-Path $PSScriptRoot 'data'
    $taskExamplesPath = Join-Path $PSScriptRoot 'examples'
    $taskEntryPath = Join-Path $PSScriptRoot 'fh5_desktop.pyw'
    $taskBuildArgs = @(
        '-X', 'utf8', '-m', 'PyInstaller', '--noconfirm', '--onefile', '--windowed',
        '--name', 'FH5CrateRadar', '--paths', $PSScriptRoot, '--distpath', 'dist',
        '--workpath', 'build/work', '--specpath', 'build',
        '--add-data', "$taskDataPath;data", '--add-data', "$taskExamplesPath;examples",
        $taskEntryPath
    )
    python @taskBuildArgs
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller build failed' }
    Write-Host 'Built: dist\FH5CrateRadar.exe'
} finally {
    Pop-Location
}
