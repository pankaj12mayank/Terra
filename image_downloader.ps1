[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

& python "$PSScriptRoot\image_downloader.py" @ScriptArgs

