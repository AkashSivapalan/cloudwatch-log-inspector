# The service's identity: users' trust policies name this role's ARN. The API and worker
# functions share it, so there is exactly one principal to trust. No path, so the app can
# derive the ARN from its own credentials.
#
# Never delete or replace this role (including renaming it): IAM ties each user's trust policy
# to the role's internal ID, so a recreated role with the same name is a different principal
# and every user's connection would stop working until they update their role.
resource "aws_iam_role" "service" {
  name = "LogInspectorService"

  lifecycle {
    prevent_destroy = true
  }

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Network interfaces in the VPC, and the functions' own logs.
resource "aws_iam_role_policy_attachment" "vpc_access" {
  role       = aws_iam_role.service.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

resource "aws_iam_role_policy" "service" {
  name = "log-inspector"
  role = aws_iam_role.service.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AssumeUsersLogRoles"
        Effect   = "Allow"
        Action   = "sts:AssumeRole"
        Resource = "arn:aws:iam::*:role/LogInspector*"
      },
      {
        Sid      = "InvestigationQueue"
        Effect   = "Allow"
        Action   = ["sqs:SendMessage", "sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        Resource = aws_sqs_queue.investigations.arn
      },
      {
        Sid      = "DatabasePassword"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_db_instance.main.master_user_secret[0].secret_arn
      },
      {
        Sid      = "AnthropicApiKey"
        Effect   = "Allow"
        Action   = "ssm:GetParameter"
        Resource = aws_ssm_parameter.anthropic_api_key.arn
      },
    ]
  })
}
