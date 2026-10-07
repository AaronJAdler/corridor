# Infrastructure

Terraform for running Corridor on AWS: a VPC across two zones, RDS PostgreSQL 16,
ElastiCache Redis 7, and the API and worker as ECS Fargate services behind a load
balancer. The design is in [section 18 of the architecture](../docs/architecture.md).

**Nothing here is applied automatically.** No workflow runs `terraform apply`. The stack
is written to be created by hand for a demonstration and destroyed afterwards. The
simulated providers are not deployed, so deposits, withdrawals and conversions, which
each need a provider, work only if `provider_urls` names providers of your own.

## Layout

| Path | What it defines |
|---|---|
| `modules/network` | VPC, two public and two private subnets, NAT, one security group per component |
| `modules/data` | RDS PostgreSQL 16 and ElastiCache Redis 7, both private and encrypted |
| `modules/identity` | Secrets (empty), log groups, an execution role and a task role per task kind, the GitHub deploy role |
| `modules/compute` | Image repository, ECS cluster, API and worker services, the migration task definition, the load balancer |
| `main.tf`, `variables.tf`, `outputs.tf` | The wiring, the inputs with their validation, and what you need afterwards |

What can reach what:

| From | To | Port |
|---|---|---|
| Internet (`ingress_cidrs`) | Load balancer | 443, and 80 for the redirect to HTTPS |
| Load balancer | API tasks | 8000 |
| API, worker, migration task | Database | 5432 |
| API, worker | Cache | 6379 |
| API, worker, migration task | Internet, through NAT | 443 |

The load balancer forwards `/v1/*` and `/healthz` and answers 404 itself for everything
else, so `/readyz`, `/metrics` and the documentation pages are not reachable from outside.

## Prerequisites

- Terraform 1.11 or later (OpenTofu 1.11 or later also works; replace `terraform` with
  `tofu` in the commands).
- AWS CLI v2, signed in to the account with rights to create everything above.
- An issued ACM certificate, in the stack's region, for the name clients will use.
- A GitHub repository with an environment named `production`. Give the environment
  required reviewers: that approval is the control on who can deploy.
