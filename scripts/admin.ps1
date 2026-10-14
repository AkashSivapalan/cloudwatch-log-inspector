<#
.SYNOPSIS
  Run a log-inspector admin command on the deployed service (via the admin Lambda function).

.DESCRIPTION
  The database is only reachable inside the VPC, so admin commands run in Lambda. The output
  (which can include a new API key) comes back to this terminal only; it isn't logged.

.EXAMPLE
  .\scripts\admin.ps1 create-user akash
  .\scripts\admin.ps1 list-users
  .\scripts\admin.ps1 create-key <user-id> --name postman
  .\scripts\admin.ps1 revoke-key <key-id>
#>
param(
    [Parameter(Mandatory, Position = 0, ValueFromRemainingArguments)]
    [string[]]$Command,
    [string]$AwsProfile = "admin",
    [string]$Region = "us-east-1"
)
$ErrorActionPreference = "Stop"
$tf = Join-Path (Split-Path $PSScriptRoot -Parent) "infra/terraform"
$function = (terraform "-chdir=$tf" output -raw admin_function_name).Trim()

$payload = New-TemporaryFile
$response = New-TemporaryFile
try {
    # Written without a BOM: the AWS CLI rejects JSON that starts with one.
    [System.IO.File]::WriteAllText($payload, (@{ args = $Command } | ConvertTo-Json -Compress))
    aws lambda invoke --function-name $function --payload "fileb://$payload" `
        --profile $AwsProfile --region $Region $response | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Invoking $function failed." }

    $result = Get-Content $response -Raw | ConvertFrom-Json
    if ($null -ne $result.errorMessage) { throw "The admin function failed: $($result.errorMessage)" }
    Write-Host $result.output
    if ($result.exit_code -ne 0) { exit $result.exit_code }
} finally {
    # The response may contain an API key; don't leave it on disk.
    Remove-Item $payload, $response -ErrorAction SilentlyContinue
}
