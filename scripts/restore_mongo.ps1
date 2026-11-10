<#
.SYNOPSIS
    Restore a WorkplaceOps MongoDB gzip archive created by backup_mongo.ps1 (mongorestore wrapper).
.DESCRIPTION
    Reads MONGODB_URI (default mongodb://localhost:27017/) and MONGODB_DATABASE (default workplaceops) from the
    environment. The URI is never printed. Requires MongoDB Database Tools (mongorestore) on PATH.
    -Drop replaces existing collections (asks for confirmation unless -Confirm:$false).
.EXAMPLE
    .\scripts\restore_mongo.ps1 -Archive backups\workplaceops-20261109-112540.archive.gz
.EXAMPLE
    .\scripts\restore_mongo.ps1 -Archive backups\workplaceops-20261109-112540.archive.gz -Drop
#>
[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "Medium")]
param(
    [Parameter(Mandatory = $true)][string]$Archive,
    [string]$Database = $(if ($env:MONGODB_DATABASE) { $env:MONGODB_DATABASE } else { "workplaceops" }),
    [switch]$Drop
)
$ErrorActionPreference = "Stop"

function Write-MongoConfig([string]$Dir) {
    # The URI goes into a short-lived config file so it never appears in output or the process list.
    $uri = if ($env:MONGODB_URI) { $env:MONGODB_URI } else { "mongodb://localhost:27017/" }
    $path = Join-Path $Dir (".mongo-tools-{0}.yaml" -f [guid]::NewGuid().ToString("N"))
    Set-Content -LiteralPath $path -Value ("uri: '{0}'" -f $uri.Replace("'", "''")) -Encoding ascii -NoNewline
    return $path
}

if (-not (Get-Command mongorestore -ErrorAction SilentlyContinue)) {
    throw "mongorestore not found. Install MongoDB Database Tools and add them to PATH."
}
if (-not (Test-Path -LiteralPath $Archive -PathType Leaf)) { throw "Archive not found: $Archive" }

$action = if ($Drop) { "Restore (dropping existing collections first)" } else { "Restore (keep existing documents)" }
if (-not $PSCmdlet.ShouldProcess("database '$Database'", $action)) { return }

$restoreArgs = @("--archive=$Archive", "--gzip", "--nsInclude=$Database.*")
if ($Drop) { $restoreArgs += "--drop" }
$config = Write-MongoConfig (Split-Path -Parent (Resolve-Path -LiteralPath $Archive))
try {
    & mongorestore --config=$config @restoreArgs
    if ($LASTEXITCODE -ne 0) { throw "mongorestore failed (exit code $LASTEXITCODE)." }
} finally {
    Remove-Item -LiteralPath $config -Force -ErrorAction SilentlyContinue
}
Write-Host "Restore complete: $Archive -> $Database"
