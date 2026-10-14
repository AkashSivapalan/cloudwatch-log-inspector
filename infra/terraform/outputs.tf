output "api_url" {
  description = "The service's base URL."
  value       = aws_apigatewayv2_api.main.api_endpoint
}

output "inspector_principal_arn" {
  description = "What users' role trust policies must name."
  value       = aws_iam_role.service.arn
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "admin_function_name" {
  value = aws_lambda_function.app["admin"].function_name
}

output "anthropic_api_key_parameter" {
  description = "Put your Anthropic API key here (see docs/deployment.md)."
  value       = aws_ssm_parameter.anthropic_api_key.name
}

output "dead_letter_queue_url" {
  value = aws_sqs_queue.investigations_dlq.url
}

output "cognito_user_pool_id" {
  description = "For the frontend's sign-in."
  value       = aws_cognito_user_pool.main.id
}

output "cognito_client_id" {
  description = "For the frontend's sign-in."
  value       = aws_cognito_user_pool_client.web.id
}
