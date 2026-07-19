[CmdletBinding()]
param(
    [string]$Subject = "CN=Twin Tongue",
    [ValidateRange(1, 60)]
    [int]$ValidityMonths = 24,
    [switch]$ForceNew
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ($env:OS -ne "Windows_NT") {
    throw "Este certificado solo se puede crear en Windows."
}

$codeSigningOid = "1.3.6.1.5.5.7.3.3"
$existingCertificates = @(Get-ChildItem Cert:\CurrentUser\My | Where-Object {
    $_.Subject -eq $Subject -and
    $_.HasPrivateKey -and
    $_.NotAfter -gt (Get-Date) -and
    ($_.EnhancedKeyUsageList.ObjectId -contains $codeSigningOid)
})

if ($existingCertificates.Count -gt 0 -and -not $ForceNew) {
    if ($existingCertificates.Count -gt 1) {
        throw "Hay varios certificados validos para '$Subject'. Usa su huella explicitamente o -ForceNew."
    }
    $certificate = $existingCertificates[0]
    Write-Host "Ya existe un certificado valido. Se reutilizara para mantener la identidad de firma."
} else {
    Write-Host "Creando certificado autofirmado de codigo..."
    $certificate = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $Subject `
        -FriendlyName "Twin Tongue" `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -KeyAlgorithm RSA `
        -KeyLength 3072 `
        -HashAlgorithm SHA256 `
        -KeyExportPolicy NonExportable `
        -NotAfter (Get-Date).AddMonths($ValidityMonths)
}

Write-Host ""
Write-Host "Certificado preparado:"
Write-Host "  Sujeto:     $($certificate.Subject)"
Write-Host "  Huella:     $($certificate.Thumbprint)"
Write-Host "  Caducidad:  $($certificate.NotAfter.ToString('u'))"
Write-Host "  Clave:      no exportable; permanece en este usuario y equipo"
Write-Host ""
Write-Host "Para generar un paquete firmado:"
Write-Host ".\packaging\build_distribution.ps1 -SigningCertificateThumbprint $($certificate.Thumbprint)"
