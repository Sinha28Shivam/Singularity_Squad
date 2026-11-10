<#
.SYNOPSIS
    Back up the WorkplaceOps MongoDB database to a timestamped gzip archive (mongodump wrapper).
.DESCRIPTION
    Reads MONGODB_URI (default mongodb://localhost:27017/) and MONGODB_DATABASE (default workplaceops) from the
    environment. The URI is never printed. Requires MongoDB Database Tools (mongodump) on PATH.
.EXAMPLE
    .\scripts\backup_mongo.ps1                      # -> backups\workplaceops-20261109-112540.archive.gz
#>
[CmdletBinding()]
param(
    [string]$OutputDir = "backups",
    [string]$Database = $(if ($env:MONGODB_DATABASE) { $env:MONGODB_DATABASE } else { "workplaceops" })
)
$ErrorActionPreference = "Stop"

function Write-MongoConfig([string]$Dir) {
    # The URI goes into a short-lived config file so it never appears in output or the process list.
    $uri = if ($env:MONGODB_URI) { $env:MONGODB_URI } else { "mongodb://localhost:27017/" }
    $path = Join-Path $Dir (".mongo-tools-{0}.yaml" -f [guid]::NewGuid().ToString("N"))
    Set-Content -LiteralPath $path -Value ("uri: '{0}'" -f $uri.Replace("'", "''")) -Encoding ascii -NoNewline
    return $path
}

if (-not (Get-Command mongodump -ErrorAction SilentlyContinue)) {
    throw "mongodump not found. Install MongoDB Database Tools and add them to PATH."
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$archive = Join-Path $OutputDir ("{0}-{1}.archive.gz" -f $Database, (Get-Date -Format "yyyyMMdd-HHmmss"))
$config = Write-MongoConfig $OutputDir
try {
    & mongodump --config=$config --db=$Database --archive=$archive --gzip
    if ($LASTEXITCODE -ne 0) { throw "mongodump failed (exit code $LASTEXITCODE)." }
} finally {
    Remove-Item -LiteralPath $config -Force -ErrorAction SilentlyContinue
}
$size = (Get-Item -LiteralPath $archive).Length
Write-Host ("Backup written: {0} ({1:N0} bytes)" -f $archive, $size)
