[CmdletBinding()]
param(
    [string]$OutputDirectory = "artifacts\distribution",
    [string]$SigningCertificateThumbprint,
    [string]$TimestampServer = "http://timestamp.digicert.com",
    [switch]$SkipDependencyInstall,
    [switch]$KeepBuildFiles
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Invoke-Checked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Executable,
        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "El comando '$Executable' termino con codigo $LASTEXITCODE."
    }
}

function Remove-SafeBuildDirectory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,
        [Parameter(Mandatory = $true)]
        [string]$AllowedRoot
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path)
    $resolvedRoot = [System.IO.Path]::GetFullPath($AllowedRoot).TrimEnd('\') + '\'
    if (-not $resolvedPath.StartsWith($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Se rechazo limpiar una ruta fuera del directorio de salida: $resolvedPath"
    }
    if (Test-Path -LiteralPath $resolvedPath) {
        Remove-Item -LiteralPath $resolvedPath -Recurse -Force
    }
}

function Get-CodeSigningCertificate {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Thumbprint
    )

    $normalizedThumbprint = ($Thumbprint -replace '[^0-9A-Fa-f]', '').ToUpperInvariant()
    if ($normalizedThumbprint.Length -ne 40) {
        throw "La huella del certificado debe contener 40 caracteres hexadecimales."
    }

    $certificatePath = "Cert:\CurrentUser\My\$normalizedThumbprint"
    if (-not (Test-Path -LiteralPath $certificatePath)) {
        throw "No se encontro el certificado de firma en $certificatePath"
    }

    $certificate = Get-Item -LiteralPath $certificatePath
    if (-not $certificate.HasPrivateKey) {
        throw "El certificado de firma no tiene una clave privada disponible."
    }
    if ($certificate.NotBefore -gt (Get-Date) -or $certificate.NotAfter -le (Get-Date)) {
        throw "El certificado de firma no esta dentro de su periodo de validez."
    }

    $codeSigningOid = "1.3.6.1.5.5.7.3.3"
    $hasCodeSigningUsage = $certificate.EnhancedKeyUsageList | Where-Object {
        $_.ObjectId -eq $codeSigningOid
    }
    if (-not $hasCodeSigningUsage) {
        throw "El certificado no permite firma de codigo."
    }

    return $certificate
}

function Set-BundleAuthenticodeSignatures {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BundlePath,
        [Parameter(Mandatory = $true)]
        [System.Security.Cryptography.X509Certificates.X509Certificate2]$Certificate,
        [Parameter(Mandatory = $true)]
        [string]$TimestampUrl
    )

    $extensions = @(".exe", ".dll", ".pyd", ".ps1")
    $files = Get-ChildItem -LiteralPath $BundlePath -Recurse -File | Where-Object {
        $extensions -contains $_.Extension.ToLowerInvariant()
    }
    $signedCount = 0
    $preservedCount = 0

    foreach ($file in $files) {
        $existingSignature = Get-AuthenticodeSignature -LiteralPath $file.FullName
        if ($null -ne $existingSignature.SignerCertificate) {
            Write-Host "Conservando firma existente: $($file.FullName)"
            $preservedCount++
            continue
        }

        Write-Host "Firmando: $($file.FullName)"
        $signature = Set-AuthenticodeSignature `
            -LiteralPath $file.FullName `
            -Certificate $Certificate `
            -HashAlgorithm SHA256 `
            -TimestampServer $TimestampUrl

        if ($null -eq $signature.SignerCertificate) {
            throw "No se pudo insertar la firma en $($file.FullName): $($signature.StatusMessage)"
        }
        if ($signature.SignerCertificate.Thumbprint -ne $Certificate.Thumbprint) {
            throw "La firma de $($file.FullName) no corresponde al certificado solicitado."
        }
        if ($signature.Status -eq "HashMismatch" -or $signature.Status -eq "NotSigned") {
            throw "La firma de $($file.FullName) no es valida: $($signature.StatusMessage)"
        }
        $signedCount++
    }

    $mainExecutable = Join-Path $BundlePath "Twin-Tongue.exe"
    $mainSignature = Get-AuthenticodeSignature -LiteralPath $mainExecutable
    if ($null -eq $mainSignature.SignerCertificate) {
        throw "El ejecutable principal ha quedado sin firma."
    }
    if ($mainSignature.SignerCertificate.Thumbprint -ne $Certificate.Thumbprint) {
        throw "El ejecutable principal no esta firmado con el certificado solicitado."
    }

    return [PSCustomObject]@{
        Signed = $signedCount
        Preserved = $preservedCount
    }
}

if ($env:OS -ne "Windows_NT") {
    throw "El paquete de Windows debe generarse desde Windows."
}

$repositoryRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $PSScriptRoot "..")
)
$signingCertificate = $null
if ($SigningCertificateThumbprint) {
    $signingCertificate = Get-CodeSigningCertificate -Thumbprint $SigningCertificateThumbprint
    Write-Host "Firma controlada habilitada: $($signingCertificate.Subject)"
    Write-Host "Huella: $($signingCertificate.Thumbprint)"
}
$python = Join-Path $repositoryRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    $pythonCommand = Get-Command python -ErrorAction Stop
    $python = $pythonCommand.Source
}

$pythonArchitecture = (& $python -c "import struct; print(struct.calcsize('P') * 8)").Trim()
if ($LASTEXITCODE -ne 0 -or $pythonArchitecture -ne "64") {
    throw "Se necesita Python de 64 bits para generar el paquete win64."
}

$version = (& $python -c "import sys; sys.path.insert(0, r'$repositoryRoot\src'); from twin_tongue_version import __version__; print(__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) {
    throw "No se pudo obtener la version de la aplicacion."
}

$outputRoot = if ([System.IO.Path]::IsPathRooted($OutputDirectory)) {
    [System.IO.Path]::GetFullPath($OutputDirectory)
} else {
    [System.IO.Path]::GetFullPath((Join-Path $repositoryRoot $OutputDirectory))
}
$packageName = "Twin-Tongue-$version-win64"
$bundleDirectory = Join-Path $outputRoot $packageName
$archivePath = Join-Path $outputRoot "$packageName.zip"
$workDirectory = Join-Path $outputRoot ".pyinstaller-work"
$stagingDirectory = Join-Path $outputRoot ".pyinstaller-dist"

New-Item -ItemType Directory -Path $outputRoot -Force | Out-Null
Remove-SafeBuildDirectory -Path $bundleDirectory -AllowedRoot $outputRoot
Remove-SafeBuildDirectory -Path $workDirectory -AllowedRoot $outputRoot
Remove-SafeBuildDirectory -Path $stagingDirectory -AllowedRoot $outputRoot
if (Test-Path -LiteralPath $archivePath) {
    Remove-Item -LiteralPath $archivePath -Force
}

if (-not $SkipDependencyInstall) {
    Write-Host "Instalando dependencias de construccion..."
    Invoke-Checked -Executable $python -Arguments @(
        "-m", "pip", "install", "-e", "${repositoryRoot}[build]"
    )
}

Write-Host "Generando ejecutable con PyInstaller..."
$pyInstallerArguments = @(
    "-m", "PyInstaller",
    "--noconfirm",
    "--clean",
    "--name", "Twin-Tongue",
    "--onedir",
    "--console",
    "--contents-directory", ".",
    "--paths", (Join-Path $repositoryRoot "src"),
    "--distpath", $stagingDirectory,
    "--workpath", $workDirectory,
    "--specpath", $workDirectory,
    "--add-data", "$(Join-Path $repositoryRoot 'config\default.toml');config",
    "--add-data", "$(Join-Path $repositoryRoot 'src\ui\index.html');ui",
    "--add-data", "$(Join-Path $repositoryRoot 'src\audio\models');audio\models",
    "--collect-all", "aec_audio_processing",
    "--exclude-module", "silero_vad",
    "--exclude-module", "torch",
    "--exclude-module", "torchaudio",
    "--exclude-module", "sqlite3",
    (Join-Path $repositoryRoot "src\main.py")
)
Invoke-Checked -Executable $python -Arguments $pyInstallerArguments

$generatedDirectory = Join-Path $stagingDirectory "Twin-Tongue"
$generatedExecutable = Join-Path $generatedDirectory "Twin-Tongue.exe"
if (-not (Test-Path -LiteralPath $generatedExecutable)) {
    throw "PyInstaller no genero el ejecutable esperado: $generatedExecutable"
}

Move-Item -LiteralPath $generatedDirectory -Destination $bundleDirectory
Copy-Item -LiteralPath (Join-Path $repositoryRoot ".env.example") -Destination (Join-Path $bundleDirectory "env.example")
Copy-Item -LiteralPath (Join-Path $repositoryRoot "README.md") -Destination (Join-Path $bundleDirectory "README.md")
Copy-Item -LiteralPath (Join-Path $repositoryRoot "docs\distribution.md") -Destination (Join-Path $bundleDirectory "DISTRIBUTION.md")
$licenseDirectory = Join-Path $bundleDirectory "THIRD_PARTY_LICENSES"
New-Item -ItemType Directory -Path $licenseDirectory | Out-Null
Copy-Item -LiteralPath (Join-Path $repositoryRoot "src\audio\models\LICENSE") -Destination (Join-Path $licenseDirectory "SILERO_VAD_LICENSE.txt")
Copy-Item -LiteralPath (Join-Path $repositoryRoot "packaging\licenses\AEC_AUDIO_PROCESSING_LICENSE.txt") -Destination (Join-Path $licenseDirectory "AEC_AUDIO_PROCESSING_LICENSE.txt")

