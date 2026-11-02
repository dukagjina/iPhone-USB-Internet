# Invoked only by the explicitly approved connection worker.
# All mutations are scoped to one exact, newly-created Wintun adapter.
param(
    [Parameter(Mandatory=$true)][ValidateSet('Inspect','Configure','Cleanup','Verify')][string]$Action,
    [ValidatePattern('^iPhoneUSB-[a-f0-9]{12}$')][string]$Alias,
    [string]$ExpectedGuid
)
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
. (Join-Path $PSScriptRoot 'adapter_identity.ps1')
if ($Action -eq 'Inspect') {
    $conflicts = @(Get-NetAdapter -IncludeHidden | Where-Object { $_.Name -like 'iPhoneUSB-*' })
    $routes = @(Get-NetRoute -PolicyStore ActiveStore | Where-Object {
        $_.DestinationPrefix -in @('0.0.0.0/1','128.0.0.0/1','::/1','8000::/1')
    })
    $addresses = @(Get-NetIPAddress | Where-Object { $_.IPAddress -eq '198.18.7.1' -or $_.IPAddress -eq 'fd72:7573:6200::1' })
    $vpn = @(Get-NetAdapter | Where-Object {
        $_.Status -eq 'Up' -and ($_.InterfaceDescription -match 'WireGuard|Wintun|TAP-Windows|OpenVPN|Tailscale|Zscaler|AnyConnect|GlobalProtect|Fortinet')
    })
    @{ conflict=($conflicts.Count -gt 0 -or $routes.Count -gt 0 -or $addresses.Count -gt 0 -or $vpn.Count -gt 0) } | ConvertTo-Json -Compress
    exit
}
if (-not $Alias) { throw 'An exact session adapter name is required.' }
$adapter = Get-NetAdapter -Name $Alias -IncludeHidden -ErrorAction SilentlyContinue
if (-not $adapter) {
    if ($Action -eq 'Cleanup') { exit }
    throw 'The temporary phone adapter was not found.'
}
$identity = Confirm-PhoneAdapterIdentity -Adapter $adapter -Alias $Alias -ExpectedGuid $ExpectedGuid
$index = $identity.index
if ($Action -eq 'Cleanup') {
    # Only this session's active routes. Never reset general networking or DNS.
    Get-NetRoute -InterfaceIndex $index -PolicyStore ActiveStore -ErrorAction SilentlyContinue |
        Where-Object { $_.DestinationPrefix -in @('0.0.0.0/0','0.0.0.0/1','128.0.0.0/1','::/0','::/1','8000::/1') } |
        Remove-NetRoute -Confirm:$false
    exit
}
if ($Action -eq 'Verify') {
    # Bind the HTTPS verification to the TUN source so fallback cannot pass it.
    & "$env:SystemRoot\System32\curl.exe" --noproxy '*' --interface '198.18.7.1' --connect-timeout 8 --max-time 15 --silent --output NUL --fail https://1.1.1.1/
    if ($LASTEXITCODE -ne 0) { throw 'The new phone route did not pass its HTTPS check.' }
    $answer = Resolve-DnsName -Name example.com -Type A -Server 1.1.1.1 -DnsOnly -QuickTimeout
    if (-not @($answer | Where-Object Type -eq 'A').Count) { throw 'Phone-route DNS check failed.' }
    @{ok=$true} | ConvertTo-Json -Compress
    exit
}
Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv4 -Dhcp Disabled -InterfaceMetric 1
New-NetIPAddress -InterfaceIndex $index -IPAddress '198.18.7.1' -PrefixLength 24 -PolicyStore ActiveStore | Out-Null
Set-NetIPInterface -InterfaceIndex $index -AddressFamily IPv6 -InterfaceMetric 1
New-NetIPAddress -InterfaceIndex $index -IPAddress 'fd72:7573:6200::1' -PrefixLength 64 -PolicyStore ActiveStore | Out-Null
$deadline = [DateTime]::UtcNow.AddSeconds(20)
do {
    $addresses = @(Get-NetIPAddress -InterfaceIndex $index | Where-Object { $_.IPAddress -in @('198.18.7.1','fd72:7573:6200::1') })
    if (@($addresses | Where-Object AddressState -eq 'Duplicate').Count) { throw 'Temporary adapter address conflict.' }
    if ($addresses.Count -eq 2 -and @($addresses | Where-Object AddressState -ne 'Preferred').Count -eq 0) { break }
    Start-Sleep -Milliseconds 200
} while ([DateTime]::UtcNow -lt $deadline)
if ($addresses.Count -ne 2 -or @($addresses | Where-Object AddressState -ne 'Preferred').Count) { throw 'Temporary addresses did not become ready.' }
Set-DnsClientServerAddress -InterfaceIndex $index -ServerAddresses @('1.1.1.1','1.0.0.1')
Set-DnsClient -InterfaceIndex $index -RegisterThisConnectionsAddress $false
foreach ($prefix in @('0.0.0.0/0','0.0.0.0/1','128.0.0.0/1','::/0','::/1','8000::/1')) {
    $nextHop = if ($prefix.Contains(':')) { '::' } else { '198.18.7.1' }
    New-NetRoute -DestinationPrefix $prefix -InterfaceIndex $index -NextHop $nextHop -RouteMetric 1 -PolicyStore ActiveStore | Out-Null
}
Clear-DnsClientCache
$identity | ConvertTo-Json -Compress
