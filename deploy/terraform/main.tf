data "aws_caller_identity" "current" {}

# ---------------------------------------------------------------------------
# Secret. Parameter Store rather than Secrets Manager: standard parameters are
# free, Secrets Manager is $0.40/secret/month, and this project's entire budget
# is zero. Rotation is not a requirement for a Neon connection string that only
# this function uses.
# ---------------------------------------------------------------------------
resource "aws_ssm_parameter" "database_url" {
  name        = "/${var.name}/database_url"
  description = "Neon connection string for the RateRadar collector"
  type        = "SecureString"
  value       = var.database_url

  lifecycle {
    # Rotating the credential out of band should not be reverted by the next apply.
    ignore_changes = [value]
  }
}

# ---------------------------------------------------------------------------
# Function
# ---------------------------------------------------------------------------
data "archive_file" "placeholder" {
  type        = "zip"
  output_path = "${path.module}/.placeholder.zip"

  source {
    filename = "placeholder.txt"
    content  = "Replaced on first deploy by deploy/build_package.sh."
  }
}

resource "aws_lambda_function" "collector" {
  function_name = var.name
  description   = "Collects Australian CDR banking product data (ADR-0006)"
  role          = aws_iam_role.lambda.arn
  handler       = "rateradar.handler.handler"
  runtime       = "python3.12"
  architectures = ["x86_64"]
  memory_size   = var.memory_mb
  timeout       = var.timeout_seconds

  # Terraform owns the infrastructure; the deploy workflow owns the code. Without
  # this, every apply would roll the function back to whatever zip Terraform last saw.
  filename = data.archive_file.placeholder.output_path
  lifecycle {
    ignore_changes = [filename, source_code_hash]
  }

  environment {
    variables = {
      RATERADAR_MIGRATIONS_DIR       = "/var/task/migrations"
      RATERADAR_BRAND_ALLOWLIST_FILE = "/var/task/config/brands.allowlist"
      RATERADAR_USER_AGENT           = var.user_agent
      # The connection string is read from Parameter Store at cold start, not
      # injected here, so it never appears in the function's configuration.
      RATERADAR_DATABASE_URL_PARAM = aws_ssm_parameter.database_url.name
      RATERADAR_BACKUP_BUCKET      = aws_s3_bucket.backups.bucket
      PYTHONUNBUFFERED             = "1"
    }
  }

  depends_on = [aws_cloudwatch_log_group.collector]
}

resource "aws_cloudwatch_log_group" "collector" {
  name              = "/aws/lambda/${var.name}"
  retention_in_days = var.log_retention_days
}

# ---------------------------------------------------------------------------
# Execution role: logs, and read access to exactly one parameter.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name               = "${var.name}-lambda"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

data "aws_iam_policy_document" "lambda" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.collector.arn}:*"]
  }

  statement {
    sid       = "ReadDatabaseUrl"
    actions   = ["ssm:GetParameter"]
    resources = [aws_ssm_parameter.database_url.arn]
  }

  statement {
    sid       = "DecryptParameter"
    actions   = ["kms:Decrypt"]
    resources = ["arn:aws:kms:${var.region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
  }

  statement {
    sid     = "WriteBackups"
    actions = ["s3:PutObject"]
    # Write only. The function has no reason to read or delete a backup, and a
    # collector that cannot delete its own backups is one fewer way to lose them.
    resources = ["${aws_s3_bucket.backups.arn}/backups/*"]
  }
}

resource "aws_iam_role_policy" "lambda" {
  name   = "${var.name}-lambda"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda.json
}

# ---------------------------------------------------------------------------
# Backups. The collected history cannot be re-gathered retrospectively -- a
# month of rate movements exists nowhere else once Neon loses it -- so a second
# copy is not optional, whatever the database tier promises.
# ---------------------------------------------------------------------------
resource "aws_s3_bucket" "backups" {
  bucket = "${var.name}-backups-${data.aws_caller_identity.current.account_id}"
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "backups" {
  bucket = aws_s3_bucket.backups.id

  versioning_configuration {
    # Protects against a bad backup overwriting a good one, and against a
    # delete -- accidental or otherwise.
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  depends_on = [aws_s3_bucket_versioning.backups]

  rule {
    id     = "expire-old-backups"
    status = "Enabled"

    filter {}

    expiration {
      days = var.backup_retention_days
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# ---------------------------------------------------------------------------
# Schedules, expressed in Sydney time so they stay put across daylight saving.
# ---------------------------------------------------------------------------
data "aws_iam_policy_document" "scheduler_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
}

resource "aws_iam_role_policy" "scheduler" {
  name = "${var.name}-scheduler"
  role = aws_iam_role.scheduler.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.collector.arn
    }]
  })
}

resource "aws_scheduler_schedule" "collect" {
  name        = "${var.name}-collect"
  description = "Twice daily: 06:17 and 18:17 Sydney (see docs/data-use.md)"

  flexible_time_window {
    mode = "OFF"
  }

  # Sydney time, so the schedule stays put across daylight saving rather than
  # drifting an hour twice a year the way a UTC cron would.
  schedule_expression          = "cron(17 6,18 * * ? *)"
  schedule_expression_timezone = "Australia/Sydney"

  target {
    arn      = aws_lambda_function.collector.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ command = "collect" })

    retry_policy {
      # Runs are idempotent, so a retry is safe -- but a failed run is usually a
      # failed bank or a sleeping database, and the next scheduled run is only
      # twelve hours away. One retry, then leave everyone alone.
      maximum_retry_attempts = 1
    }
  }
}

