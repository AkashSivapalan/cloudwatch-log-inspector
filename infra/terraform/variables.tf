variable "region" {
  description = "AWS region for the service."
  type        = string
  default     = "us-east-1"
}

variable "aws_profile" {
  description = "AWS CLI profile Terraform uses. Needs permission to create every resource here."
  type        = string
  default     = "admin"
}

variable "image_tag" {
  description = "Tag of the Lambda image in ECR (scripts/deploy.ps1 sets it to the git commit)."
  type        = string
  default     = "latest"
}

variable "db_instance_class" {
  description = "RDS instance size. db.t4g.micro is free-tier eligible on new accounts."
  type        = string
  default     = "db.t4g.micro"
}

variable "nat_instance_type" {
  description = "Instance type for the fck-nat NAT instance."
  type        = string
  default     = "t4g.nano"
}

variable "api_throttle_rate" {
  description = "API Gateway steady-state requests per second, for all users together."
  type        = number
  default     = 20
}

variable "api_throttle_burst" {
  description = "API Gateway burst limit."
  type        = number
  default     = 40
}

variable "cors_origins" {
  description = "Browser origins allowed to call the API: the frontend's CloudFront URL, plus localhost for development."
  type        = list(string)
  default     = ["https://dgc5idmxco766.cloudfront.net", "http://localhost:4200"]
}
