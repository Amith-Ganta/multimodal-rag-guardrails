# Backend configuration

The `backend "s3"` block in `versions.tf` is deliberately incomplete. It sets
only `encrypt` and `use_lockfile`; the bucket, key and region live in the
`*.s3.tfbackend` files here and are supplied at init time.

```bash
terraform init -backend-config=backends/dev.s3.tfbackend
```

## Why the values are not in versions.tf

A backend block cannot interpolate anything. It is read before Terraform has
evaluated variables, locals or data sources, so the state bucket cannot be
derived from the account id the way every other globally-unique name in this
stack is.

Writing the values inline was what pinned this configuration to one AWS
account. The failure mode is worth spelling out, because only one half of it is
loud: an operator in a second account either fails at init because they cannot
write to the first account's bucket, or, if someone has helpfully granted
cross-account access, plans against the first account's state and sees a diff
that proposes destroying infrastructure they have never seen.

Leaving the values out converts that into an error at the first command. Init
refuses to run without `-backend-config`, so the target account has to be named
every time rather than inherited from whatever was committed.

## Adding a new account

Copy `example.s3.tfbackend.template` to `<name>.s3.tfbackend` and fill in the
three values, then create the bucket before running init. The state bucket is
the standard chicken-and-egg exception to "everything in Terraform": it cannot
be managed by the state it holds.

```bash
ACCOUNT_ID=<account id>
REGION=<region>

aws s3api create-bucket \
  --bucket "multimodal-rag-tfstate-${ACCOUNT_ID}" \
  --region "${REGION}"
# outside us-east-1, add:
#   --create-bucket-configuration LocationConstraint="${REGION}"

aws s3api put-bucket-versioning \
  --bucket "multimodal-rag-tfstate-${ACCOUNT_ID}" \
  --versioning-configuration Status=Enabled

aws s3api put-public-access-block \
  --bucket "multimodal-rag-tfstate-${ACCOUNT_ID}" \
  --public-access-block-configuration \
  BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

Versioning is not optional. `use_lockfile` stops two people writing state at
once, but nothing except versioning recovers a state file that was truncated or
corrupted mid-write.

Then:

```bash
terraform init -reconfigure -backend-config=backends/<name>.s3.tfbackend
terraform plan -var-file=envs/<name>.tfvars
```

## -reconfigure is not optional when switching accounts

In a working copy that has already been initialised against another backend,
plain `init` offers to copy the existing state into the new backend. Accepting
that seeds the second account's state with the first account's resource ids, so
the next plan proposes updating resources that do not exist in the account it
is pointed at. `-reconfigure` discards the old backend association instead of
migrating it.

This is not theoretical. Initialising this directory after the backend was made
partial hit exactly that prompt, because two pre-migration `terraform.tfstate`
files were still sitting in `terraform/eks/` from before the move to S3. They
held 40 resources at serial 49 under one lineage; the real state in S3 held 62
at serial 21 under a different lineage. Migrating would have replaced the live
state with a copy that was 22 resources out of date. They now live in
`.state-archive/`, which is gitignored.

## Are these files secret?

No. A bucket name, an object key and a region. The bucket is private and access
is governed by the caller's AWS credentials, not by knowledge of the name. The
state files themselves are a different matter and are gitignored.
