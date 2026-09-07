terraform {
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    kubernetes = {
      source  = "hashicorp/kubernetes"
      version = "~> 2.31"
    }
    helm = {
      source  = "hashicorp/helm"
      version = "~> 2.14"
    }
    tls = {
      source  = "hashicorp/tls"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Partial backend configuration. bucket, key and region are deliberately
  # absent and supplied at init time:
  #
  #   terraform init -backend-config=backends/dev.s3.tfbackend
  #
  # A backend block cannot interpolate variables, locals or data sources: it is
  # read before Terraform has evaluated anything, which is why the state bucket
  # cannot simply be derived from the account id the way every other name in
  # this stack is. Writing the values inline is what previously pinned this
  # whole configuration to one AWS account, since a second account cannot write
  # to the first account's state bucket and an operator who missed it would
  # either fail on init or, with cross-account access granted, quietly plan
  # against the wrong state.
  #
  # Leaving them out makes the omission loud: init refuses to run without a
  # -backend-config, so the target account has to be named explicitly every
  # time. See backends/README.md for creating the bucket in a new account.
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region
}

# The account this configuration is currently pointed at. Used to suffix
# globally-unique names (S3) so the same code produces non-colliding resources
# in a second account rather than reaching into the first account's.
data "aws_caller_identity" "current" {}

# Populated once aws_eks_cluster.this exists (cluster.tf) — providers below
# depend on its output attributes, which Terraform resolves at apply time.
data "aws_eks_cluster_auth" "this" {
  name = aws_eks_cluster.this.name
}

provider "kubernetes" {
  host                   = aws_eks_cluster.this.endpoint
  cluster_ca_certificate = base64decode(aws_eks_cluster.this.certificate_authority[0].data)
  token                  = data.aws_eks_cluster_auth.this.token
}

provider "helm" {
  kubernetes {
    host                   = aws_eks_cluster.this.endpoint
    cluster_ca_certificate = base64decode(aws_eks_cluster.this.certificate_authority[0].data)
    token                  = data.aws_eks_cluster_auth.this.token
  }
}