resource "aws_scheduler_schedule" "discover" {
  name        = "${var.name}-discover"
  description = "Weekly refresh of the brand registry from the CDR Register"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(23 5 ? * MON *)"
  schedule_expression_timezone = "Australia/Sydney"

  target {
    arn      = aws_lambda_function.collector.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ command = "discover" })

    retry_policy {
      maximum_retry_attempts = 1
    }
  }
}

resource "aws_scheduler_schedule" "backup" {
  name        = "${var.name}-backup"
  description = "Weekly dump of every table to S3"

  flexible_time_window {
    mode = "OFF"
  }

  schedule_expression          = "cron(41 3 ? * SUN *)"
  schedule_expression_timezone = "Australia/Sydney"

  target {
    arn      = aws_lambda_function.collector.arn
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ command = "backup" })

    retry_policy {
      maximum_retry_attempts = 2
    }
  }
}

# ---------------------------------------------------------------------------
# Alerting. Without this, the failure mode is silence: the collector stops and
# nobody notices for weeks, which is the one outcome this project cannot
# survive. Two alarms, both inside the free allowance of ten.
# ---------------------------------------------------------------------------
resource "aws_sns_topic" "alerts" {
  count = var.alarm_email == "" ? 0 : 1
  name  = "${var.name}-alerts"
}

resource "aws_sns_topic_subscription" "email" {
  count     = var.alarm_email == "" ? 0 : 1
  topic_arn = aws_sns_topic.alerts[0].arn
  protocol  = "email"
  endpoint  = var.alarm_email
}

resource "aws_cloudwatch_metric_alarm" "errors" {
  count               = var.alarm_email == "" ? 0 : 1
  alarm_name          = "${var.name}-errors"
  alarm_description   = "The collector raised an unhandled error."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  dimensions          = { FunctionName = aws_lambda_function.collector.function_name }
  statistic           = "Sum"
  period              = 3600
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alerts[0].arn]
}

resource "aws_cloudwatch_metric_alarm" "not_running" {
  count             = var.alarm_email == "" ? 0 : 1
  alarm_name        = "${var.name}-not-running"
  alarm_description = "No invocation in 24h — the schedule has stopped firing."
  namespace         = "AWS/Lambda"
  metric_name       = "Invocations"
  dimensions        = { FunctionName = aws_lambda_function.collector.function_name }
  statistic         = "Sum"
  period            = 86400
  evaluation_periods = 1
  threshold         = 1
  comparison_operator = "LessThanThreshold"
  # Missing data IS the failure here: no data point means nothing ran at all.
  treat_missing_data = "breaching"
  alarm_actions      = [aws_sns_topic.alerts[0].arn]
}

# ---------------------------------------------------------------------------
# Deploy role for GitHub Actions, via OIDC: short-lived credentials, no access
# keys stored anywhere. Scoped to updating this one function's code.
# ---------------------------------------------------------------------------
# The OIDC provider is account-wide, not per project: an AWS account has at most
# one for GitHub. Create it here by default, and look up the existing one instead
# if this account already trusts GitHub for another project.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.github_repository != "" && var.create_github_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # thumbprint_list is deliberately omitted: optional since AWS provider 5.81,
  # because AWS now validates GitHub's certificate against its own trusted CA
  # library. Pinning a thumbprint here would be a hostage to GitHub's cert
  # rotation for no security gain.
}

data "aws_iam_openid_connect_provider" "github" {
  count = var.github_repository != "" && !var.create_github_oidc_provider ? 1 : 0
  url   = "https://token.actions.githubusercontent.com"
}

locals {
  gh_owner = try(split("/", var.github_repository)[0], "")
  gh_repo  = try(split("/", var.github_repository)[1], "")

  github_oidc_arn = (
    var.github_repository == "" ? null :
    var.create_github_oidc_provider
    ? aws_iam_openid_connect_provider.github[0].arn
    : data.aws_iam_openid_connect_provider.github[0].arn
  )
}

data "aws_iam_policy_document" "deploy_assume" {
  count = var.github_repository == "" ? 0 : 1

  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [local.github_oidc_arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # IAM refuses a GitHub trust policy that does not constrain "sub" or
    # "job_workflow_ref" -- a guard against roles that trust all of GitHub -- so
    # sub has to be here, and it has to match the format actually issued.
    #
    # GitHub changed the default for repositories created after 15 July 2026 to
    # embed immutable owner and repository IDs:
    #     repo:vinodhkumarp@80761765/rateradar@1368457965:ref:refs/heads/main
    # rather than the repo:owner/name:ref:... every tutorial still shows. Both
    # patterns are listed; the wildcard covers only the numeric ids, so the
    # owner, repository and branch all remain pinned.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "repo:${local.gh_owner}@*/${local.gh_repo}@*:ref:refs/heads/${var.deploy_branch}",
        "repo:${var.github_repository}:ref:refs/heads/${var.deploy_branch}",
      ]
    }

    # Belt and braces, and legible: these two say the same thing in claims that
    # are not subject to the format change above.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:repository"
      values   = [var.github_repository]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:ref"
      values   = ["refs/heads/${var.deploy_branch}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  count              = var.github_repository == "" ? 0 : 1
  name               = "${var.name}-deploy"
  description        = "Lets GitHub Actions update the collector's code, and nothing else"
  assume_role_policy = data.aws_iam_policy_document.deploy_assume[0].json
}

resource "aws_iam_role_policy" "deploy" {
  count = var.github_repository == "" ? 0 : 1
  name  = "${var.name}-deploy"
  role  = aws_iam_role.deploy[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow"
      Action = [
        "lambda:UpdateFunctionCode",
        "lambda:GetFunction",
        "lambda:GetFunctionConfiguration",
        "lambda:InvokeFunction",
      ]
      Resource = aws_lambda_function.collector.arn
    }]
  })
}
