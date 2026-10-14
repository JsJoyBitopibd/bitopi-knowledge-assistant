<#
Points the two SQL Server connections in .env (SQLSERVER_CONN_BITOPISPLINT and SQLSERVER_CONN_PRODUCTION) at a server
and login. Every other database's catalog reuses one of the two (connection_database:), and every other line in .env
is left as it is. The password is typed at a hidden prompt and written only to .env: run this yourself, nobody
else needs to see it. Then check with:  docker compose run --rm --no-deps --entrypoint python app scripts/db_ping.py

  pwsh -File scripts\set_db_connection.ps1 -Server "192.168.10.6\MSSQLSERVER2019" -User rag_reader

With -Variable and -Database it writes only that one line instead, for a database on another server (its catalog
names the variable as connection_env), leaving the two lines above untouched:

  pwsh -File scripts\set_db_connection.ps1 -Server <host> -User <login> -Variable SQLSERVER_CONN_VISTAQ -Database VistaQ_Default_RHL
#>
param(
    [Parameter(Mandatory = $true)][string]$Server,     # host, or host\instance for a named instance
    [Parameter(Mandatory = $true)][string]$User,
    [string]$Variable,                                 # e.g. SQLSERVER_CONN_VISTAQ (needs -Database)
    [string]$Database
)
$ErrorActionPreference = "Stop"
$envFile = Join-Path (Split-Path $PSScriptRoot -Parent) ".env"
if ($Variable -and -not $Database) { throw "-Variable needs -Database (the database that line opens)." }
if ($Variable -and $Variable -notmatch '^SQLSERVER_CONN_[A-Z0-9_]+$') { throw "-Variable must look like SQLSERVER_CONN_NAME." }

$secure = Read-Host "Password for $User on $Server" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try { $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr) }
finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr) }
if (-not $password) { throw "No password given; .env not changed." }
# ODBC: a value holding ';' or braces goes inside {…}, with '}' doubled
if ($password -match '[;{}]') { $password = '{' + $password.Replace('}', '}}') + '}' }
# docker compose reads `$name` in .env as a variable; `$$` is a literal `$`
$password = $password.Replace('$', '$$')

# (assigned inside each branch: an `if` used as an expression would flatten a one-pair list into its two strings)
if ($Variable) { $targets = @(, @($Variable, $Database)) }
else { $targets = @(@("SQLSERVER_CONN_BITOPISPLINT", "BitopiSplint"), @("SQLSERVER_CONN_PRODUCTION", "Production")) }
$lines = @()
if (Test-Path $envFile) { $lines = @([IO.File]::ReadAllLines($envFile)) }
foreach ($pair in $targets) {
    $name, $db = $pair
    # drop every earlier copy of the line, including commented-out or mistyped "NAME =" forms
    $lines = @($lines | Where-Object { $_ -notmatch "^\s*#?\s*$name\s*=" })
    $lines += "$name=Driver={ODBC Driver 18 for SQL Server};Server=$Server;Database=$db;UID=$User;PWD=$password;" +
              "Encrypt=yes;TrustServerCertificate=yes;ApplicationIntent=ReadOnly"
}
[IO.File]::WriteAllLines($envFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding $false))   # UTF-8, no BOM
$password = $null
Write-Host "Updated $(($targets | ForEach-Object { $_[0] }) -join ' and ') in .env -> $Server as $User."
Write-Host "The running app keeps its old settings until it is restarted."