- For the checks only: [uv](https://docs.astral.sh/uv/), and optionally `tflint`.

## Checks

These read files and download the provider. They need no AWS account.

```text
uv run poe lint-infra
```

That runs `terraform fmt -check -recursive`, `terraform init -backend=false`,
`terraform validate` and `checkov`. Every `checkov:skip` in the modules carries its reason
on the same line. `tflint --recursive --config <full path to infra/.tflint.hcl>`, run from
`infra`, is a further check that CI runs.

The first `terraform init` writes `.terraform.lock.hcl`, which records the provider's
checksums. Commit it.

## Create

Every command is run from `infra` and is the same in PowerShell and in a Unix shell,
except where two forms are shown.

1. Settings. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in the region,
   the certificate and the repository. Leave both desired counts at `0` for now.

2. The cache's auth token. It is the one secret Terraform is handed, and it goes through a
   write-only argument, so it is in neither the state nor a plan. Generate 32 to 128
   characters from letters and digits, keep them for step 5, and put them in the
   environment of this shell only:

   ```text
   PowerShell 7: $env:TF_VAR_redis_auth_token = Read-Host -MaskInput "Redis auth token"
   Unix shell:   read -rs TF_VAR_redis_auth_token && export TF_VAR_redis_auth_token
   ```

3. Apply. This takes about fifteen minutes; the database is the slow part.

   ```text
   terraform init
   terraform plan -out corridor.tfplan
   terraform apply corridor.tfplan
   ```

4. Database roles. The instance starts with one administrative user, whose password RDS
   generated and keeps in the secret named by `terraform output database_master_secret_arn`.
   The application needs two roles: `corridor_owner`, which owns the schema and runs
   migrations, and `corridor_app`, which the services connect as. Terraform does not
   create them, because it would have to be given what they sign in with. The database is
   reachable only from inside the VPC, so connect from there (for example a short-lived
   task or instance in a private subnet, in the `migrate` security group) and run this in
   `psql`. `\password` asks for each password without echoing it and sends only its hash,
   so neither is in your history or the server's log:

   ```text
   CREATE ROLE corridor_owner LOGIN;
   CREATE ROLE corridor_app LOGIN;
   \password corridor_owner
   \password corridor_app
   GRANT ALL ON DATABASE corridor TO corridor_owner;
   ALTER DATABASE corridor OWNER TO corridor_owner;
   ALTER SCHEMA public OWNER TO corridor_owner;
   GRANT CONNECT ON DATABASE corridor TO corridor_app;
   ```

   Everything else `corridor_app` may do is granted by the migrations.

5. Secret values. `terraform output secret_names` lists the secrets. Each was created with
   no value, and a task whose secret has no value does not start. Set each one with a
   file, so that the value is in neither your shell history nor the process list, and
   delete the file afterwards:

   ```text
   aws secretsmanager put-secret-value --secret-id corridor/database-url --secret-string file://value.txt
   ```

   | Secret | Value |
   |---|---|
   | `corridor/database-url` | `postgresql+asyncpg://corridor_app@DATABASE_ADDRESS:5432/corridor?ssl=require`, with a colon and the role's password inserted after `corridor_app` |
   | `corridor/database-owner-url` | The same with `corridor_owner` and its password |
   | `corridor/redis-url` | `rediss://CACHE_ADDRESS:6379/0`, with a colon, the token from step 2 and an `@` inserted before the address. Note the double `s`: the cache refuses a connection without TLS |
   | `corridor/jwt-signing-key` | The private key file that `uv run corridor keys generate --out DIRECTORY` writes, whole |
   | `corridor/api-key-hash-key` | At least 32 random characters |
   | `corridor/fx-cache-mac-key` | At least 32 random characters |
   | `corridor/*-api-key`, `corridor/*-webhook-secrets` | Exist only for a provider named in `provider_urls`. Webhook secrets are a JSON list of strings, each at least 32 characters |

   The names begin with the `name` variable, `corridor` unless you changed it.
   `DATABASE_ADDRESS` and `CACHE_ADDRESS` are the outputs `database_address` and
   `cache_address`. A password with characters that are special in a URL must be
   percent-encoded.

6. GitHub. `terraform output github_variables` prints the variables the deploy workflow
   reads. Create each as a variable of the `production` environment. None is a secret, and
   the workflow uses no stored AWS key: it exchanges a GitHub identity token for the
   deploy role, which trusts only this repository's `production` environment.

7. First release. Run the **Deploy** workflow from the `main` branch. It builds the image,
   pushes it, runs the migration task and waits for it to succeed, then updates both
   services. They still have a desired count of 0, so:

8. Start the services. Set `api_desired_count = 2` and `worker_desired_count = 1` in
   `terraform.tfvars`, then plan and apply as in step 3.

9. DNS. Point the certificate's name at the output `alb_dns_name`.

After the first release, deploying is step 7 alone. Terraform does not track which release
a service runs; a change to a task's settings here takes effect at the next deploy, which
copies the newest task definition and changes only its image.

### To confirm against the running stack

- **Client address.** The API believes `X-Forwarded-For` only from the public subnets,
  where the load balancer is. Rate limiting by address depends on it: make a request and
  check that the address in the API's access log is yours and not the load balancer's.
- **TLS to the database.** `ssl=require` encrypts without checking the server's
  certificate. To check it, the image must carry the RDS certificate bundle and the
  connection string must name it. The application refuses to start in production with a
  database URL that does not ask for TLS (`ssl=require`, `verify-ca` or `verify-full`) or
  a Redis URL that is not `rediss://`, so a connection string stored without them shows
  as a task that stops at start, with the setting named in its log.
- **Each task starts with what it is given.** The API and the worker check their
  configuration for production when they start, each against what that process needs: the
  worker is given no webhook secret and none of the API's keys, and is not asked for
  them. `tests/assembly/test_deployed_settings.py` reads `main.tf` and the identity
  module and loads the settings as each task does; it has not been tried against tasks
  that really ran.
- **Read-only filesystem.** The containers run with a read-only root filesystem and a
  writable `/tmp`. If the image writes anywhere else, the task stops at start with the
  path in its log.

## Rotating a secret

- An application secret: put the new value, then run the Deploy workflow (or force a new
  deployment of the service). Tasks read secrets only when they start.
- The cache's token: export the new token as in step 2, raise `redis_auth_token_version`
  by one and apply. With the default strategy the cache accepts both tokens. Update
  `corridor/redis-url`, redeploy, then set `redis_auth_token_update_strategy = "SET"`,
  raise the version again and apply, which retires the old token.
- The signing key: see `corridor keys generate --help` for how a new key is rolled out
  without invalidating tokens signed by the old one.

## Remote state

State is a local file until you configure a backend. `versions.tf` has an S3 backend,
commented out, with the steps beside it. Use it for anything more than a trial: state
lists every resource and must not be lost or shared by copying. No secret value is in the
state, by design, but treat it as sensitive all the same.

## Cost

**An estimate, not a quotation.** These are rough figures for the default settings in
`us-east-1`, from list prices as the author remembered them, with almost no traffic.
**Check every line in the [AWS Pricing Calculator](https://calculator.aws/) before you
create anything.** Prices differ by region and change.

| Item | Default | Rough USD per month |
|---|---|---|
| Load balancer | One, lightly used | 18–25 |
| NAT gateway | One (`single_nat_gateway = true`), plus data processed | 33–40 |
| Fargate | Three tasks of 0.25 vCPU and 0.5 GB (two API, one worker) | 25–30 |
| RDS | `db.t4g.micro`, one zone, 20 GB gp3, 7 days of backups | 15–20 |
| ElastiCache | One `cache.t4g.micro` | 10–15 |
| Secrets Manager | Six secrets, plus the one RDS keeps | 3–4 |
| Logs, image storage, public addresses | Little, at low volume | 5–15 |
| **Total if left running** | | **roughly 110–150** |

Choices that raise it: `db_multi_az = true` roughly doubles the database line;
`single_nat_gateway = false` doubles the NAT line; each cache replica adds a node;
`container_insights = true` adds per-metric charges.

## Destroy

Deletion is refused twice on purpose, so destroying takes two applies.

1. In `terraform.tfvars`, set:

   ```text
   db_deletion_protection  = false
   alb_deletion_protection = false
   db_skip_final_snapshot  = true
   ```

   Leave `db_skip_final_snapshot` at `false` if you want a last snapshot of the database;
   it is then kept, and billed, as `corridor-final` until you delete it.

2. Apply that change, then destroy:

   ```text
   terraform apply
   terraform destroy
   ```

3. If `terraform destroy` stops at the image repository because it still holds images,
   delete them and run it again:

   ```text
   aws ecr batch-delete-image --repository-name corridor --image-ids imageTag=TAG
   ```

4. What is left, and still billed or still in the way:
   - The secrets are scheduled for deletion and are gone after seven days. Until then a
     new stack with the same name cannot create them; restore them or wait.
   - Automated database backups are deleted with the instance. A final snapshot and any
     manual snapshot are not.
   - The state bucket, if you made one, the ACM certificate and the DNS record are yours
     to remove.
   - Delete the variables of the GitHub `production` environment, so that a deploy cannot
     be started against a stack that is gone.

Afterwards, check the Billing console the next day. That is the only check that covers
what this list forgot.
