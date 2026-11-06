$ErrorActionPreference = 'Stop'
$taskProject = [IO.Path]::GetFullPath($PSScriptRoot)
$taskEnvironment = [IO.Path]::GetFullPath((Join-Path $taskProject '.venv'))
$taskBackup = [IO.Path]::GetFullPath((Join-Path $taskProject ('.venv_anterior_' + [guid]::NewGuid().ToString('N'))))
$taskPrefix = $taskProject.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
foreach ($taskTarget in @($taskEnvironment, $taskBackup)) {
    if (-not $taskTarget.StartsWith($taskPrefix, [StringComparison]::OrdinalIgnoreCase)) {
        throw 'El destino del entorno queda fuera de la carpeta del proyecto.'
    }
}
$taskInfo = Get-Item -LiteralPath $taskEnvironment -Force
if (($taskInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -ne 0) {
    throw 'El entorno .venv es un enlace o junction; no se modificara.'
}
Move-Item -LiteralPath $taskEnvironment -Destination $taskBackup
Write-Output ('Entorno anterior conservado en: ' + $taskBackup)
