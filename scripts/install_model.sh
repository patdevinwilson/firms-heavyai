#!/bin/bash
set -euo pipefail
mkdir -p /opt/firms-model
cd /opt/firms-model
if ! test -d llama.cpp; then
  git clone https://github.com/ggml-org/llama.cpp.git
fi
git -C llama.cpp fetch origin 8345f333951c661d166b00e6f9362e553768f292
git -C llama.cpp checkout --detach 8345f333951c661d166b00e6f9362e553768f292
cmake -S llama.cpp -B llama.cpp/build -DGGML_CUDA=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=ON
cmake --build llama.cpp/build --target llama-server -j 8
checksum=be5d9a656a51922f24f1f09a759cebb694e1f5d9728bf0ef9f8c972c5a0b5ef2
if ! echo "$checksum  model.gguf" | sha256sum --check --status; then
  curl --fail --location --retry 3 --proto '=https' --tlsv1.2 \
    https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF/resolve/ba223d14e45525f7fae81db77ea8cabeb2fc6c25/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf -o model.gguf.part
  echo "$checksum  model.gguf.part" | sha256sum --check --status
  mv model.gguf.part model.gguf
fi
cat > /etc/systemd/system/firms-model.service <<'UNIT'
[Unit]
Description=Nemotron CPU inference for HeavyIQ
After=network-online.target
[Service]
ExecStart=/opt/firms-model/llama.cpp/build/bin/llama-server --model /opt/firms-model/model.gguf --alias firms-nemotron --host 127.0.0.1 --port 8101 --ctx-size 8192 --threads 8 --n-gpu-layers 0 --parallel 1
Restart=on-failure
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
cat > /etc/systemd/system/firms-iq-adapter.service <<'UNIT'
[Unit]
Description=HeavyIQ task prompts to Nemotron chat adapter
After=firms-model.service docker.service
Requires=firms-model.service
[Service]
WorkingDirectory=/opt/firms-heavyai
ExecStart=/opt/firms-heavyai/.venv/bin/python -m app.iq_adapter
Restart=on-failure
UMask=0077
[Install]
WantedBy=multi-user.target
UNIT
systemctl daemon-reload
systemctl enable --now firms-model.service firms-iq-adapter.service
for attempt in {1..120}; do
  if curl --fail --silent http://127.0.0.1:8101/health >/dev/null; then exit 0; fi
  sleep 5
done
echo 'Nemotron model did not become ready' >&2
exit 1
