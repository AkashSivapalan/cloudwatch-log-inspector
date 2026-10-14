resource "aws_ecr_repository" "app" {
  name                 = local.name
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 10 most recent images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 10 }
      action       = { type = "expire" }
    }]
  })
}

locals {
  image_uri = "${aws_ecr_repository.app.repository_url}:${var.image_tag}"

  common_env = {
    LOG_INSPECTOR_ENVIRONMENT                    = "production"
    LOG_INSPECTOR_LOG_FORMAT                     = "json"
    LOG_INSPECTOR_AWS_REGION                     = var.region
    LOG_INSPECTOR_DATABASE_SECRET_ARN            = aws_db_instance.main.master_user_secret[0].secret_arn
    LOG_INSPECTOR_DATABASE_HOST                  = aws_db_instance.main.endpoint
    LOG_INSPECTOR_DATABASE_NAME                  = aws_db_instance.main.db_name
    LOG_INSPECTOR_ANTHROPIC_API_KEY_PARAMETER    = aws_ssm_parameter.anthropic_api_key.name
    LOG_INSPECTOR_INVESTIGATION_QUEUE_URL        = aws_sqs_queue.investigations.url
    LOG_INSPECTOR_RECOVER_INTERRUPTED_ON_STARTUP = "false"
    LOG_INSPECTOR_RUN_MIGRATIONS_ON_STARTUP      = "false"
    LOG_INSPECTOR_CORS_ORIGINS                   = join(",", var.cors_origins)
    LOG_INSPECTOR_COGNITO_USER_POOL_ID           = aws_cognito_user_pool.main.id
    LOG_INSPECTOR_COGNITO_CLIENT_ID              = aws_cognito_user_pool_client.web.id
  }

  functions = {
    api = {
      handler = "log_inspector.lambda_handlers.api"
      timeout = 30 # API Gateway stops waiting at 30 seconds anyway
      memory  = 1024
    }
    worker = {
      handler = "log_inspector.lambda_handlers.worker"
      timeout = 900 # Insights queries plus a Claude call; the worker stops Claude 30s before this
      memory  = 1024
    }
    admin = {
      handler = "log_inspector.lambda_handlers.admin"
      timeout = 120
      memory  = 512
    }
  }
}

resource "aws_cloudwatch_log_group" "functions" {
  for_each          = local.functions
  name              = "/aws/lambda/${local.name}-${each.key}"
  retention_in_days = 30
}

resource "aws_lambda_function" "app" {
  for_each = local.functions

  function_name = "${local.name}-${each.key}"
  role          = aws_iam_role.service.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  architectures = ["x86_64"]
  timeout       = each.value.timeout
  memory_size   = each.value.memory

  image_config {
    command = [each.value.handler]
  }

  vpc_config {
    subnet_ids         = aws_subnet.private[*].id
    security_group_ids = [aws_security_group.lambda.id]
  }

  environment {
    variables = local.common_env
  }

  depends_on = [aws_cloudwatch_log_group.functions, aws_iam_role_policy_attachment.vpc_access]
}

# One investigation per invocation, so a slow one doesn't hold up others.
resource "aws_lambda_event_source_mapping" "worker" {
  event_source_arn = aws_sqs_queue.investigations.arn
  function_name    = aws_lambda_function.app["worker"].arn
  batch_size       = 1
  scaling_config {
    maximum_concurrency = 5 # at most 5 investigations at once, for cost
  }
}
