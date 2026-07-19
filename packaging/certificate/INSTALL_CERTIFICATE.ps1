[CmdletBinding()]
param(
    [switch]$ConfirmInstall
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
$codeSigningOid = "1.3.6.1.5.5.7.3.3"
if ($certificate.Subject -ne $certificate.Issuer) {
    throw "The included certificate is not self-signed."
}
if ($certificate.HasPrivateKey) {
    throw "The package must not contain a private key."
}
$enhancedKeyUsage = $certificate.Extensions | Where-Object {
    $_.Oid.Value -eq "2.5.29.37"
}
$hasCodeSigningUsage = $enhancedKeyUsage.EnhancedKeyUsages | Where-Object {
    $_.Value -eq $codeSigningOid
}
if (-not $hasCodeSigningUsage) {
    throw "The included certificate does not declare code-signing usage."
}
if ($certificate.NotAfter -le (Get-Date)) {
    throw "The included certificate has expired."
}

Write-Host "SELF-SIGNED CERTIFICATE FOR A CONTROLLED ENVIRONMENT" -ForegroundColor Yellow
Write-Host "Subject:    $($certificate.Subject)"
Write-Host "Issuer:     $($certificate.Issuer)"
Write-Host "Thumbprint: $($certificate.Thumbprint)"
Write-Host "Expires:    $($certificate.NotAfter.ToString('u'))"
Write-Host ""
Write-Host "Installing it makes this computer trust programs signed with this identity."
Write-Host "Verify the thumbprint through an independent channel first."

if (-not $ConfirmInstall) {
    $confirmation = Read-Host "Type INSTALL to continue"
    if ($confirmation -cne "INSTALL") {
        throw "Installation cancelled."
    }
}

foreach ($store in @("Cert:\LocalMachine\Root", "Cert:\LocalMachine\TrustedPublisher")) {
    $installedPath = Join-Path $store $certificate.Thumbprint
    if (-not (Test-Path -LiteralPath $installedPath)) {
        Import-Certificate -FilePath $certificatePath -CertStoreLocation $store | Out-Null
    }
}

Write-Host "Certificate installed successfully." -ForegroundColor Green
Write-Host "Close this console and run Twin-Tongue.exe from the extracted directory."