$cableSourceDirectory = Join-Path $repositoryRoot "packaging\VBCable_A_B"
$cableGuide = Join-Path $repositoryRoot "docs\vb-cable-setup.md"
$cableInstaller = Join-Path $cableSourceDirectory "VBCABLE_A_B_Driver_Pack45.zip"
if (-not (Test-Path -LiteralPath $cableGuide) -or -not (Test-Path -LiteralPath $cableInstaller)) {
    throw "Faltan la guia o el instalador de VB-CABLE en $cableSourceDirectory"
}
$cableTargetDirectory = Join-Path $bundleDirectory "VB-CABLE"
New-Item -ItemType Directory -Path $cableTargetDirectory | Out-Null
Copy-Item -LiteralPath $cableGuide -Destination (Join-Path $cableTargetDirectory "SETUP.md")
Copy-Item -LiteralPath $cableInstaller -Destination (Join-Path $cableTargetDirectory "VBCABLE_A_B_Driver_Pack45.zip")

$signatureResult = $null
if ($null -ne $signingCertificate) {
    $certificateTargetDirectory = Join-Path $bundleDirectory "CERTIFICATE"
    New-Item -ItemType Directory -Path $certificateTargetDirectory | Out-Null
    $publicCertificatePath = Join-Path $certificateTargetDirectory "Twin-Tongue.cer"
    Export-Certificate -Cert $signingCertificate -FilePath $publicCertificatePath -Type CERT | Out-Null
    Copy-Item `
        -LiteralPath (Join-Path $repositoryRoot "packaging\certificate\INSTALL_CERTIFICATE.ps1") `
        -Destination (Join-Path $certificateTargetDirectory "INSTALL_CERTIFICATE.ps1")
    Copy-Item `
        -LiteralPath (Join-Path $repositoryRoot "packaging\certificate\REMOVE_CERTIFICATE.ps1") `
        -Destination (Join-Path $certificateTargetDirectory "REMOVE_CERTIFICATE.ps1")

    $certificateInformation = @(
        "TWIN TONGUE - SELF-SIGNED CERTIFICATE",
        "",
        "For use in approved controlled environments only.",
        "Subject: $($signingCertificate.Subject)",
        "SHA-1 thumbprint: $($signingCertificate.Thumbprint)",
        "Valid from: $($signingCertificate.NotBefore.ToString('u'))",
        "Valid until: $($signingCertificate.NotAfter.ToString('u'))",
        "",
        "Verify the thumbprint through an independent channel before installation."
    )
    Set-Content `
        -LiteralPath (Join-Path $certificateTargetDirectory "INFORMATION.txt") `
        -Value $certificateInformation `
        -Encoding utf8

    $signatureResult = Set-BundleAuthenticodeSignatures `
        -BundlePath $bundleDirectory `
        -Certificate $signingCertificate `
        -TimestampUrl $TimestampServer
}

try {
    $executableStream = [System.IO.File]::Open(
        (Join-Path $bundleDirectory "Twin-Tongue.exe"),
        [System.IO.FileMode]::Open,
        [System.IO.FileAccess]::Read,
        [System.IO.FileShare]::Read
    )
    $executableStream.Dispose()
} catch {
    throw (
        "Windows ha bloqueado la lectura del ejecutable generado. " +
        "Ejecuta la construccion en un equipo autorizado para generar binarios " +
        "o aplica la firma de codigo exigida por la organizacion. La carpeta " +
        "portatil se conserva en: $bundleDirectory"
    )
}

Write-Host "Comprimiendo paquete..."
Compress-Archive -LiteralPath $bundleDirectory -DestinationPath $archivePath -CompressionLevel Optimal
$archiveHash = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256).Hash
$hashPath = "$archivePath.sha256"
Set-Content -LiteralPath $hashPath -Value "$archiveHash  $([System.IO.Path]::GetFileName($archivePath))" -Encoding ascii

if (-not $KeepBuildFiles) {
    Remove-SafeBuildDirectory -Path $workDirectory -AllowedRoot $outputRoot
    Remove-SafeBuildDirectory -Path $stagingDirectory -AllowedRoot $outputRoot
}

Write-Host ""
Write-Host "Paquete generado correctamente:"
Write-Host "  Carpeta: $bundleDirectory"
Write-Host "  ZIP:     $archivePath"
Write-Host "  SHA256:  $archiveHash"
if ($null -ne $signatureResult) {
    Write-Host "  Firma:   $($signingCertificate.Subject)"
    Write-Host "  Huella:  $($signingCertificate.Thumbprint)"
    Write-Host "  Nuevas:  $($signatureResult.Signed) firmas; $($signatureResult.Preserved) existentes conservadas"
} else {
    Write-Host "  Firma:   SIN FIRMA"
}
Write-Host ""
Write-Host "El paquete no contiene .env, claves, logs, metricas ni preferencias locales."
