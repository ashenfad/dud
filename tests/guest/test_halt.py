"""How the guest stops the machine, and why it differs per arch.

Worth its own file because the failure this guards is silent. A guest
that picks the wrong reboot(2) command still *serves*, still answers
`shutdown`, and still reports success to the host — it simply never
goes away, and the host discovers that only by waiting out its
VMM-exit timeout. Nothing raises. The bill arrives as wall-clock, on
every teardown, which is how it hid long enough to make a conformance
run look like it was measuring the guest.
"""

from dud.backends.firecracker import FirecrackerSession
from dud.guest.init import _RB_AUTOBOOT, _RB_POWER_OFF, _halt_cmd


def test_aarch64_powers_off():
    """PSCI SYSTEM_OFF is real, so the honest call works."""
    assert _halt_cmd("aarch64") == _RB_POWER_OFF


def test_x86_64_reboots_instead():
    """No ACPI under firecracker: power-off halts the CPU forever, and
    only a reset makes the VMM exit."""
    assert _halt_cmd("x86_64") == _RB_AUTOBOOT


def test_an_unknown_arch_powers_off():
    """The portable call is the safe default; the reset trick is opt-in
    per arch that is known to need it."""
    assert _halt_cmd("riscv64") == _RB_POWER_OFF


def test_the_x86_reset_path_is_actually_wired_up():
    """The half that makes the other half mean anything.

    Asking Linux to restart only *stops* the machine because
    ``reboot=k`` aims its restart path at the i8042 reset line, which
    firecracker traps and exits on. Drop that token from the cmdline
    and the guest's reboot becomes a real reboot — the VM comes back up
    instead of going away, and we are back to paying the timeout on
    every teardown with nothing to show for it.

    So the two are asserted together. They are one mechanism split
    across a host file and a guest file, and neither is correct alone.
    """
    cmdline = FirecrackerSession._console_arg(FirecrackerSession)
    assert "reboot=k" in cmdline

    # And nothing in the boot-time probe trimming may turn the
    # controller off, since that is the device the reset rides.
    assert "i8042.nokbd" not in cmdline
    assert "noi8042" not in cmdline
