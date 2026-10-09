# Ayos emergency reset: undoes the four practice faults without Python and without Ayos running.
#   restore_network.bat        undo only what the practice bench changes
#   restore_network.bat -All   also set every adapter's DNS to automatic and turn any proxy off
param([switch]$All)

$bogus = @('192.0.2.53', '2001:db8::53')
$demoProxy = '127.0.0.1:9'
$inet = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings'
$hosts = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'

Write-Host 'Ayos: putting network settings back...'

Get-NetAdapter -Physical | Where-Object { $_.Status -eq 'Disabled' } | ForEach-Object {
    Enable-NetAdapter -Name $_.Name -Confirm:$false
    Write-Host "  Turned adapter '$($_.Name)' back on."
}

Get-NetAdapter -Physical | ForEach-Object {
    $name = $_.Name
    $servers = @(Get-DnsClientServerAddress -InterfaceAlias $name -ErrorAction SilentlyContinue | ForEach-Object { $_.ServerAddresses })
    $isBogus = @($servers | Where-Object { $bogus -contains $_ }).Count -gt 0
    if ($All -or $isBogus) {
        Set-DnsClientServerAddress -InterfaceAlias $name -ResetServerAddresses -ErrorAction SilentlyContinue
        Write-Host "  DNS on '$name' set back to automatic."
    }
}

$p = Get-ItemProperty -Path $inet -ErrorAction SilentlyContinue
if ($p -and ($All -or $p.ProxyServer -eq $demoProxy)) {
    Set-ItemProperty -Path $inet -Name ProxyEnable -Value 0
    if ($p.ProxyServer -eq $demoProxy) { Set-ItemProperty -Path $inet -Name ProxyServer -Value '' }
    Write-Host '  Proxy turned off.'
}

if (Test-Path $hosts) {
    $lines = @(Get-Content -Path $hosts)
    $kept = @($lines | Where-Object { $_ -notmatch '# ayos-demo' })
    if ($kept.Count -ne $lines.Count) {
        Set-Content -Path $hosts -Value $kept -Encoding ASCII
        Write-Host '  Removed practice lines from the hosts file.'
    }
}

Clear-DnsClientCache
Write-Host 'Done. If the internet is still down, wait 15 seconds for Wi-Fi to reconnect.'
