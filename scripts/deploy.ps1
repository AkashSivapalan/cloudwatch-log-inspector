<#
.SYNOPSIS
  Build the Lambda image, push it to ECR, apply the Terraform, and run database migrations.

.DESCRIPTION
  Needs: Docker Desktop running, Terraform, the AWS CLI, and an AWS profile allowed to create
  the infrastructure (default "admin"). Terraform shows its plan and asks for "yes" before
  changing anything. The image is tagged with the current git commit, so commit first.

.EXAMPLE
  .\scripts\deploy.ps1
  .\scripts\deploy.ps1 -AwsProfile admin -Region us-east-1
#>
param(
    [string]$AwsProfile = "admin",
    [string]$Region = "us-east-1"
)
$ErrorActionPreference = "Stop"
$root = Split-Path $PSScriptRoot -Parent
$tf = Join-Path $root "infra/terraform"
$tfVars = @("-var", "aws_profile=$AwsProfile", "-var", "region=$Region")

function Invoke-Checked([scriptblock]$Command) {
    & $Command
    if ($LASTEXITCODE -ne 0) { throw "Command failed (exit code $LASTEXITCODE)." }
}

if (git -C $root status --porcelain) {
    throw "Commit or stash your changes first: the image is tagged with the current commit."
}
$tag = (git -C $root rev-parse --short HEAD).Trim()
Write-Host "Deploying commit $tag to $Region with profile '$AwsProfile'." -ForegroundColor Cyan

Write-Host "`n[1/4] Container registry (ECR)" -ForegroundColor Cyan
Invoke-Checked { terraform "-chdir=$tf" init -input=false }
Invoke-Checked { terraform "-chdir=$tf" apply -input=false -target="aws_ecr_repository.app" @tfVars }
$repo = (terraform "-chdir=$tf" output -raw ecr_repository_url).Trim()

Write-Host "`n[2/4] Build and push the image ${repo}:$tag" -ForegroundColor Cyan
# Counts matching images instead of asking for one: a "not found" error would stop the script
# (Windows PowerShell treats a native command's redirected stderr as a terminating error).
$existing = aws ecr list-images --repository-name ($repo.Split("/")[-1]) `
    --query "length(imageIds[?imageTag=='$tag'])" --output text `
    --profile $AwsProfile --region $Region
if ($LASTEXITCODE -ne 0) { throw "Could not list images in $repo." }
if ([int]$existing -gt 0) {
    Write-Host "Image $tag is already in ECR; skipping the build."
} else {
    $registry = $repo.Split("/")[0]
    # Piped through cmd.exe: Windows PowerShell re-encodes text piped between native
    # programs, which corrupts the token (ECR answers 400 Bad Request).
    Invoke-Checked { cmd /c "aws ecr get-login-password --profile $AwsProfile --region $Region | docker login --username AWS --password-stdin $registry" }
    Invoke-Checked { docker build --platform linux/amd64 -f (Join-Path $root "Dockerfile.lambda") -t "${repo}:$tag" $root }
    Invoke-Checked { docker push "${repo}:$tag" }
}

Write-Host "`n[3/4] Infrastructure (review the plan, then type yes)" -ForegroundColor Cyan
Invoke-Checked { terraform "-chdir=$tf" apply -input=false @tfVars -var "image_tag=$tag" }

Write-Host "`n[4/4] Database migrations" -ForegroundColor Cyan
& (Join-Path $PSScriptRoot "admin.ps1") migrate -AwsProfile $AwsProfile -Region $Region

Write-Host "`nDeployed." -ForegroundColor Green
Write-Host "API:                     $((terraform "-chdir=$tf" output -raw api_url).Trim())"
Write-Host "Inspector principal ARN: $((terraform "-chdir=$tf" output -raw inspector_principal_arn).Trim())"
