# Interactive WhatsApp simulator - chat with the agent as if from WhatsApp,
# without any Meta account.
#
# Prerequisites (running):
#   1. the agent container            (docker-compose up -d)
#   2. the fake Meta server           (python -u tests\fake_meta.py)
#   3. agent .env has                 WHATSAPP_API_BASE=http://host.docker.internal:9001
#
# Usage:  powershell -File tests\wa_simulator.ps1 [-Phone 966512345678] [-Name "Test Parent"]
param(
    [string]$Phone = "9665$(Get-Random -Minimum 10000000 -Maximum 99999999)",
    [string]$Name = "Sim Parent",
    [string]$AgentUrl = "http://localhost:8085",
    [string]$FakeMetaUrl = "http://localhost:9001",
    [string]$ChannelId = "2"
)

Write-Host "WhatsApp simulator - you are $Name ($Phone) on channel $ChannelId. Type 'exit' to quit." -ForegroundColor Green
$lastId = 0
# drain anything already in the outbox
try { $init = Invoke-RestMethod "$FakeMetaUrl/outbox?after_id=0" -TimeoutSec 5; if ($init.messages) { $lastId = ($init.messages | Select-Object -Last 1).id } } catch {}

while ($true) {
    $text = Read-Host "`nYou"
    if ($text -eq 'exit' -or [string]::IsNullOrWhiteSpace($text)) { break }

    $payload = @{ object = 'whatsapp_business_account'; entry = @(@{ id = '1'; changes = @(@{ field = 'messages'; value = @{
        messaging_product = 'whatsapp'
        metadata = @{ display_phone_number = '15550000000'; phone_number_id = 'SIMULATOR' }
        contacts = @(@{ profile = @{ name = $Name }; wa_id = $Phone })
        messages = @(@{ from = $Phone; id = "wamid.SIM$(Get-Random)"; timestamp = [string][int][double]::Parse((Get-Date -UFormat %s)); type = 'text'; text = @{ body = $text } })
    } }) }) } | ConvertTo-Json -Depth 10

    try { Invoke-RestMethod -Method Post "$AgentUrl/webhooks/whatsapp/$ChannelId/" -Body $payload -ContentType 'application/json' -TimeoutSec 20 | Out-Null }
    catch { Write-Host "send failed: $($_.Exception.Message)" -ForegroundColor Red; continue }

    # poll the fake Meta outbox for the reply (debounce 1.5s + LLM takes a while)
    $deadline = (Get-Date).AddSeconds(60)
    $got = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 1500
        try { $r = Invoke-RestMethod "$FakeMetaUrl/outbox?after_id=$lastId" -TimeoutSec 5 } catch { continue }
        $mine = @($r.messages | Where-Object { $_.to -eq $Phone })
        if ($mine.Count -gt 0) {
            foreach ($m in $mine) { Write-Host "`nAgent: $($m.text)" -ForegroundColor Cyan }
            $lastId = ($r.messages | Select-Object -Last 1).id
            $got = $true
            # brief grace poll for a second bubble (e.g. OTP notice)
            Start-Sleep -Milliseconds 2500
            try { $r2 = Invoke-RestMethod "$FakeMetaUrl/outbox?after_id=$lastId" -TimeoutSec 5 } catch { $r2 = $null }
            if ($r2 -and $r2.messages) {
                foreach ($m in ($r2.messages | Where-Object { $_.to -eq $Phone })) { Write-Host "Agent: $($m.text)" -ForegroundColor Cyan }
                $lastId = ($r2.messages | Select-Object -Last 1).id
            }
            break
        }
    }
    if (-not $got) { Write-Host "(no reply within 60s - check docker logs crono-agent)" -ForegroundColor Yellow }
}
