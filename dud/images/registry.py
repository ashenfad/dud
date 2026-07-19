"""Pull OCI images from a registry with no daemon and no dependencies.

A minimal Docker Registry v2 / OCI client: anonymous bearer-token auth,
manifest-index platform selection, digest-verified blob download into a
content-addressed cache, and a per-(reference, arch) resolution cache
so already-pulled images survive registry outages and rate limits.
Enough to flatten a public base image (``python:3.12-slim``) into a
rootfs — deliberately *not* a general registry library.

Scope: Docker Hub and any registry that speaks the v2 token flow. A
bare name (``python``) resolves to ``library/python`` on Docker Hub; a
``host/`` prefix routes elsewhere. Private auth, pushes, and foreign
layers are out of scope for the rung.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import platform
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import DudError

_DEFAULT_REGISTRY = "registry-1.docker.io"
_DOCKER_AUTH = "https://auth.docker.io/token"
_DOCKER_SERVICE = "registry.docker.io"

# Manifest media types we understand, most-preferred last is irrelevant —
# the registry picks; we just have to advertise all four.
_MANIFEST_ACCEPT = ",".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
])

_INDEX_TYPES = {
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
}


class RegistryError(DudError):
    """A pull failed: transport, auth, digest mismatch, or no platform."""


@dataclass(frozen=True)
class ImageRef:
    """A parsed image reference split into registry / repository / tag."""

    registry: str
    repository: str
    reference: str  # tag or digest

    @classmethod
    def parse(cls, ref: str) -> "ImageRef":
        registry = _DEFAULT_REGISTRY
        name = ref
        # A leading component with a dot or ':' (or 'localhost') is a registry.
        head, slash, rest = ref.partition("/")
        if slash and ("." in head or ":" in head or head == "localhost"):
            registry, name = head, rest
        if "@" in name:
            repo, reference = name.rsplit("@", 1)
        elif ":" in name.rsplit("/", 1)[-1]:
            repo, reference = name.rsplit(":", 1)
        else:
            repo, reference = name, "latest"
        if registry == _DEFAULT_REGISTRY and "/" not in repo:
            repo = f"library/{repo}"
        return cls(registry, repo, reference)

    def __str__(self) -> str:
        return f"{self.registry}/{self.repository}:{self.reference}"


@dataclass
class PulledImage:
    """A resolved single-platform image: config + ordered gzipped layers.

    ``layer_paths`` point at content-addressed files in the blob cache
    (``.tar.gz``); ``config`` is the parsed image config JSON; ``digest``
    is the manifest digest (the stable identity used for spec hashing).
    """

    ref: ImageRef
    digest: str
    config: dict
    layer_paths: list[Path] = field(default_factory=list)

    @property
    def env(self) -> list[str]:
        return list(self.config.get("config", {}).get("Env", []))

    @property
    def workdir(self) -> str:
        return self.config.get("config", {}).get("WorkingDir", "") or "/"


def _host_arch() -> str:
    m = platform.machine().lower()
    if m in ("arm64", "aarch64"):
        return "arm64"
    if m in ("x86_64", "amd64"):
        return "amd64"
    return m


class Registry:
    """Token-authenticated v2 client with a content-addressed blob cache.

    ``cache_dir`` holds ``blobs/sha256/<hex>``; blobs are shared across
    every image (they *are* their digest), so re-pulls of overlapping
    images cost nothing.
    """

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)
        self.blobs = self.cache_dir / "blobs" / "sha256"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._tokens: dict[str, str] = {}

    # ---- auth --------------------------------------------------------

    def _token(self, ref: ImageRef) -> str:
        if ref.repository in self._tokens:
            return self._tokens[ref.repository]
        # Only Docker Hub's anonymous flow is wired; other registries that
        # need no auth still work (the token just goes unused).
        if ref.registry != _DEFAULT_REGISTRY:
            self._tokens[ref.repository] = ""
            return ""
        q = urllib.parse.urlencode({
            "service": _DOCKER_SERVICE,
            "scope": f"repository:{ref.repository}:pull",
        })
        try:
            with urllib.request.urlopen(f"{_DOCKER_AUTH}?{q}", timeout=30) as r:
                tok = json.load(r).get("token", "")
        except urllib.error.URLError as e:
            raise RegistryError(f"auth failed for {ref.repository}: {e}") from e
        self._tokens[ref.repository] = tok
        return tok

    def _get(self, ref: ImageRef, path: str, accept: str) -> urllib.request.addinfourl:
        url = f"https://{ref.registry}/v2/{ref.repository}/{path}"
        for attempt in (0, 1):
            headers = {"Accept": accept}
            tok = self._token(ref)
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
            try:
                return urllib.request.urlopen(
                    urllib.request.Request(url, headers=headers), timeout=120
                )
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 0:
                    # Anonymous tokens expire (~5 min); a long multi-blob
                    # pull can outlive one. Re-auth once and retry.
                    self._tokens.pop(ref.repository, None)
                    continue
                raise RegistryError(f"GET {path} -> {e.code} {e.reason}") from e
            except urllib.error.URLError as e:
                raise RegistryError(f"GET {path} failed: {e}") from e
        raise AssertionError("unreachable")

    # ---- manifests ---------------------------------------------------

    def _resolution_path(self, ref: ImageRef, arch: str) -> Path:
        key = hashlib.sha256(f"{ref}|{arch}".encode()).hexdigest()
        return self.cache_dir / "manifests" / f"{key}.json"

    def _resolve(self, ref: ImageRef, arch: str) -> tuple[dict, str]:
        """Network resolution: reference -> (platform manifest, digest)."""
        manifest, digest = self._manifest(ref, ref.reference)
        if manifest.get("mediaType") in _INDEX_TYPES or "manifests" in manifest:
            plat_digest = self._select_platform(manifest, arch)
            manifest, digest = self._manifest(ref, plat_digest)
        return manifest, digest

    def _manifest(self, ref: ImageRef, reference: str) -> tuple[dict, str]:
        with self._get(ref, f"manifests/{reference}", _MANIFEST_ACCEPT) as r:
            raw = r.read()
        actual = "sha256:" + hashlib.sha256(raw).hexdigest()
        if reference.startswith("sha256:") and actual != reference:
            # Closes the trust chain index -> manifest -> blobs: a
            # manifest fetched BY digest must hash to that digest.
            raise RegistryError(
                f"manifest digest mismatch: asked {reference}, got {actual}"
            )
        return json.loads(raw), actual

    def _select_platform(self, index: dict, arch: str) -> str:
        for m in index.get("manifests", []):
            p = m.get("platform", {})
            if p.get("os") == "linux" and p.get("architecture") == arch:
                return m["digest"]
        have = sorted(
            f"{m.get('platform', {}).get('os')}/{m.get('platform', {}).get('architecture')}"
            for m in index.get("manifests", [])
        )
        raise RegistryError(f"no linux/{arch} in image (have: {', '.join(have)})")

    # ---- blobs -------------------------------------------------------

    def _blob(self, ref: ImageRef, digest: str) -> Path:
        algo, _, hexd = digest.partition(":")
        if algo != "sha256":
            raise RegistryError(f"unsupported digest algo {algo!r}")
        dest = self.blobs / hexd
        if dest.exists():
            return dest
        tmp = dest.with_suffix(".part")
        h = hashlib.sha256()
        with self._get(ref, f"blobs/{digest}", "*/*") as r, open(tmp, "wb") as f:
            while chunk := r.read(1 << 20):
                h.update(chunk)
                f.write(chunk)
        if h.hexdigest() != hexd:
            tmp.unlink(missing_ok=True)
            raise RegistryError(f"digest mismatch for {digest}: got {h.hexdigest()}")
        tmp.rename(dest)
        return dest

    # ---- public ------------------------------------------------------

    def pull(self, ref: str | ImageRef, arch: str | None = None) -> PulledImage:
        """Resolve a reference to one platform and fetch its blobs.

        Resolution is cached per (reference, arch): a successful resolve
        refreshes the cache; an unreachable or rate-limiting registry
        falls back to the last-known resolution, so images whose blobs
        are already cached keep working offline. The fallback pins a
        mutable tag to whatever it meant last time — the same tradeoff
        every local image store makes.
        """
        r = ref if isinstance(ref, ImageRef) else ImageRef.parse(ref)
        arch = arch or _host_arch()
        cache = self._resolution_path(r, arch)
        try:
            manifest, digest = self._resolve(r, arch)
        except RegistryError:
            if not cache.exists():
                raise
            cached = json.loads(cache.read_text())
            manifest, digest = cached["manifest"], cached["digest"]
        else:
            cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache.with_suffix(".part")
            tmp.write_text(json.dumps({"digest": digest, "manifest": manifest}))
            tmp.rename(cache)
        config_path = self._blob(r, manifest["config"]["digest"])
        config = json.loads(config_path.read_bytes())
        layers = [self._blob(r, lyr["digest"]) for lyr in manifest["layers"]]
        return PulledImage(ref=r, digest=digest, config=config, layer_paths=layers)


def open_layer(path: Path):
    """Open a layer blob as a decompressed stream (gzip or plain tar)."""
    with open(path, "rb") as f:
        magic = f.read(2)
    if magic == b"\x1f\x8b":
        return gzip.open(path, "rb")
    return open(path, "rb")
