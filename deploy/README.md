# Deploying RateRadar to AWS (Sydney)

Why here and not GitHub Actions: [ADR-0006](../docs/adr/0006-collect-from-sydney-on-lambda.md).

```
EventBridge Scheduler  ──►  Lambda (ap-southeast-2)  ──►  Neon Postgres
  06:17 / 18:17 Sydney        rateradar.handler            (also Sydney)
  Mondays: discover           900s timeout, 512MB
                                   │
                              CloudWatch logs + 2 alarms ──► SNS ──► email
```

## Running it locally

The collector is a CLI, so the fastest loop stays what it always was:

```bash
make db-up && rateradar migrate && rateradar collect --brand <id>
```

That exercises the collector. It does not exercise the *Lambda* — the handler,
the Parameter Store lookup, the S3 write, the packaged zip actually importing on
Linux. [Floci](https://floci.io/) covers that gap: an AWS emulator that runs
Lambda in real Docker containers.

```bash
curl -fsSL https://floci.io/install.sh | sh     # once
make local-up                                   # Floci + Postgres + deploy + migrate
make local-invoke CMD=collect                   # a real collection pass, locally
make local-invoke CMD=backup                    # writes to the emulated bucket
make local-logs
make local-down
```

`make local-up` is idempotent; run it again after changing code, or
`make local-deploy` to skip straight to updating the function.

Two details that make this worth having rather than a toy:

- **The function reads its connection string from Parameter Store locally too.**
  Testing a different code path than production runs is how you ship a broken
  one — the SSM lookup, the caching, the whole path runs here.
- **Inside a Lambda container, `localhost` is that container.** The database and
  the emulator are on the host, so the function is configured with
  `host.docker.internal`. On Linux without Docker Desktop, set
  `RATERADAR_LOCAL_HOST_ALIAS=172.17.0.1`.

The real Terraform is deliberately *not* run against Floci: it would share the
state file with production, and Terraform would then believe the real function
had been replaced by an emulated one. The local stack needs four resources, and
a short script is easier to trust than a second state file is to keep honest.

Note that `collect` talks to real banks even locally — the same politeness rules
apply, so don't loop it.

## First deploy

Prerequisites: an AWS account, Terraform ≥ 1.6, the AWS CLI configured, and
your Neon connection string.

**0. Check your Neon region first.** If the database is not in
`ap-southeast-2`, move it now while the dataset is small — otherwise you have
moved compute to Sydney and left the data in Virginia. Neon's region is fixed
at project creation, so moving means a new project plus `pg_dump | psql`.

```bash
cd deploy/terraform

export TF_VAR_database_url='postgresql://...'          # not in a .tfvars file
export TF_VAR_user_agent='RateRadar/0.1 (+https://github.com/YOU/rateradar)'
export TF_VAR_github_repository='YOU/rateradar'        # enables the deploy role
export TF_VAR_alarm_email='you@example.com'            # optional but recommended

# Only if this AWS account ALREADY has a GitHub OIDC provider from another
# project -- AWS permits one per issuer, so a second create fails:
# export TF_VAR_create_github_oidc_provider=false

terraform init
terraform plan          # read it: this is the only thing standing between you and IAM
terraform apply
```

Then wire up deployment:

1. `terraform output deploy_role_arn`
2. GitHub → Settings → Secrets → Actions → new secret `AWS_DEPLOY_ROLE` with that ARN
3. Copy `deploy/github-workflow-deploy.yml` to `.github/workflows/deploy.yml`, commit, push

The push builds the package, updates the function and runs a migrate-only smoke
test. Confirm the SNS subscription email or the alarms go nowhere.

## First run

```bash
aws lambda invoke --function-name rateradar \
  --payload '{"command":"collect"}' --cli-binary-format raw-in-base64-out \
  /dev/stdout

aws logs tail /aws/lambda/rateradar --follow
```

## Operating it

| Task | Command |
| --- | --- |
| Run now | `aws lambda invoke --function-name rateradar --payload '{"command":"collect"}' --cli-binary-format raw-in-base64-out /dev/stdout` |
| Refresh brands | same, with `{"command":"discover"}` |
| Watch logs | `aws logs tail /aws/lambda/rateradar --follow` |
| Inspect data | `rateradar runs` / `rateradar health` locally, pointed at the same database |
| Pause collection | disable the schedules in EventBridge, or `terraform destroy -target=aws_scheduler_schedule.collect` |
| Back up now | `aws lambda invoke --function-name rateradar --payload '{"command":"backup"}' --cli-binary-format raw-in-base64-out /dev/stdout` |
| List backups | `aws s3 ls s3://$(terraform -chdir=deploy/terraform output -raw backup_bucket)/backups/ --recursive` |
| Restore | see [`docs/operations.md`](../docs/operations.md#restoring-from-backup) |

The CLI still works locally against the same database, which is where you read
the ledger and investigate. Lambda runs the pipeline; it is not where you debug it.

## Cost

Near zero, and structurally so: Lambda's free tier is perpetual and this uses a
couple of percent of it, standard SSM parameters are free, and ten CloudWatch
alarms are free. The one eventual line item is S3: its free tier lasts only 12
months, after which a year of weekly dumps costs a few cents a month. That is
the cheapest insurance in this project. The things that could cost money — a runaway retry loop, log
retention forever — are bounded by `maximum_retry_attempts = 1`, a 900s
timeout, and 14-day log retention.

Set a billing alarm anyway. Free tiers are free until they are not.
