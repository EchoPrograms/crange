$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

$logDir = 'C:\ProgramData\CRANGE'
New-Item -Path $logDir -ItemType Directory -Force | Out-Null
Start-Transcript -Path (Join-Path $logDir 'base-setup.log') -Append
try {
    if ((Get-CimInstance Win32_ComputerSystem).PartOfDomain) {
        throw 'Refusing to run base setup on a domain member or controller.'
    }

    $agentMsi = $null
    $serialDriver = $null
    $seedAnswer = $null
    foreach ($drive in Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=5') {
        $candidate = Join-Path $drive.DeviceID 'guest-agent\qemu-ga-x86_64.msi'
        if (Test-Path -LiteralPath $candidate) { $agentMsi = $candidate; break }
    }
    if (-not $agentMsi) { throw 'Signed VirtIO QEMU Guest Agent MSI was not found.' }

    foreach ($drive in Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=5') {
        $candidate = Join-Path $drive.DeviceID 'vioserial\2k25\amd64\vioser.inf'
        if (Test-Path -LiteralPath $candidate) { $serialDriver = $candidate; break }
    }
    if (-not $serialDriver) { throw 'Signed Windows Server 2025 VirtIO serial driver was not found.' }

    foreach ($drive in Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=5') {
        $candidateScript = Join-Path $drive.DeviceID 'crange-win-setup.ps1'
        $candidateAnswer = Join-Path $drive.DeviceID 'Autounattend.xml'
        if ((Test-Path -LiteralPath $candidateScript) -and (Test-Path -LiteralPath $candidateAnswer)) {
            $seedAnswer = $candidateAnswer
            break
        }
    }
    if (-not $seedAnswer) { throw 'The private per-build unattended answerfile was not found.' }
    [xml]$answerXml = [IO.File]::ReadAllText($seedAnswer)
    if ($answerXml.DocumentElement.LocalName -ne 'unattend' -or
        $answerXml.DocumentElement.NamespaceURI -ne 'urn:schemas-microsoft-com:unattend') {
        throw 'The generated private unattended answerfile is invalid.'
    }
    $cloneAnswer = Join-Path $logDir 'clone-unattend.xml'
    Copy-Item -LiteralPath $seedAnswer -Destination $cloneAnswer -Force
    (Get-Item -LiteralPath $cloneAnswer).IsReadOnly = $true

    $driver = Start-Process -FilePath "$env:SystemRoot\System32\pnputil.exe" `
        -ArgumentList @('/add-driver', "`"$serialDriver`"", '/install') -Wait -PassThru
    if ($driver.ExitCode -notin @(0, 3010)) {
        throw "VirtIO serial driver installation exited with code $($driver.ExitCode)."
    }

    $install = Start-Process -FilePath "$env:SystemRoot\System32\msiexec.exe" `
        -ArgumentList @('/i', "`"$agentMsi`"", '/qn', '/norestart') -Wait -PassThru
    if ($install.ExitCode -notin @(0, 3010)) {
        throw "QEMU Guest Agent MSI exited with code $($install.ExitCode)."
    }
    Set-Service -Name 'QEMU-GA' -StartupType Automatic
    Start-Service -Name 'QEMU-GA'

    Set-ItemProperty -Path 'HKLM:\SYSTEM\CurrentControlSet\Control\Terminal Server' `
        -Name 'fDenyTSConnections' -Value 0 -Type DWord
    Set-Service -Name 'TermService' -StartupType Automatic
    $rdpRules = Get-NetFirewallRule -DisplayGroup 'Remote Desktop'
    $rdpRules | Set-NetFirewallRule -Enabled True -Profile Any
    $rdpRules | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress '10.60.0.20'

    Enable-PSRemoting -Force -SkipNetworkProfileCheck
    Set-Service -Name 'WinRM' -StartupType Automatic
    Set-Item -Path 'WSMan:\localhost\Service\Auth\Basic' -Value $false
    Set-Item -Path 'WSMan:\localhost\Service\AllowUnencrypted' -Value $false
    $winrmRules = Get-NetFirewallRule -DisplayGroup 'Windows Remote Management'
    $winrmRules | Set-NetFirewallRule -Enabled True -Profile Any
    $winrmRules | Get-NetFirewallAddressFilter | Set-NetFirewallAddressFilter -RemoteAddress '10.60.0.20'

    Write-Output 'Installed the signed VirtIO serial driver and QEMU Guest Agent; RDP and WinRM are limited to the controller/Guacamole address.'
}
finally {
    Stop-Transcript
}
