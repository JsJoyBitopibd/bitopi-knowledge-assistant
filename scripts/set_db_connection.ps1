<#
Points the two SQL Server connections in .env (SQLSERVER_CONN_BITOPISPLINT and SQLSERVER_CONN_PRODUCTION) at a server
and login. Every other database's catalog reuses one of the two (connection_database:), and every other line in .env
is left as it is. The password is typed at a hidden prompt and written only to .env: run this yourself, nobody
else needs to see it. Then check with:  docker compose run --rm --no-deps --entrypoint python app scripts/db_ping.py

  pwsh -File scripts\set_db_connection.ps1 -Server 172.16.7.14 -User mis-joy
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,     # host, or host\instance for a named instance
    [Parameter(Mandatory = $true)][string]$User
)
$ErrorActionPreference = "Stop"
$envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"

$secure = Read-Host "Password for $User on $Server" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $password) { throw "No password given; .env not changed." }
if ($password.Contains('$')) {
    Write-Warning "The password contains '$'. docker compose may read it as a variable; if the check fails, change the password or ask for help."
}
# ODBC: a value holding ';' or braces goes inside {…}, with '}' doubled
if ($password -match '[;{}]') { $password = '{' + $password.Replace('}', '}}') + '}' }

$lines = @()
if (Test-Path $envFile) { $lines = @([IO.File]::ReadAllLines($envFile)) }
foreach ($pair in @(@("SQLSERVER_CONN_BITOPISPLINT", "BitopiSplint"), @("SQLSERVER_CONN_PRODUCTION", "Production"))) {
    $name, $database = $pair
    # drop every earlier copy of the line, including commented-out or mistyped "NAME =" forms
    $lines = @($lines | Where-Object { $_ -notmatch "^\s*#?\s*$name\s*=" })
    $lines += "$name=Driver={ODBC Driver 18 for SQL Server};Server=$Server;Database=$database;UID=$User;PWD=$password;" +
              "Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly"
}
[IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))   # UTF-8, no BOM
$password = $null
Write-Host "Updated SQLSERVER_CONN_BITOPISPLINT and SQLSERVER_CONN_PRODUCTION in .env -> $Server as $User."
Write-Host "The running app keeps its old settings until it is restarted."
