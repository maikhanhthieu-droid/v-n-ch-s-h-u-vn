<#
Start the bot without putting the token in the command line or a file.

Usage:
  powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
#>

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$secureToken = Read-Host "Telegram token mới (sẽ không hiển thị)" -AsSecureString
$secureGeminiKey = Read-Host "Gemini API key (Enter để bỏ qua)" -AsSecureString
$tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
$geminiPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureGeminiKey)
try {
    $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
    $geminiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($geminiPtr)
    if ($geminiKey) {
        $env:GEMINI_API_KEY = $geminiKey
    }
    $env:PYTHONUTF8 = "1"
    $python = if (Test-Path -LiteralPath ".\.venv\Scripts\python.exe") {
        ".\.venv\Scripts\python.exe"
    }
    else {
        "python"
    }
    & $python .\bot.py
}
finally {
    if ($tokenPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
    }
    if ($geminiPtr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($geminiPtr)
    }
    Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
    Remove-Item Env:GEMINI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:PYTHONUTF8 -ErrorAction SilentlyContinue
}
