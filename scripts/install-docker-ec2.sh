#!/bin/bash
# Run this on your Ubuntu EC2 instance to install Docker + Docker Compose
set -e

echo "Updating packages..."
sudo apt-get update -y

echo "Installing Docker..."
sudo apt-get install -y ca-certificates curl gnupg
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg

echo \
  "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu \
  $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list > /dev/null

sudo apt-get update -y
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

echo "Adding current user to docker group (avoids needing sudo)..."
sudo usermod -aG docker $USER

echo "Done. Log out and back in (or run 'newgrp docker') for group changes to apply."
echo "Verify with: docker --version && docker compose version"
