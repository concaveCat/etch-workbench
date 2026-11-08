"""Engine/runtime boundary for the v2 process-model contract.

An *engine* is the thing that actually hosts an implementation: the installed
ViennaPS module, the Python interpreter, or an external simulator. A variant
declares an :class:`EngineRequirement`; a provider resolves it to a concrete
:class:`EngineLease`. Resolution is strict - there is no silent fallback to a
different engine, version, or backend.

Version matching
----------------
Only ``MAJOR.MINOR.PATCH`` bounds are supported (no ranges, wildcards, or
pre-release ordering). If either the installed version or a requested bound is
not parseable, that bound is satisfied **only by exact string equality**. This
keeps commit-style versions (``"rev-a"``) deterministic without pretending to
understand them.

Phase-1 boundaries
------------------
There is no process launch, no environment management, and no warm pool. A
``StaticEngine`` wraps an already-initialized handle supplied by the caller.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Any, Iterable, Literal, Protocol, runtime_checkable

from .state_backend import HandleStateBackend, StateBackend

__all__ = [
    "Backend",
    "ComputePolicy",
    "ConcreteBackend",
    "Engine",
    "EngineCapabilities",
    "EngineKind",
    "EngineLease",
    "EngineProvider",
    "EngineRequirement",
    "EngineUnavailableError",
    "ExecutionProfile",
    "StaticEngine",
    "StaticEngineProvider",
    "version_satisfies",
]

EngineKind = Literal["native", "surrogate", "external"]
Backend = Literal["cpu", "gpu", "any"]
ConcreteBackend = Literal["cpu", "gpu"]

_SEMVER = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")


class EngineUnavailableError(RuntimeError):
    """Raised when no provider engine satisfies a requirement."""


def _semver_key(version: str) -> tuple[int, int, int] | None:
    match = _SEMVER.match(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups())  # type: ignore[return-value]


def version_satisfies(version: str, min_version: str | None = None, max_version: str | None = None) -> bool:
    """Return whether ``version`` lies within the inclusive ``min``/``max`` bounds.

    Non-semver versions and bounds compare by exact string equality, as
    documented in the module docstring.
    """
    installed = _semver_key(version)
    if min_version is not None:
        floor = _semver_key(min_version)
        if installed is None or floor is None:
            if version != min_version:
                return False
        elif installed < floor:
            return False
    if max_version is not None:
        ceiling = _semver_key(max_version)
        if installed is None or ceiling is None:
            if version != max_version:
                return False
        elif installed > ceiling:
            return False
    return True


@dataclass(frozen=True)
class ExecutionProfile:
    """One concrete way an engine can execute a variant.

    ``backend`` is the host backend. ``kernel`` is engine-defined: ViennaPS uses
    ``CPU_DISK``/``GPU_DISK``/``CPU_TRIANGLE``/``GPU_TRIANGLE``; another engine
    may use any non-empty string. ``device`` is a concrete GPU identity and is
    filled by the platform, never by a variant's declaration. ``strategy`` is an
    engine-defined execution strategy (for example multi-ion split/merge).
    """

    backend: ConcreteBackend
    kernel: str | None = None
    device: str | None = None
    strategy: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in ("cpu", "gpu"):
            raise ValueError(f"execution profile backend must be 'cpu' or 'gpu', got {self.backend!r}")
        for name in ("kernel", "device", "strategy"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"execution profile {name} must be a non-empty string or None")
        if self.backend == "cpu" and self.device is not None:
            raise ValueError("a CPU execution profile cannot pin a device")
        object.__setattr__(self, "kernel", self.kernel or None)
        object.__setattr__(self, "device", self.device or None)
        object.__setattr__(self, "strategy", self.strategy or None)

    def identity(self) -> dict[str, str]:
        """Return the stable JSON-safe fields used by plan identity."""
        return {
            "backend": self.backend,
            "kernel": self.kernel or "",
            "device": self.device or "",
            "strategy": self.strategy or "",
        }


@dataclass(frozen=True)
class ComputePolicy:
    """Platform-resolved compute request for one step.

    ``auto`` keeps the historical provider preference (CPU when available).
    An explicit ``gpu`` never falls back to CPU. ``device`` implies GPU. A
    platform that has already resolved a concrete engine can pass ``cpu`` or
    ``gpu`` directly; a generic caller may leave ``auto``.
    """

    backend: Backend = "auto"
    kernel: str | None = None
    device: str | None = None
    strategy: str | None = None

    def __post_init__(self) -> None:
        if self.backend not in ("auto", "cpu", "gpu"):
            raise ValueError(f"compute policy backend must be 'auto', 'cpu', or 'gpu', got {self.backend!r}")
        for name in ("kernel", "device", "strategy"):
            value = getattr(self, name)
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(f"compute policy {name} must be a non-empty string or None")
        if self.backend == "cpu" and self.device is not None:
            raise ValueError("a CPU compute policy cannot pin a device")
        object.__setattr__(self, "kernel", self.kernel or None)
        object.__setattr__(self, "device", self.device or None)
        object.__setattr__(self, "strategy", self.strategy or None)


@dataclass(frozen=True)
class EngineRequirement:
    """What a variant needs from an engine.

    ``backend="any"`` is the legacy shorthand. New variants declare
    ``profiles`` (the concrete backend/kernel/strategy combinations they can
    drive); the first matching profile is selected. A declared profile must not
    pin a device: devices are resolved by the platform. ``capabilities`` is a
    set of free-form feature names that must all be advertised.
    """

    kind: EngineKind
    engine_id: str
    min_version: str | None = None
    max_version: str | None = None
    backend: Backend = "any"
    capabilities: tuple[str, ...] = ()
    profiles: tuple[ExecutionProfile, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.engine_id, str) or not self.engine_id.strip():
            raise ValueError("engine_id must be a non-empty string")
        if self.kind not in ("native", "surrogate", "external"):
            raise ValueError(f"unknown engine kind {self.kind!r}")
        if self.backend not in ("cpu", "gpu", "any"):
            raise ValueError(f"unknown engine backend {self.backend!r}")
        profiles = tuple(self.profiles)
        for profile in profiles:
            if not isinstance(profile, ExecutionProfile):
                raise TypeError(f"engine profiles must be ExecutionProfile instances, got {type(profile).__name__}")
            if profile.device is not None:
                raise ValueError("a declared execution profile must not pin a device; the platform resolves it")
            if self.backend != "any" and profile.backend != self.backend:
                raise ValueError(
                    f"profile backend {profile.backend!r} contradicts EngineRequirement.backend {self.backend!r}"
                )
        object.__setattr__(self, "capabilities", tuple(str(capability) for capability in self.capabilities))
        object.__setattr__(self, "profiles", profiles)


@dataclass(frozen=True)
class EngineCapabilities:
    """What an engine advertises about itself.

    ``kernels`` and ``devices`` are optional. An empty tuple means the engine
    does not advertise that dimension; when ``kernels`` is non-empty, a
    requested profile kernel must appear in it. ``devices`` lists concrete GPU
    identities for providers that can enumerate them.
    """

    engine_id: str
    version: str
    kind: EngineKind
    backends: tuple[str, ...]
    capabilities: tuple[str, ...] = ()
    kernels: tuple[str, ...] = ()
    devices: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.engine_id, str) or not self.engine_id.strip():
            raise ValueError("engine_id must be a non-empty string")
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("engine version must be a non-empty string")
        backends = tuple(str(backend) for backend in self.backends)
        if not backends:
            raise ValueError("engine backends must not be empty")
        if any(backend not in ("cpu", "gpu", "any") for backend in backends):
            raise ValueError(f"unknown engine backends {backends!r}")
        kernels = tuple(str(kernel) for kernel in self.kernels)
        devices = tuple(str(device) for device in self.devices)
        if any(not kernel.strip() for kernel in kernels):
            raise ValueError(f"engine kernels must be non-empty strings, got {kernels!r}")
        if any(not device.strip() for device in devices):
            raise ValueError(f"engine devices must be non-empty strings, got {devices!r}")
        object.__setattr__(self, "backends", backends)
        object.__setattr__(self, "capabilities", tuple(str(capability) for capability in self.capabilities))
        object.__setattr__(self, "kernels", kernels)
        object.__setattr__(self, "devices", devices)


@runtime_checkable
class EngineLease(Protocol):
    """An acquired engine handle plus the identity needed for replay.

    The lease is deliberately opaque beyond its identity: ``handle`` is
    whatever the implementation needs (the ViennaPS module, a Python module,
    ...). ``release`` is idempotent and safe to call from a context manager.
    """

    engine_id: str
    engine_version: str
    kind: EngineKind
    backend: str
    profile: ExecutionProfile
    state_backend: StateBackend
    #: Optional native cell-set lifecycle supplied by engines that can derive
    #: a ``DenseCellSet`` from their level-set geometry. ``None`` means the
    #: engine has no cell-set field path.
    cellset_backend: Any
    #: Optional engine-neutral surface projection for the viewer. ``None``
    #: means the caller must use the engine's native snapshot path.
    surface_backend: Any
    fingerprint: str
    handle: Any

    def release(self) -> None: ...

    def __enter__(self) -> EngineLease: ...

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None: ...


@runtime_checkable
class Engine(Protocol):
    """A single describable/acquirable engine."""

    def describe(self) -> EngineCapabilities: ...

    def acquire(self, requirement: EngineRequirement, policy: ComputePolicy | None = None) -> EngineLease: ...


@runtime_checkable
class EngineProvider(Protocol):
    """A set of engines that can be acquired by requirement."""

    def engines(self) -> list[EngineCapabilities]: ...

    def acquire(self, requirement: EngineRequirement, policy: ComputePolicy | None = None) -> EngineLease: ...


@dataclass(frozen=True)
class _StaticLease:
    """Lease over an already-initialized handle; releasing is a no-op."""

    engine_id: str
    engine_version: str
    kind: EngineKind
    profile: ExecutionProfile
    state_backend: StateBackend
    fingerprint: str
    handle: Any
    cellset_backend: Any = None
    surface_backend: Any = None

    @property
    def backend(self) -> str:
        return self.profile.backend

    @property
    def kernel(self) -> str | None:
        return self.profile.kernel

    @property
    def device(self) -> str | None:
        return self.profile.device

    @property
    def strategy(self) -> str | None:
        return self.profile.strategy

    def release(self) -> None:
        """Release the lease.

        Static handles are owned by the caller, so this intentionally does
        nothing. It exists so callers can use a uniform ``with`` block.
        """

    def __enter__(self) -> _StaticLease:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.release()


@dataclass(frozen=True)
class StaticEngine:
    """An engine wrapping an already-initialized handle.

    Matching is strict over kind, ``engine_id``, version bounds, backend, and
    advertised capabilities. A non-match raises :class:`EngineUnavailableError`;
    the engine never substitutes another version or backend.
    """

    capabilities: EngineCapabilities
    handle: Any
    fingerprint: str
    state_backend: StateBackend | None = None
    cellset_backend: Any = None
    surface_backend: Any = None

    def __post_init__(self) -> None:
        if self.state_backend is None:
            object.__setattr__(self, "state_backend", HandleStateBackend(self.handle))

    def describe(self) -> EngineCapabilities:
        return self.capabilities

    def acquire(self, requirement: EngineRequirement, policy: ComputePolicy | None = None) -> EngineLease:
        caps = self.capabilities
        reasons: list[str] = []
        if requirement.kind != caps.kind:
            reasons.append(f"kind {requirement.kind!r} != {caps.kind!r}")
        if requirement.engine_id != caps.engine_id:
            reasons.append(f"engine_id {requirement.engine_id!r} != {caps.engine_id!r}")
        if not version_satisfies(caps.version, requirement.min_version, requirement.max_version):
            reasons.append(
                f"version {caps.version!r} outside [{requirement.min_version!r}, {requirement.max_version!r}]"
            )
        missing = sorted(set(requirement.capabilities) - set(caps.capabilities))
        if missing:
            reasons.append(f"missing capabilities {missing!r}")

        profile = _select_profile(requirement, caps, policy)
        if profile is None:
            reasons.append(_profile_mismatch(requirement, caps, policy))
        if reasons or profile is None:
            raise EngineUnavailableError(
                f"engine {caps.engine_id!r} {caps.version!r} does not match: " + "; ".join(reasons)
            )

        return _StaticLease(
            engine_id=caps.engine_id,
            engine_version=caps.version,
            kind=caps.kind,
            profile=profile,
            state_backend=self.state_backend,
            fingerprint=self.fingerprint,
            handle=self.handle,
            cellset_backend=self.cellset_backend,
            surface_backend=self.surface_backend,
        )


class StaticEngineProvider:
    """A fixed set of :class:`StaticEngine` values.

    ``acquire`` returns the first engine that matches the requirement. If none
    matches it raises :class:`EngineUnavailableError`; requests never fall back
    to a different requirement.
    """

    def __init__(self, engines: Iterable[StaticEngine]) -> None:
        self._engines = tuple(engines)
        for engine in self._engines:
            if not isinstance(engine, StaticEngine):
                raise TypeError("StaticEngineProvider accepts StaticEngine instances")

    def engines(self) -> list[EngineCapabilities]:
        """Return the advertised capabilities in registration order."""
        return [engine.describe() for engine in self._engines]

    def acquire(self, requirement: EngineRequirement, policy: ComputePolicy | None = None) -> EngineLease:
        failures: list[str] = []
        for engine in self._engines:
            try:
                return engine.acquire(requirement, policy)
            except EngineUnavailableError as error:
                failures.append(str(error))
        detail = "; ".join(failures) if failures else "no engines are registered"
        raise EngineUnavailableError(
            f"no engine satisfies {requirement.kind!r} {requirement.engine_id!r} "
            f"[{requirement.min_version!r}, {requirement.max_version!r}]: {detail}"
        )


def _select_profile(
    requirement: EngineRequirement,
    caps: EngineCapabilities,
    policy: ComputePolicy | None,
) -> ExecutionProfile | None:
    """Return the first declared or legacy profile matching the request."""
    policy = policy or ComputePolicy()
    candidates = list(requirement.profiles)
    if candidates:
        if policy.backend != "auto":
            candidates = [profile for profile in candidates if profile.backend == policy.backend]
        if policy.kernel is not None:
            candidates = [profile for profile in candidates if profile.kernel == policy.kernel]
        if policy.strategy is not None:
            candidates = [profile for profile in candidates if profile.strategy == policy.strategy]
    else:
        backend = policy.backend if policy.backend != "auto" else requirement.backend
        resolved = _resolve_backend(backend, caps.backends)
        if resolved is None:
            return None
        candidates = [
            ExecutionProfile(
                backend=resolved,
                kernel=policy.kernel,
                strategy=policy.strategy,
            )
        ]

    selected: list[ExecutionProfile] = []
    for profile in candidates:
        if profile.backend not in caps.backends and "any" not in caps.backends:
            continue
        if caps.kernels:
            if profile.kernel is None:
                if policy.kernel is not None:
                    continue
            elif profile.kernel not in caps.kernels:
                continue
        if policy.device is not None:
            if profile.backend != "gpu":
                continue
            if caps.devices and policy.device not in caps.devices:
                continue
        selected.append(profile)
    if not selected:
        return None

    profile = selected[0]
    device = policy.device
    if profile.backend == "gpu" and device is None and caps.devices:
        device = caps.devices[0]
    if device is not None:
        if profile.backend != "gpu":
            return None
        if caps.devices and device not in caps.devices:
            return None
        profile = replace(profile, device=device)
    return profile


def _profile_mismatch(
    requirement: EngineRequirement,
    caps: EngineCapabilities,
    policy: ComputePolicy | None,
) -> str:
    policy = policy or ComputePolicy()
    requested = (
        f"profiles {[profile.identity() for profile in requirement.profiles]!r}"
        if requirement.profiles
        else f"backend {requirement.backend!r}"
    )
    return (
        f"no execution profile matches {requested} with policy "
        f"backend={policy.backend!r} kernel={policy.kernel!r} device={policy.device!r} "
        f"strategy={policy.strategy!r}; engine advertises backends={list(caps.backends)!r} "
        f"kernels={list(caps.kernels)!r} devices={list(caps.devices)!r}"
    )


def _resolve_backend(requested: Backend, available: tuple[str, ...]) -> str | None:
    """Return the backend to expose, or ``None`` when the requirement cannot match."""
    if requested == "any":
        if not available:
            return "any"
        if "cpu" in available:
            return "cpu"
        return available[0]
    if requested in available or "any" in available:
        return requested
    return None
