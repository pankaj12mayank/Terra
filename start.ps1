[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ScriptArgs
)

& python "$PSScriptRoot\run.py" @ScriptArgs

