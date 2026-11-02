# Identity validation only. This file never changes adapters or routes.
function Confirm-PhoneAdapterIdentity {
    param(
        [Parameter(Mandatory=$true)]$Adapter,
        [Parameter(Mandatory=$true)][ValidatePattern('^iPhoneUSB-[a-f0-9]{12}$')][string]$Alias,
        [string]$ExpectedGuid
    )
    if ($Adapter.Name -ne $Alias) { throw 'Adapter name changed; refusing the operation.' }
    $guid = [Guid]$Adapter.InterfaceGuid
    if ($guid -eq [Guid]::Empty) { throw 'The adapter has no valid interface identity.' }
    if ($ExpectedGuid -and $guid -ne [Guid]$ExpectedGuid) {
        throw 'Adapter identity changed; refusing the operation.'
    }
    $deviceId = [string]$Adapter.PnPDeviceID
    if ($deviceId -notmatch '^(SWD|ROOT)\\Wintun\\[^\\]+$') {
        throw 'Refusing to touch an adapter outside the Wintun device family.'
    }
    # Friendly descriptions are caller-controlled/localized (e.g. tun2socks Tunnel).
    # Verify the actual PnP driver binding instead, without accepting lookalike names.
    $device = Get-ItemProperty -LiteralPath ('HKLM:\SYSTEM\CurrentControlSet\Enum\' + $deviceId) -ErrorAction Stop
    if ($device.Service -ne 'wintun' -or @($device.HardwareID) -notcontains 'Wintun') {
        throw 'Refusing to touch an adapter not bound to the Wintun driver.'
    }
    if ([int]$Adapter.ifIndex -le 0) { throw 'The adapter interface index is invalid.' }
    return @{guid=$guid.ToString(); index=[int]$Adapter.ifIndex}
}
