"""OCI image -> bootable dud rootfs pipeline (host-side build tool).

Pulls a base image with a dependency-free registry client, flattens its
layers into a root-owned initramfs, injects the dud guest runtime, and
caches the result under ``~/.dud`` keyed by spec hash. Not part of the
guest; runs on the developer/host machine to produce what a VM backend
boots.
"""

from __future__ import annotations

from .builder import RootfsBuild, build, dud_home
from .registry import ImageRef, PulledImage, Registry, RegistryError

__all__ = [
    "build",
    "dud_home",
    "RootfsBuild",
    "Registry",
    "RegistryError",
    "ImageRef",
    "PulledImage",
]
