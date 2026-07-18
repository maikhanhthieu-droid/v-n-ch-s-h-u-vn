<#
Start the bot without putting the token in the command line or a file.

Usage:
  powershell -ExecutionPolicy Bypass -File .\run_bot.ps1
#>

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $PSScriptRoot

$secureToken = Read-Host "Telegram token mới (sẽ không hiển thị)" -AsSecureString
$ptr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $env:TELEGRAM_BOT_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($ptr)
    python .\bot.py
}
finally {
    if ($ptr -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($ptr)
    }
    Remove-Item Env:TELEGRAM_BOT_TOKEN -ErrorAction SilentlyContinue
}

