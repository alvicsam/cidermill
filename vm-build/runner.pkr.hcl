packer {
  required_plugins {
    tart = {
      version = ">= 0.6.1"
      source  = "github.com/cirruslabs/tart"
    }
  }
}

variable "name" {
  type = string
}

source "tart-cli" "tart" {
  vm_base_name = "ghcr.io/cirruslabs/macos-sonoma-xcode:latest"
  vm_name      = "${var.name}"
  cpu_count    = 4
  memory_gb    = 8
  disk_size_gb = 150
  headless     = true
  ssh_password = "admin"
  ssh_username = "admin"
  ssh_timeout  = "120s"
}

build {
  sources = ["source.tart-cli.tart"]

  // Install SSH key DO NOT DELETE
  provisioner "shell" {
    inline = [
      "mkdir ~/.ssh",
    ]
  }
  // Install SSH key DO NOT DELETE
  provisioner "file" {
    source = "runner_authorized_keys"
    destination = "~/.ssh/authorized_keys"
  }
  // Install SSH key DO NOT DELETE
  provisioner "shell" {
    inline = [
      "chmod -R 700 ~/.ssh",
    ]
  }
}
