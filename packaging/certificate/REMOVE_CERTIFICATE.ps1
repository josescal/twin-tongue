[CmdletBinding()]
param(
    [switch]$ConfirmRemoval
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw "Open PowerShell as administrator and run this script again."
}

$certificatePath = Join-Path $PSScriptRoot "Twin-Tongue.cer"
if (-not (Test-Path -LiteralPath $certificatePath)) {
    throw "Certificate not found: $certificatePath"
}

$certificate = [Security.Cryptography.X509Certificates.X509Certificate2]::new($certificatePath)
Write-Host "Only this certificate will be removed:" -ForegroundColor Yellow
Write-Host "Subject:    $($certificate.Subject)"
Write-Host "Thumbprint: $($certificate.Thumbprint)"

if (-not $ConfirmRemoval) {
    $confirmation = Read-Host "Type REMOVE to continue"
    if ($confirmation -cne "REMOVE") {
        throw "Removal cancelled."
    }
}

foreach ($store in @("Cert:\LocalMachine\Root", "Cert:\LocalMachine\TrustedPublisher")) {
    $installedPath = Join-Path $store $certificate.Thumbprint
    if (Test-Path -LiteralPath $installedPath) {
        Remove-Item -LiteralPath $installedPath -Force
    }
}

Write-Host "Certificate removed successfully." -ForegroundColor Green
