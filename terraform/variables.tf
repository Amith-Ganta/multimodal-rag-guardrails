variable "aws_region" {
  description = "AWS region to deploy into."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = "Short name used to prefix/tag every resource."
  type        = string
  default     = "multimodal-rag"
}

variable "instance_type" {
  description = "EC2 instance type. Two CPU-only images (~1.7GB each) plus torch/transformers at runtime need real RAM, so this defaults above t3.micro."
  type        = string
  default     = "t3.large"
}

variable "root_volume_gb" {
  description = "Root EBS volume size in GB. Two images at ~1.7GB each, HF model cache, and the FAISS index add up fast."
  type        = number
  default     = 40
}

variable "ssh_key_name" {
  description = "Name of an EXISTING EC2 key pair (create it in the AWS console or with `aws ec2 create-key-pair` first). Terraform does not create or manage the private key."
  type        = string
}

variable "ssh_ingress_cidr" {
  description = "CIDR allowed to reach port 22. Restrict to your own IP/32 in production; 0.0.0.0/0 is a convenience default for first setup only."
  type        = string
  default     = "0.0.0.0/0"
}

variable "app_ingress_cidr" {
  description = "CIDR allowed to reach the app ports (8501 UI, 8000 API)."
  type        = string
  default     = "0.0.0.0/0"
}

variable "allocate_eip" {
  description = "Whether to attach a stable Elastic IP to the instance."
  type        = bool
  default     = true
}
