terraform {
  required_version = ">= 1.6"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      # >= 5.81 specifically: that is where thumbprint_list became optional
      # for the GitHub OIDC provider (December 2024).
      version = "~> 5.81"
    }
  }

  # State is local by default: this is a single-operator project and a remote
  # backend is another thing to set up and pay attention to. Move to an S3
  # backend the moment a second person or a second machine touches it.
  # backend "s3" {
  #   bucket = "rateradar-tfstate"
  #   key    = "prod/terraform.tfstate"
  #   region = "ap-southeast-2"
  # }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "rateradar"
      ManagedBy = "terraform"
    }
  }
}
