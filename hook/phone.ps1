# Sends Claude Code events to the phone through ntfy. Reads the hook JSON from stdin.
# Topic name is kept in ntfy-topic.txt next to this script. Anyone with the name can read the alerts.
$ErrorActionPreference = 'SilentlyContinue'
$topic = (Get-Content -Raw (Join-Path $PSScriptRoot 'ntfy-topic.txt')).Trim()
$in = [Console]::In.ReadToEnd() | ConvertFrom-Json

function Send-Phone([string]$title, [string]$body, [string]$priority) {
    $text = if ($body.Length -gt 500) { $body.Substring(0, 500) } else { $body }
    Invoke-RestMethod -Method Post -Uri "https://ntfy.sh/$topic" -Body $text `
        -Headers @{ Title = $title; Priority = $priority } | Out-Null
}

switch ($in.hook_event_name) {
    'Notification' {
        $msg = if ($in.message) { [string]$in.message } else { 'Claude is waiting for your input' }
        Send-Phone 'Claude needs you' $msg 'high'
    }
    'Stop' {
        # A marker counts only as the first word of a line, so plain prose that mentions it does not alert
        $message = [string]$in.last_assistant_message
        $lines = $message -split '\r?\n'
        if ($lines | Where-Object { $_ -match '^\s*LOOP_DONE\b' }) {
            Send-Phone 'Loop finished' $message 'default'
        } elseif ($lines | Where-Object { $_ -match '^\s*NEED_INPUT\b' }) {
            Send-Phone 'Loop needs a decision' $message 'high'
        }
    }
}
exit 0
