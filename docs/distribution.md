# Windows distribution guide

The build script creates a portable Windows directory and ZIP containing the application, Python runtime, dependencies, default configuration, documentation, VB-CABLE installer, and third-party license notices.

## Build the package

From an activated Windows environment:

```powershell
.\packaging\build_distribution.ps1
```

The first build can install the optional PyInstaller dependency. If it is already installed, use:

```powershell
.\packaging\build_distribution.ps1 -SkipDependencyInstall
```

Results are written to `artifacts/distribution/`. The build creates the unpacked application, a `win64` ZIP, and its SHA-256 checksum. It excludes `.env`, private keys, logs, metrics, and local preferences.

## Optional self-signed package

A self-signed certificate can establish a stable identity for controlled internal testing. It does not provide public trust, bypass Microsoft Defender policy, or replace approval from the receiving organization.

Create or reuse the Twin Tongue certificate in the current Windows user's certificate store:

```powershell
.\tools\create_signing_certificate.ps1
```

The private RSA key is non-exportable and remains in the Windows certificate store. The script prints the certificate thumbprint. Supply it explicitly:

```powershell
.\packaging\build_distribution.ps1 `
  -SigningCertificateThumbprint CERTIFICATE_THUMBPRINT
```

The build signs unsigned executables, DLLs, Python extensions, and scripts with SHA-256. Existing third-party signatures remain unchanged. Only the public certificate is exported to `CERTIFICATE/Twin-Tongue.cer`.

## Recipient instructions

1. Extract the ZIP completely to a writable directory. Do not run the executable from inside the ZIP.
2. If the package contains `CERTIFICATE/`, verify the public certificate thumbprint through an independent channel.
3. Only for an approved controlled environment, open PowerShell as administrator and run `CERTIFICATE/INSTALL_CERTIFICATE.ps1`.
4. Copy `env.example` to `.env`. For the shipped Realtime default, enter
   `OPENAI_API_KEY`. Add ElevenLabs and Google Cloud Translation credentials only
   if a direction is configured as Classic. Never share this file.
5. Install and validate CABLE A and CABLE B by following `VB-CABLE/SETUP.md`.
6. Run `Twin-Tongue.exe` and keep its console window open.
7. Open `http://127.0.0.1:8765` on the same computer.
8. Stop the application with `Ctrl+C`.
9. If the test computer should no longer trust future builds with the same identity, run `CERTIFICATE/REMOVE_CERTIFICATE.ps1` as administrator.

Twin Tongue sends audio and text to external providers for transcription, translation, and voice synthesis. Inform call participants and follow applicable privacy, consent, data-handling, and organizational policies.

The shipped configuration uses OpenAI Realtime in both directions but starts in
passthrough. Enabling translation does not open an API session until a call
application is detected on the relevant cable. The diagnostic configuration
controls whether Realtime writes `captured`, `accepted`, `sent`, `received`, and
`played`
audio plus per-call manifests below its configured local directory. Classic
diagnostic recording follows its own configuration. Diagnostic artifacts are
excluded from packages; review privacy and retention requirements before
enabling them on a recipient system.
