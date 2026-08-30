"""The reset sweep's kernel-thread exclusion.

`do_reset_guest` kills every non-PID-1 process and then waits for the
machine to be ours again, bounded at 2 s. It counted kernel threads —
which are unkillable by design — so "only PID 1 remains" was a
condition that could never become true, and the sweep ran its entire
budget on every single reset.

Measured on vfkit: 2.01 s per reset, every time, against a 0.94 s cold
boot. Pooled reuse cost more than booting a fresh machine, which is
the opposite of what a pool is for.

The sweep itself only runs as guest PID 1, so these test the predicate
that decides what it counts.
"""

from __future__ import annotations

import os

from dud.guest.supervisor import _is_kernel_thread


def _proc_entry(root, pid: str, cmdline: bytes | None):
    d = root / pid
    d.mkdir()
    if cmdline is not None:
        (d / "cmdline").write_bytes(cmdline)
    return d


def test_a_real_process_is_not_a_kernel_thread(tmp_path):
    _proc_entry(tmp_path, "42", b"/usr/local/bin/python3\x00-m\x00dud.guest.runner\x00")
    assert _is_kernel_thread("42", proc=str(tmp_path)) is False


def test_an_empty_cmdline_is_a_kernel_thread(tmp_path):
    """How ps and top tell them apart, and the whole fix: a guest has
    dozens of these (kworker, ksoftirqd, the virtio threads) and none
    of them will ever die."""
    _proc_entry(tmp_path, "7", b"")
    assert _is_kernel_thread("7", proc=str(tmp_path)) is True


def test_a_vanished_entry_is_not_ours_to_kill(tmp_path):
    assert _is_kernel_thread("999", proc=str(tmp_path)) is True


def test_a_zombie_reads_as_unkillable_too(tmp_path):
    """A zombie's cmdline is empty as well, and skipping it is right:
    it is already dead, and the waitpid pass beside the sweep reaps
    it. Counting it would reintroduce the same never-converging loop."""
    _proc_entry(tmp_path, "13", b"")
    assert _is_kernel_thread("13", proc=str(tmp_path)) is True


def test_the_sweep_converges_when_only_kernel_threads_remain(tmp_path):
    """The loop's exit condition, in the shape the supervisor uses it.

    This is the assertion that would have caught the bug: with kernel
    threads counted, `others` is never empty and the sweep burns its
    full deadline.
    """
    for pid, cmd in [("2", b""), ("3", b""), ("40", b""), ("55", b"")]:
        _proc_entry(tmp_path, pid, cmd)
    _proc_entry(tmp_path, "96", b"/usr/local/bin/python3\x00-m\x00dud.guest.template\x00")

    def scan():
        return [e for e in os.listdir(tmp_path)
                if e.isdigit() and e != "1"
                and not _is_kernel_thread(e, proc=str(tmp_path))]

    assert scan() == ["96"]  # only the real process is a target
    (tmp_path / "96" / "cmdline").unlink()  # it died; cmdline goes empty
    assert scan() == [], "the sweep would never see the machine as idle"
