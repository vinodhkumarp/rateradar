output "function_name" {
  value       = aws_lambda_function.collector.function_name
  description = "Pass to `aws lambda invoke` and to the deploy workflow."
}

output "log_group" {
  value       = aws_cloudwatch_log_group.collector.name
  description = "aws logs tail <this> --follow"
}

output "deploy_role_arn" {
  value       = try(aws_iam_role.deploy[0].arn, null)
  description = "Set as the AWS_DEPLOY_ROLE secret in GitHub."
}

output "schedules" {
  value = {
    collect  = aws_scheduler_schedule.collect.schedule_expression
    discover = aws_scheduler_schedule.discover.schedule_expression
  }
}

output "backup_bucket" {
  value       = aws_s3_bucket.backups.bucket
  description = "aws s3 ls s3://<this>/backups/ --recursive"
}
