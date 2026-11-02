# Offline regression tests: only the identity helper is loaded. No network cmdlets.
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\src\adapter_identity.ps1')
$script:device = [pscustomobject]@{ Service='wintun'; HardwareID=@('Wintun') }
$script:reads = 0
function Get-ItemProperty {
    param([string]$LiteralPath, [string]$ErrorAction)
    if ($LiteralPath -notlike 'HKLM:\SYSTEM\CurrentControlSet\Enum\SWD\Wintun\*') { throw 'Unexpected registry target' }
    $script:reads++
    return $script:device
}
function New-Fixture {
    return [pscustomobject]@{
        Name='iPhoneUSB-0123456789ab'; InterfaceGuid='00000000-0000-0000-0000-000000000001'
        ifIndex=42; InterfaceDescription='tun2socks Tunnel'
        PnPDeviceID='SWD\Wintun\{00000000-0000-0000-0000-000000000003}'
    }
}
function Expect-Rejected($fixture, [string]$expectedGuid='') {
    $rejected = $false
    try { Confirm-PhoneAdapterIdentity -Adapter $fixture -Alias 'iPhoneUSB-0123456789ab' -ExpectedGuid $expectedGuid | Out-Null }
    catch { $rejected = $true }
    if (-not $rejected) { throw 'Unsafe fixture was accepted' }
}
$adapter = New-Fixture
$result = Confirm-PhoneAdapterIdentity -Adapter $adapter -Alias $adapter.Name -ExpectedGuid $adapter.InterfaceGuid
if ($result.index -ne 42 -or $result.guid -ne $adapter.InterfaceGuid) { throw 'Identity not preserved' }
'PASS: real tun2socks display name accepted with genuine Wintun binding'
$adapter.InterfaceDescription = 'Localized display text'
Confirm-PhoneAdapterIdentity -Adapter $adapter -Alias $adapter.Name | Out-Null
'PASS: identity does not depend on display language'
$adapter = New-Fixture
$adapter.Name = 'Wi-Fi'
Expect-Rejected $adapter
'PASS: unrelated adapter name rejected'
$adapter = New-Fixture
Expect-Rejected $adapter '00000000-0000-0000-0000-000000000002'
'PASS: changed GUID rejected'
$adapter = New-Fixture
$adapter.PnPDeviceID = 'PCI\VEN_8086\Wintun'
$before = $script:reads
Expect-Rejected $adapter
if ($script:reads -ne $before) { throw 'Unexpected driver registry read' }
'PASS: non-Wintun device family rejected before registry access'
$adapter = New-Fixture
$adapter.InterfaceDescription = 'Wintun Userspace Tunnel'
$script:device = [pscustomobject]@{ Service='other-driver'; HardwareID=@('Wintun') }
Expect-Rejected $adapter
'PASS: spoofed display name with wrong service rejected'
$script:device = [pscustomobject]@{ Service='wintun'; HardwareID=@('OtherHardware') }
Expect-Rejected $adapter
'PASS: wrong hardware ID rejected'
$script:device = [pscustomobject]@{ Service='wintun'; HardwareID=@('Wintun') }
$adapter.InterfaceGuid = [Guid]::Empty
Expect-Rejected $adapter
'PASS: empty GUID rejected'
$adapter = New-Fixture
$adapter.ifIndex = 0
Expect-Rejected $adapter
'PASS: invalid interface index rejected'
'9 adapter identity tests passed. No real registry or network settings were accessed.'
