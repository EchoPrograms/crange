$ErrorActionPreference = 'Stop'

# QEMU Guest Agent invokes this reviewed launcher as Local System. Sysprep must
# run as the per-build local Administrator, so create and start one fixed task.
$computer = Get-CimInstance -ClassName Win32_ComputerSystem
if ($computer.PartOfDomain) {
    throw 'Never Sysprep a domain controller or domain member as a Windows base.'
}
if (-not (Get-Service -Name 'QEMU-GA' -ErrorAction SilentlyContinue)) {
    throw 'The QEMU Guest Agent is required before sealing this Windows build.'
}
$answer = 'C:\ProgramData\CRANGE\clone-unattend.xml'
if (-not (Test-Path -LiteralPath $answer)) {
    throw 'The private per-build clone answerfile is required.'
}
[xml]$xml = [IO.File]::ReadAllText($answer)
$ns = New-Object Xml.XmlNamespaceManager($xml.NameTable)
$ns.AddNamespace('u', 'urn:schemas-microsoft-com:unattend')
$locale = $xml.SelectSingleNode('/u:unattend/u:settings[@pass="oobeSystem"]/u:component[@name="Microsoft-Windows-International-Core"]', $ns)
foreach ($name in @('InputLocale', 'SystemLocale', 'UILanguage', 'UserLocale')) {
    if (-not $locale -or $locale.SelectSingleNode('u:' + $name, $ns).InnerText -ne 'en-US') {
        throw 'Reviewed en-US OOBE locale settings are required for unattended clones.'
    }
}
$passwordNode = $xml.SelectSingleNode('/u:unattend/u:settings[@pass="oobeSystem"]/u:component[@name="Microsoft-Windows-Shell-Setup"]/u:UserAccounts/u:AdministratorPassword', $ns)
if (-not $passwordNode -or $passwordNode.PlainText -ne 'true' -or -not $passwordNode.Value) {
    throw 'The private per-build Administrator credential is unavailable.'
}
if ((Get-CimInstance Win32_OperatingSystem).Caption -match 'Evaluation') {
    $skipRearm = $xml.SelectSingleNode('/u:unattend/u:settings[@pass="generalize"]/u:component[@name="Microsoft-Windows-Security-SPP"]/u:SkipRearm', $ns)
    if (-not $skipRearm -or $skipRearm.InnerText -ne '1') {
        throw 'Evaluation image customization must preserve its activation timer with SkipRearm=1.'
    }
}
$sysprep = Join-Path $env:WINDIR 'System32\Sysprep\Sysprep.exe'
if (-not (Test-Path -LiteralPath $sysprep)) { throw 'Windows Sysprep is unavailable.' }
$credential = ConvertTo-SecureString $passwordNode.Value -AsPlainText -Force
Set-LocalUser -Name 'Administrator' -Password $credential
Enable-LocalUser -Name 'Administrator'
$action = New-ScheduledTaskAction -Execute $sysprep -Argument '/generalize /oobe /shutdown /quiet /unattend:C:\ProgramData\CRANGE\clone-unattend.xml'
$principal = New-ScheduledTaskPrincipal -UserId ($env:COMPUTERNAME + '\Administrator') -LogonType Password -RunLevel Highest
$task = New-ScheduledTask -Action $action -Principal $principal
Register-ScheduledTask -TaskPath '\CRANGE\' -TaskName 'BaseSysprep' -InputObject $task -User ($env:COMPUTERNAME + '\Administrator') -Password $passwordNode.Value -Force | Out-Null
Start-ScheduledTask -TaskPath '\CRANGE\' -TaskName 'BaseSysprep'
$deadline = (Get-Date).AddSeconds(30)
$process = $null
while ((Get-Date) -lt $deadline -and -not $process) {
    $process = Get-CimInstance Win32_Process | Where-Object { $_.Name -ieq 'sysprep.exe' } | Select-Object -First 1
    if (-not $process) { Start-Sleep -Seconds 1 }
}
if (-not $process) { throw 'The fixed Administrator Sysprep task did not start.' }
$owner = Invoke-CimMethod -InputObject $process -MethodName GetOwner
if ($owner.User -ne 'Administrator' -or $owner.Domain -ne $env:COMPUTERNAME) {
    throw 'Sysprep must run as the per-build local Administrator, never Local System.'
}
Unregister-ScheduledTask -TaskPath '\CRANGE\' -TaskName 'BaseSysprep' -Confirm:$false
# Do not print the answerfile, password, or guest command output. The worker
# durably records this QGA handle and waits for the VM shutdown before sealing.
Write-Output 'CRANGE_FIXED_ADMINISTRATOR_SYSPREP_STARTED'
