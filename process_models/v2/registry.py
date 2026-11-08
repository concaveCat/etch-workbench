"""Explicit registry for v2 process specs and engine variants.

Resolution is intentionally dumb and deterministic:

* process identity is keyed by ``(process_id, process_version)``;
* implementations are keyed by ``(process_id, variant_id, variant_version)``;
* variants are *not* tied to a process version, so adding a variant never
  forces a process-version bump.

Without a version pin, the newest ``MAJOR.MINOR.PATCH`` version wins. If any
candidate version is not semver, registration order decides instead. Pins are
strict: a missing pinned version raises ``KeyError`` and never falls back.

``implementation_hash`` covers the variant's entire source *file* plus the
qualified class name. That closes the v1 gap where a change to a module-level
helper function left the hash untouched.
"""

from __future__ import annotations

import hashlib
import inspect
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel

from .contract import ModelDescriptor, ProcessSpec, VariantDescriptor, VariantRegistration

__all__ = [
    "ProcessRegistry",
    "ResolvedVariant",
    "compute_implementation_hash",
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


@dataclass(frozen=True)
class ResolvedVariant:
    """A concrete process version plus a concrete variant version.

    ``implementation_hash`` is the registration's explicit hash, or the
    module-file hash computed by :func:`compute_implementation_hash`.
    """

    process: ProcessSpec
    variant: VariantRegistration
    process_version: str
    variant_version: str
    implementation_hash: str


class ProcessRegistry:
    """In-memory registry with explicit, ID-based registration.

    Registration order is preserved for listing and for non-semver version
    selection. The registry owns no native objects and performs no imports
    named by project JSON.
    """

    def __init__(self) -> None:
        self._processes: dict[str, dict[str, ProcessSpec]] = {}
        self._variants: dict[str, dict[str, list[VariantRegistration]]] = {}

    def register_process(self, spec: ProcessSpec) -> None:
        """Register one process version.

        Raises ``ValueError`` when ``(spec.id, spec.version)`` is already
        registered. Multiple versions of the same id may coexist.
        """
        if not isinstance(spec, ProcessSpec):
            raise TypeError("register_process expects a ProcessSpec")
        versions = self._processes.setdefault(spec.id, {})
        if spec.version in versions:
            raise ValueError(f"process {spec.id!r} version {spec.version!r} is already registered")
        versions[spec.version] = spec

    def register_variant(self, registration: VariantRegistration) -> None:
        """Register one variant version under an existing process id.

        Raises ``KeyError`` when the process id is unknown and ``ValueError``
        for a duplicate ``(process_id, variant_id, variant_version)``.
        """
        if not isinstance(registration, VariantRegistration):
            raise TypeError("register_variant expects a VariantRegistration")
        if registration.process_id not in self._processes:
            raise KeyError(f"unknown process {registration.process_id!r}")

        declared_process_id = getattr(registration.variant, "process_id", None)
        if declared_process_id is not None and declared_process_id != registration.process_id:
            raise ValueError(
                f"variant {registration.variant_id!r} declares process {declared_process_id!r}, "
                f"expected {registration.process_id!r}"
            )

        by_variant = self._variants.setdefault(registration.process_id, {})
        versions = by_variant.setdefault(registration.variant_id, [])
        if any(version.version == registration.version for version in versions):
            raise ValueError(
                f"variant {registration.process_id!r}/{registration.variant_id!r} "
                f"version {registration.version!r} is already registered"
            )
        versions.append(registration)

    def get_process(self, process_id: str, expected_version: str | None = None) -> ProcessSpec:
        """Return a process version, newest by default.

        Raises ``KeyError`` for an unknown id or a pinned version that is not
        registered.
        """
        versions = self._processes.get(process_id)
        if not versions:
            raise KeyError(f"unknown process {process_id!r}")
        if expected_version is not None:
            if expected_version not in versions:
                raise KeyError(f"unknown version {expected_version!r} for process {process_id!r}")
            return versions[expected_version]
        return versions[_latest_version(list(versions))]

    def resolve_step(
        self,
        project: Mapping[str, Any],
        step: Mapping[str, Any],
    ) -> tuple[ProcessSpec, BaseModel] | None:
        """Return the single process that parses ``step``, plus its parameters.

        Each process calls its ``parse_step`` hook exactly once. Only the
        newest registered version of each process id is consulted, so adding a
        newer version never leaves the older one claiming steps. No match
        returns ``None``; more than one match raises ``ValueError`` because the
        step would be ambiguous.
        """
        matches: list[tuple[ProcessSpec, BaseModel]] = []
        for versions in self._processes.values():
            spec = versions[_latest_version(list(versions))]
            if spec.parse_step is None:
                continue
            params = spec.parse_step(project, step)
            if params is not None:
                matches.append((spec, params))
        if not matches:
            return None
        if len(matches) > 1:
            candidates = ", ".join(sorted(f"{spec.id}@{spec.version}" for spec, _ in matches))
            raise ValueError(f"step matches more than one registered process: {candidates}")
        return matches[0]

    def default_variant_id(self, process_id: str) -> str:
        """Return the variant a step should use when it pins none.

        The process's explicit ``default_variant`` wins. Otherwise the process
        must have exactly one registered variant id. Raises ``KeyError`` for an
        unknown process, no variants, or an ambiguous set of variants.
        """
        spec = self.get_process(process_id)
        if spec.default_variant is not None:
            return spec.default_variant
        by_variant = self._variants.get(process_id)
        if not by_variant:
            raise KeyError(f"process {process_id!r} has no registered variants")
        if len(by_variant) != 1:
            raise KeyError(
                f"process {process_id!r} has no default variant and {len(by_variant)} variants are registered: "
                f"{', '.join(sorted(by_variant))}"
            )
        return next(iter(by_variant))

    def get_variant(
        self,
        process_id: str,
        variant_id: str,
        expected_process_version: str | None = None,
        expected_variant_version: str | None = None,
    ) -> ResolvedVariant:
        """Resolve stable IDs to a process version and a variant registration.

        ``expected_process_version`` pins the process metadata only; variants
        remain independent. ``expected_variant_version`` is a strict pin. All
        misses raise ``KeyError``.
        """
        process = self.get_process(process_id, expected_process_version)

        by_variant = self._variants.get(process_id)
        if not by_variant:
            raise KeyError(f"process {process_id!r} has no registered variants")
        registrations = by_variant.get(variant_id)
        if not registrations:
            raise KeyError(f"unknown variant {variant_id!r} for process {process_id!r}")

        if expected_variant_version is not None:
            matches = [item for item in registrations if item.version == expected_variant_version]
            if not matches:
                raise KeyError(
                    f"unknown variant {variant_id!r} version {expected_variant_version!r} for process {process_id!r}"
                )
            registration = matches[0]
        else:
            selected = _latest_version([item.version for item in registrations])
            registration = next(item for item in registrations if item.version == selected)

        implementation_hash = registration.declared_hash or compute_implementation_hash(registration.variant)
        return ResolvedVariant(
            process=process,
            variant=registration,
            process_version=process.version,
            variant_version=registration.version,
            implementation_hash=implementation_hash,
        )

    def list_processes(self) -> list[ModelDescriptor]:
        """Return the newest descriptor per process id, in registration order."""
        descriptors: list[ModelDescriptor] = []
        for process_id, versions in self._processes.items():
            spec = versions[_latest_version(list(versions))]
            descriptors.append(
                ModelDescriptor(
                    id=spec.id,
                    version=spec.version,
                    label=spec.label,
                    dimensions=spec.dimensions,
                    operations=spec.operations,
                    variants=tuple(self._variant_descriptors(process_id)),
                    description=spec.description,
                    execution_kind=spec.execution_kind,
                )
            )
        return descriptors

    def list_variants(self, process_id: str) -> list[VariantDescriptor]:
        """Return every registered variant for ``process_id`` in registration order."""
        if process_id not in self._processes:
            raise KeyError(f"unknown process {process_id!r}")
        return self._variant_descriptors(process_id)

    def _variant_descriptors(self, process_id: str) -> list[VariantDescriptor]:
        descriptors: list[VariantDescriptor] = []
        for registrations in self._variants.get(process_id, {}).values():
            for registration in registrations:
                capabilities = registration.capabilities
                descriptors.append(
                    VariantDescriptor(
                        id=registration.variant_id,
                        version=registration.version,
                        kind=registration.engine.kind,
                        engine_id=registration.engine.engine_id,
                        reads=tuple(sorted(capabilities.reads)),
                        writes=tuple(sorted(capabilities.writes)),
                        experimental=capabilities.experimental,
                        implementation_hash=registration.declared_hash
                        or compute_implementation_hash(registration.variant),
                        output_kinds=capabilities.output_kinds,
                        execution_profiles=tuple(
                            _execution_profile_label(profile) for profile in registration.engine.profiles
                        ),
                        state_profile=_state_profile_descriptor(capabilities.state_profile),
                    )
                )
        return descriptors


def _state_profile_descriptor(profile: object | None) -> dict[str, object] | None:
    """Return a JSON-safe summary of a variant's declared state profile."""
    if profile is None:
        return None
    return {
        "conversions": list(getattr(profile, "conversions", ())),
        "requiredMaterials": list(getattr(profile, "required_materials", ())),
        "allowNewMaterials": bool(getattr(profile, "allow_new_materials", False)),
        "persistence": sorted(getattr(profile, "persistence", ())),
    }


def _execution_profile_label(profile: object) -> str:
    """Return a stable human/API label for one declared execution profile."""
    backend = str(getattr(profile, "backend"))
    kernel = getattr(profile, "kernel", None)
    strategy = getattr(profile, "strategy", None)
    label = f"{backend}:{kernel}" if kernel else backend
    if strategy:
        label = f"{label}:{strategy}"
    return label


def _semver_key(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def _latest_version(versions: list[str]) -> str:
    """Pick the highest semver, or the last registered version otherwise."""
    keys = [_semver_key(version) for version in versions]
    if all(key is not None for key in keys):
        return max(zip(keys, versions), key=lambda item: item[0])[1]  # type: ignore[arg-type]
    return versions[-1]


def _source_file(cls: type) -> str | None:
    """Return the class source file, isolated for tests and fallbacks."""
    return inspect.getsourcefile(cls)


def compute_implementation_hash(variant: object) -> str:
    """Hash the variant implementation, not just its class body.

    For a plugin package (a directory below ``plugins/`` with a
    ``plugin.toml``), every ``.py`` file in that package is hashed, sorted by
    relative path. This covers ``step.py``, ``params.py``, ``spec.py``,
    ``register.py`` and module-level helpers that the variant calls. Any
    explicit ``implementation_files`` declared on the variant are hashed as
    well (the isotropic variant uses this for the ViennaPS engine helpers).

    For non-package variants (for example the in-tree voxel probe), the
    variant source file plus its qualified class name is used. Fallbacks:

    1. class source, when the file cannot be read;
    2. the qualified class name, when source is unavailable entirely.

    The result is a 64-character lowercase SHA-256 hex digest.
    """
    cls = type(variant)
    qualname = f"{cls.__module__}.{cls.__qualname__}"
    suffix = qualname.encode("utf-8")

    source_file = _source_file(cls)
    if source_file:
        source_path = Path(source_file).resolve()
        plugin_root = _plugin_root(source_path)
        digest = hashlib.sha256()
        if plugin_root is not None:
            for path in sorted(plugin_root.rglob("*.py")):
                relative = path.relative_to(plugin_root).as_posix().encode("utf-8")
                digest.update(relative + b"\x00" + path.read_bytes() + b"\x00")
        else:
            try:
                digest.update(b"variant-source\x00" + source_path.read_bytes() + b"\x00")
            except OSError:
                source = inspect.getsource(cls)
                digest.update(b"variant-source\x00" + source.encode("utf-8") + b"\x00")
        for label, path in _implementation_files(variant):
            digest.update(label.encode("utf-8") + b"\x00" + path.read_bytes() + b"\x00")
        digest.update(suffix)
        return digest.hexdigest()

    try:
        source = inspect.getsource(cls)
    except (OSError, TypeError):
        return hashlib.sha256(suffix).hexdigest()
    return hashlib.sha256(source.encode("utf-8") + b"\x00" + suffix).hexdigest()


def _plugin_root(source_path: Path) -> Path | None:
    """Return the nearest ancestor directory containing ``plugin.toml``."""
    for parent in source_path.parents:
        if (parent / "plugin.toml").is_file():
            return parent
    return None


def _implementation_files(variant: object) -> tuple[tuple[str, Path], ...]:
    """Return ``(stable_label, path)`` pairs declared by the variant.

    A declaration may be a path (the label is the repo-relative path when
    possible, otherwise the basename) or an explicit ``(label, path)`` pair.
    A missing declared file is an error rather than a silent omission: the
    lock must never pass while a declared implementation file is absent.
    """
    values = getattr(variant, "implementation_files", ()) or ()
    files: list[tuple[str, Path]] = []
    for value in values:
        if isinstance(value, (tuple, list)):
            if len(value) != 2:
                raise ValueError(f"implementation_files entries must be paths or (label, path) pairs, got {value!r}")
            label, raw_path = value
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"implementation_files label must be a non-empty string, got {label!r}")
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file():
                raise ValueError(f"declared implementation file {raw_path!r} does not exist")
            files.append((label, path))
            continue
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f"declared implementation file {value!r} does not exist")
        try:
            label = path.relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            label = path.name
        files.append((label, path))
    return tuple(files)
