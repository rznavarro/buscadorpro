# Arma descargas/VortexiaProspector-Windows.zip (el archivo del botón "Descargar para Windows")
# con lo que está en el último commit. Uso, después de hacer commit de los cambios:
#   powershell -ExecutionPolicy Bypass -File tools\armar-descarga.ps1
#   git add descargas; git commit -m "Descarga para Windows actualizada"; git push
# Deja fuera lo marcado con export-ignore en .gitattributes (tests, notas de desarrollo).
Set-Location (Join-Path $PSScriptRoot "..")
New-Item -ItemType Directory -Force descargas | Out-Null
git -c core.autocrlf=false archive --format=zip --prefix=VortexiaProspector/ -o descargas/VortexiaProspector-Windows.zip HEAD
Write-Host "Listo: descargas\VortexiaProspector-Windows.zip ($([math]::Round((Get-Item descargas\VortexiaProspector-Windows.zip).Length / 1KB)) KB)"
