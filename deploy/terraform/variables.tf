variable "region" {
  description = "Sydney, deliberately: the banks are here, and so is the latency budget (ADR-0006)."
  type        = string
  default     = "ap-southeast-2"
}

variable "name" {
  description = "Base name for every resource."
  type        = string
  default     = "rateradar"
}

variable "database_url" {
  description = <<-EOT
    Neon connection string. Passed once at apply time into SSM Parameter Store
    as a SecureString; never written to the Lambda's environment, so it does not
    show up in the console or in `aws lambda get-function-configuration`.
    Supply it via TF_VAR_database_url, not a .tfvars file that could be committed.
  EOT
  type        = string
  sensitive   = true
}

variable "user_agent" {
  description = "Identifies this collector to the banks. Must carry a real contact URL."
  type        = string
}

variable "alarm_email" {
  description = "Optional address for failure alerts. Empty disables the alarm."
  type        = string
  default     = ""
}

variable "github_repository" {
  description = "owner/repo allowed to deploy via OIDC. Empty disables the deploy role."
  type        = string
  default     = ""
}

variable "memory_mb" {
  description = "512MB is ample: the work is network-bound, not compute-bound."
  type        = number
  default     = 512
}

variable "timeout_seconds" {
  description = <<-EOT
    A full 20-brand run takes ~5 minutes from Sydney. 900s is Lambda's ceiling
    and leaves generous headroom; if a run ever approaches it, the fix is one
    invocation per brand rather than a larger number here.
  EOT
  type        = number
  default     = 900
}

variable "log_retention_days" {
  description = "Long enough to investigate, short enough to stay free."
  type        = number
  default     = 14
}

variable "create_github_oidc_provider" {
  description = <<-EOT
    Whether to create the account-wide GitHub OIDC provider. True for a fresh
    account. Set false if this account already trusts GitHub for another
    project, in which case the existing provider is looked up instead --
    creating a second one fails, since AWS allows only one per issuer URL.
  EOT
  type        = bool
  default     = true
}
