"""Where the data lives, resolved in one place.

ADR 0002. Every read in this project already funnels through two functions that
turn a path into a PyArrow dataset -- :func:`reconcile.curated.open_curated` and
:func:`payer.curated.open_payer_dataset`. PyArrow abstracts local storage from
ADLS Gen2 behind ``pyarrow.fs`` already, so moving to the cloud needs a
filesystem and a root, not a second implementation of anything.

That is the whole design. There is no ``Backend`` class and no ``FabricBackend``,
because the two would differ only in the filesystem object they hand to the same
``ds.dataset`` call, and only one of them could ever be exercised without cloud
credentials. One code path stays under test; the other would have been hoped for.

**No credential passes through this module.** ``AzureFileSystem`` falls back to
``DefaultAzureCredential`` when constructed with only an account name, so
authentication comes from the Azure CLI login, a managed identity, or the
environment -- outside this process and outside this repository. The account name
is a public identifier, not a secret. Nothing here reads a key, a token or a
connection string, and nothing should be added that does.

Configuration, all optional, all defaulting to local:

* ``RECKONER_STORAGE`` -- ``local`` (default) or ``adls``.
* ``RECKONER_ADLS_ACCOUNT`` -- storage account name, required when ``adls``.
* ``RECKONER_ADLS_ROOT`` -- container and prefix, e.g. ``lake/curated``.

Fabric is deliberately absent from all of it. Per architecture rule 2, ADLS Gen2
is the authoritative store and Fabric reads it through OneLake shortcuts, so
Fabric is a consumer of this location rather than a variety of it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import pyarrow.fs as pafs

#: Environment variables read here. Named so a caller can report configuration
#: without this module having to log anything itself.
STORAGE_MODE = "RECKONER_STORAGE"
ADLS_ACCOUNT = "RECKONER_ADLS_ACCOUNT"
ADLS_ROOT = "RECKONER_ADLS_ROOT"

LOCAL = "local"
ADLS = "adls"


class StorageConfigError(RuntimeError):
    """Configuration that cannot be honoured, reported rather than guessed at.

    Falling back to local when ADLS was asked for would be worse than failing:
    the run would succeed against whatever happened to be on the laptop and the
    numbers would be quietly about the wrong dataset.
    """


@dataclass(frozen=True)
class Location:
    """A root and the filesystem it is a root of."""

    root: str
    filesystem: pafs.FileSystem

    @property
    def is_local(self) -> bool:
        return isinstance(self.filesystem, pafs.LocalFileSystem)

    def child(self, *parts: str) -> Location:
        """A location below this one. ``/`` is the only delimiter ADLS accepts."""
        suffix = "/".join(p.strip("/") for p in parts if p)
        root = f"{self.root.rstrip('/')}/{suffix}" if suffix else self.root
        return Location(root=root, filesystem=self.filesystem)

    def exists(self) -> bool:
        info = self.filesystem.get_file_info(self.root)
        return bool(info.type != pafs.FileType.NotFound)

    def describe(self) -> str:
        kind = LOCAL if self.is_local else type(self.filesystem).__name__
        return f"{kind}:{self.root}"


def local(root: Path | str) -> Location:
    """A location on this machine. The default, and what every test uses."""
    return Location(root=str(root), filesystem=pafs.LocalFileSystem())


def resolve(root: Path | str | None = None, *, env: dict[str, str] | None = None) -> Location:
    """Where to read from, given the environment.

    ``root`` is the local path a caller already had. It is used as-is in local
    mode, and ignored in ADLS mode, where the root comes from configuration --
    a laptop path has no meaning in a storage account.
    """
    environ = os.environ if env is None else env
    mode = (environ.get(STORAGE_MODE) or LOCAL).strip().lower()

    if mode == LOCAL:
        return local(root if root is not None else ".")

    if mode != ADLS:
        raise StorageConfigError(
            f"{STORAGE_MODE}={mode!r} is not a storage mode; use {LOCAL!r} or {ADLS!r}"
        )

    account = (environ.get(ADLS_ACCOUNT) or "").strip()
    if not account:
        raise StorageConfigError(f"{STORAGE_MODE}={ADLS} requires {ADLS_ACCOUNT} to be set")
    prefix = (environ.get(ADLS_ROOT) or "").strip().strip("/")
    if not prefix:
        raise StorageConfigError(
            f"{STORAGE_MODE}={ADLS} requires {ADLS_ROOT}, the container and prefix to read"
        )

    # Constructed with the account name only, so authentication is
    # DefaultAzureCredential's problem and no secret is read here.
    return Location(root=prefix, filesystem=pafs.AzureFileSystem(account_name=account))


__all__ = [
    "ADLS",
    "ADLS_ACCOUNT",
    "ADLS_ROOT",
    "LOCAL",
    "STORAGE_MODE",
    "Location",
    "StorageConfigError",
    "local",
    "resolve",
]
