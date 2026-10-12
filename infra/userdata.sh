#!/bin/bash
set -euo pipefail
exec >> /var/log/firms-base-install.log 2>&1
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y docker.io python3-venv python3-pip awscli unzip curl gnupg ubuntu-drivers-common git cmake build-essential libcurl4-openssl-dev
systemctl enable --now docker
if ! snap list amazon-ssm-agent >/dev/null 2>&1; then
  snap install amazon-ssm-agent --classic
fi
snap start amazon-ssm-agent
ubuntu-drivers install --gpgpu
# Rendering needs the GL/Vulkan driver libraries as well as the headless driver.
branch=$(dpkg-query -W -f='${Package}\n' | sed -nE 's/^nvidia-(driver|headless(-no-dkms)?)-([0-9]+)-server(-open)?$/\3/p' | sort -u | tail -1)
test -n "$branch"
apt-get install -y "nvidia-utils-${branch}-server" "libnvidia-gl-${branch}-server"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list |
  sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl restart docker
touch /var/lib/firms-base-ready
reboot
