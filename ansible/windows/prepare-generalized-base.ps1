$ErrorActionPreference = 'Stop'

# Run only on a fresh Windows Server 2025 Desktop Experience installer VM,
# before publishing it. This deliberately leaves the machine in a workgroup.
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
if ($computer.PartOfDomain) {
    throw 'Refusing to prepare an Active Directory domain controller or member as a base image.'
}

$qemuAgent = Get-Service -Name 'QEMU-GA' -ErrorAction SilentlyContinue
if (-not $qemuAgent) {
    throw 'Install the signed VirtIO QEMU Guest Agent package before preparing the base.'
}
$answer = 'C:\ProgramData\CRANGE\clone-unattend.xml'
if (-not (Test-Path -LiteralPath $answer)) {
    throw 'The private per-build unattended answerfile was not installed by Windows Setup.'
}
[xml]$xml = [IO.File]::ReadAllText($answer)
$ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
$password = $xml.SelectSingleNode('/u:unattend/u:settings[@pass="oobeSystem"]/u:component[@name="Microsoft-Windows-Shell-Setup"]/u:UserAccounts/u:AdministratorPassword/u:Value', $ns)
$locale = $xml.SelectSingleNode('/u:unattend/u:settings[@pass="oobeSystem"]/u:component[@name="Microsoft-Windows-International-Core"]', $ns)
if (-not $password -or -not $password.InnerText -or -not $locale) {
    throw 'The private answerfile must contain the per-build Administrator credential and OOBE locale settings.'
}
foreach ($name in @('InputLocale', 'SystemLocale', 'UILanguage', 'UserLocale')) {
    if ($locale.SelectSingleNode('u:' + $name, $ns).InnerText -ne 'en-US') {
        throw 'The reviewed en-US OOBE locale settings are required.'
    }
}
Set-Service -Name 'QEMU-GA' -StartupType Automatic
Start-Service -Name 'QEMU-GA'

Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' `
    -Name 'fDenyTSConnections' -Value 0 -Type DWord
Set-Service -Name 'TermService' -StartupType Automatic
Enable-NetFirewallRule -DisplayGroup 'Remote Desktop'

Enable-PSRemoting -Force -SkipNetworkProfileCheck
Set-Service -Name 'WinRM' -StartupType Automatic
Set-Item -Path 'WSMan:\localhost\Service\Auth\Basic' -Value $false
Set-Item -Path 'WSMan:\localhost\Service\AllowUnencrypted' -Value $false
$allowed = '10.60.0.20'
$winrmRules = Get-NetFirewallRule -DisplayGroup 'Windows Remote Management'
$winrmRules | Set-NetFirewallRule -Enabled True -Profile Any
$winrmRules | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $allowed
$rdpRules = Get-NetFirewallRule -DisplayGroup 'Remote Desktop'
$rdpRules | Set-NetFirewallRule -Enabled True -Profile Any
$rdpRules | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress $allowed

# The controlled worker assigns each clone's hostname and static network through
# fixed QEMU Guest Agent PowerShell calls before it opens WinRM to project traffic.
Write-Output 'Windows base setup verified; signed agent and serial driver are installed, and controller-only remote firewall scopes are ready.'
