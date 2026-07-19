#!/usr/bin/env bash
# Run the firecracker conformance corpus from the macOS host, sliced
# across Lima VM restarts.
#
# Why: the nested-virt dev VM (dev/lima-fc.yaml) wears out after
# ~30-40 min of VM churn — boots stall mid-kernel and guest python
# hangs silently at startup, even on known-good code. Repeated GiB
# guest allocations and snapshot writes appear to fragment the VM's
# memory; a restart resets it. So: run the corpus in slices, restart
# the VM between slices, and keep every slice well inside the window.
# (CI on real Linux gets fresh machines per run and never needs this.)
#
# Usage: dev/fc-test.sh [erofs]        # medium defaults to initramfs
set -euo pipefail

MEDIUM="${1:-initramfs}"
VM=fc
RUN='export UV_PROJECT_ENVIRONMENT=$HOME/venv-dud PATH=$HOME/.local/bin:$PATH && '
RUN+="DUD_BACKEND=firecracker DUD_MEDIUM=$MEDIUM uv run --extra dev pytest -q "

SLICES=(
  # snapshot-heavy first (freshest VM), then the classic corpus split
  "tests/conformance/test_freeze.py tests/conformance/test_lifecycle.py"
  "tests/conformance/test_python.py tests/conformance/test_shell.py tests/conformance/test_tree_diff.py"
  "tests/conformance/test_services.py tests/conformance/test_scratch.py tests/conformance/test_view_worker.py"
)

restart_vm() {
  limactl stop "$VM" >/dev/null 2>&1 || true
  limactl start "$VM" >/dev/null
  limactl shell "$VM" -- sudo chmod 666 /dev/kvm
}

fail=0
for i in "${!SLICES[@]}"; do
  echo "=== slice $((i + 1))/${#SLICES[@]}: ${SLICES[$i]}"
  restart_vm
  limactl shell "$VM" -- bash -c "$RUN ${SLICES[$i]}" || fail=1
done
exit $fail
