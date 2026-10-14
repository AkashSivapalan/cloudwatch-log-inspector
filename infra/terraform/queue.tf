resource "aws_sqs_queue" "investigations_dlq" {
  name                      = "${local.name}-investigations-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "investigations" {
  name = "${local.name}-investigations"
  # Longer than the worker's timeout, so a running investigation isn't handed to a second
  # worker while the first is still on it.
  visibility_timeout_seconds = 960
  message_retention_seconds  = 3600

  # Messages a worker failed on twice go to the dead-letter queue; the investigation itself
  # is marked failed by the timeout check.
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.investigations_dlq.arn
    maxReceiveCount     = 2
  })
}

# The Anthropic API key. Terraform creates the parameter with a placeholder and then ignores
# its value; you set the real key yourself (see docs/deployment.md), so it never passes
# through Terraform or its state.
resource "aws_ssm_parameter" "anthropic_api_key" {
  name  = "/${local.name}/anthropic-api-key"
  type  = "SecureString"
  value = "set-me"

  lifecycle {
    ignore_changes = [value]
  }
}
