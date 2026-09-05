output "instance_id" {
  value = aws_instance.app.id
}

output "public_ip" {
  description = "Stable EIP if allocate_eip=true, otherwise the instance's (unstable) public IP."
  value       = var.allocate_eip ? aws_eip.app[0].public_ip : aws_instance.app.public_ip
}

output "ssh_command" {
  value = "ssh -i <path-to-${var.ssh_key_name}.pem> ubuntu@${var.allocate_eip ? aws_eip.app[0].public_ip : aws_instance.app.public_ip}"
}

output "streamlit_url" {
  value = "http://${var.allocate_eip ? aws_eip.app[0].public_ip : aws_instance.app.public_ip}:8501"
}

output "api_url" {
  value = "http://${var.allocate_eip ? aws_eip.app[0].public_ip : aws_instance.app.public_ip}:8000"
}

output "ansible_inventory_hint" {
  description = "Paste this IP into ansible/inventory.ini, or generate it with the provided script."
  value       = var.allocate_eip ? aws_eip.app[0].public_ip : aws_instance.app.public_ip
}
