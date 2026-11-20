"""Exact, causal momentum ranks per MomentumSpec over a supplied eligible set.

Observations are window-scoped (D-06): min_observations counts valid bars in the
closed window [anchor, as_of] and None requires every session in that window.
History counts are diagnostic only. Basis provenance (D-08) names the executed
price source, not merely the configuration label. The identity of a caller callable
includes its frozen live state: bound-method receivers (instance ``__dict__`` and
``__slots__``), callable-object state, ``functools.partial`` arguments and keywords,
statically inspectable closure cells, function defaults and function attributes.
Inert immutable global dependencies are included as before. A caller attestation
(``basis_id``/``basis_parameters``) is metadata, never a substitute for live state
that cannot be frozen. If any reachable state is opaque or mutable in a way that
cannot be canonically encoded - a C object, generator, thread lock, module, mutable
global, builtin callable or dynamic namespace lookup - the legacy route fails closed
and directs the caller to ``rank_admitted_momentum`` or ``rank_formula_momentum``
rather than publishing an ambiguous provenance.

Production admission is separate from that legacy path. ``rank_admitted_momentum``
admits only the builtin raw close basis, and ``rank_formula_momentum`` admits only a
validated strict Formula whose intrinsic digest is the basis identity. Neither
production entry point accepts a caller callable or a caller ``basis_id``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import builtins
import dis
import json
from fractions import Fraction
from functools import partial
import hashlib
import inspect
import types
from types import MappingProxyType
import textwrap
from typing import Callable, Iterable, Mapping
from collections.abc import Mapping as MappingABC

from nse_pipeline.specs import MomentumSpec

from .accounting_types import identifier
from .evidence import ObservationLedger
from .formulas import Formula, FormulaBasis, admit_price_inputs, evaluate_formula_prices

BasisPrice = Callable[[str, date, date], "Decimal | None"]

_BUILTIN_BASES = ("close", "adj_close", "total_close")
_HEX_DIGITS = frozenset("0123456789abcdef")
_SAFE_GLOBAL_TYPE_MODULES = frozenset({"builtins", "datetime", "decimal", "fractions"})
_DYNAMIC_NAMESPACE_NAMES = frozenset(
    {"__import__", "compile", "eval", "exec", "globals", "locals", "vars"})
_MAX_STATE_DEPTH = 64


def _freeze_parameters(value):
    """Copy caller parameters into an immutable structure without changing values."""
    if isinstance(value, Mapping):
        frozen = {}
        for key, item in value.items():
            frozen[identifier(key)] = _freeze_parameters(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_parameters(item) for item in value)
    if isinstance(value, (set, frozenset)):
        return frozenset(_freeze_parameters(item) for item in value)
    return value


@dataclass(frozen=True)
class BasisProvenance:
    """Truthful record of the price basis that produced a ranking."""

    kind: str
    basis_id: str | None
    name: str
    version: str | None = None
    source_digest: str | None = None
    parameters: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self):
        if self.kind not in ("builtin", "callable", "composite"):
            raise ValueError("BasisProvenance.kind must be builtin, callable or composite")
        identifier(self.name)
        if self.kind == "builtin":
            if self.basis_id is not None:
                raise ValueError("A built-in basis must not carry a basis_id")
        else:
            identifier(self.basis_id)
        if self.version is not None:
            identifier(self.version)
        if self.source_digest is not None:
            if (not isinstance(self.source_digest, str) or len(self.source_digest) != 64
                    or any(character not in _HEX_DIGITS for character in self.source_digest)):
                raise ValueError("source_digest must be a lowercase SHA-256 digest or None")
        object.__setattr__(self, "parameters", _freeze_parameters(self.parameters))

    @property
    def price_basis(self) -> str:
        """Backward-compatible label: built-ins keep the spec string, callables are custom."""
        return self.name if self.kind == "builtin" else f"custom:{self.basis_id}"

    @property
    def reproducible(self) -> bool:
        """A missing digest is an explicit unknown and claims no reproducibility."""
        return self.source_digest is not None


def _callable_name(basis_price) -> str:
    target = _unwrap_partial(basis_price)
    if inspect.ismethod(target):
        target = target.__func__
    module = getattr(target, "__module__", None)
    qualname = getattr(target, "__qualname__", None) or getattr(target, "__name__", None)
    if not isinstance(module, str) or not module:
        module = type(target).__module__
    if not isinstance(qualname, str) or not qualname:
        qualname = type(target).__qualname__
    return f"{module}.{qualname}"


def _unwrap_partial(basis_price):
    target = basis_price
    while isinstance(target, partial):
        target = target.func
    return target


def _has_unsafe_global_dependencies(basis_price) -> bool:
    function = _unwrap_partial(basis_price)
    if not inspect.isfunction(function):
        return False
    for instruction in dis.get_instructions(function):
        if instruction.opname in ("LOAD_GLOBAL", "LOAD_NAME"):
            name = instruction.argval
            if name not in function.__globals__:
                if hasattr(builtins, name):
                    continue
                return True
            value = function.__globals__[name]
            if isinstance(value, type) and value.__module__ == "builtins":
                continue
            if _canonical_global_value(value) is None:
                return True
    return False


def _requires_basis_id(basis_price) -> bool:
    """Closures, lambdas, partials, bound methods and stateful/opaque callables are ambiguous."""
    if isinstance(basis_price, partial):
        return True
    if inspect.isfunction(basis_price):
        function = basis_price
        if function.__name__ == "<lambda>":
            return True
        if function.__closure__ or function.__code__.co_freevars:
            return True
        if "<locals>" in function.__qualname__:
            return True
        return _has_unsafe_global_dependencies(function)
    if inspect.isbuiltin(basis_price):
        return False
    return True


class _UnfreezableCallableState(Exception):
    """Internal signal that a legacy callable's live identity cannot be frozen."""


def _type_label(value) -> list[str]:
    value_type = value if isinstance(value, type) else type(value)
    module = getattr(value_type, "__module__", None)
    qualname = getattr(value_type, "__qualname__", None)
    if not isinstance(module, str):
        module = "?"
    if not isinstance(qualname, str):
        qualname = getattr(value_type, "__name__", "?")
    return ["type", module, qualname]


def _json_sort_key(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _canonical_items(values, *, where, seen, depth):
    items = [_canonical_state(item, where=f"{where}[{index}]", seen=seen, depth=depth + 1)
             for index, item in enumerate(values)]
    items.sort(key=_json_sort_key)
    return items


def _canonical_pairs(mapping, *, where, seen, depth):
    pairs = []
    for index, (key, item) in enumerate(mapping.items()):
        pairs.append((
            _canonical_state(key, where=f"{where}.key[{index}]", seen=seen, depth=depth + 1),
            _canonical_state(item, where=f"{where}.value[{index}]", seen=seen, depth=depth + 1),
        ))
    pairs.sort(key=lambda pair: _json_sort_key(pair[0]))
    return [[key, item] for key, item in pairs]


def _canonical_public_class_state(value, *, where, seen, depth):
    """Canonicalize public non-descriptor class data that can affect behaviour."""
    entries = []
    for cls in type(value).__mro__:
        for name, attribute in cls.__dict__.items():
            if name.startswith("_") or hasattr(attribute, "__get__"):
                continue
            entries.append((f"{cls.__qualname__}.{name}", _canonical_state(
                attribute, where=f"{where}.{name}", seen=seen, depth=depth + 1)))
    return [[name, encoded] for name, encoded in sorted(entries)]


def _canonical_instance_state(value, *, where, seen, depth):
    """Canonicalize instance ``__dict__``, ``__slots__`` and public class data."""
    marker = id(value)
    if marker in seen:
        raise _UnfreezableCallableState(f"{where}: cyclic state")
    seen.add(marker)
    try:
        parts = []
        try:
            instance_dict = object.__getattribute__(value, "__dict__")
        except AttributeError:
            instance_dict = None
        except Exception as exc:
            raise _UnfreezableCallableState(
                f"{where}: __dict__ cannot be read ({type(exc).__name__})") from None
        if instance_dict is not None:
            if not isinstance(instance_dict, MappingABC):
                raise _UnfreezableCallableState(
                    f"{where}: __dict__ is not a mapping "
                    f"({type(instance_dict).__name__})")
            parts.append(["__dict__", _canonical_pairs(
                instance_dict, where=f"{where}.__dict__", seen=seen, depth=depth + 1)])
        slot_parts = []
        declared_slots = False
        declared_slot_names = set()
        for cls in type(value).__mro__:
            raw_slots = cls.__dict__.get("__slots__")
            if raw_slots is None:
                continue
            declared_slots = True
            if isinstance(raw_slots, str):
                slot_names = (raw_slots,)
            else:
                try:
                    slot_names = tuple(raw_slots)
                except TypeError:
                    raise _UnfreezableCallableState(
                        f"{where}: unreadable __slots__ on {cls.__qualname__}") from None
            for name in slot_names:
                if name in ("__dict__", "__weakref__"):
                    continue
                candidates = [name]
                if name.startswith("__") and not name.endswith("__"):
                    candidates.insert(0, f"_{cls.__name__.lstrip('_')}{name}")
                declared_slot_names.update(candidates)
                slot_value = None
                found = False
                for candidate in candidates:
                    try:
                        slot_value = object.__getattribute__(value, candidate)
                    except AttributeError:
                        continue
                    except Exception as exc:
                        raise _UnfreezableCallableState(
                            f"{where}: slot {name!r} cannot be read "
                            f"({type(exc).__name__})") from None
                    found = True
                    break
                if not found:
                    continue
                slot_parts.append((f"{cls.__qualname__}.{name}", _canonical_state(
                    slot_value, where=f"{where}.{name}", seen=seen, depth=depth + 1)))
        allowed_descriptors = {"__class__", "__dict__", "__weakref__"}
        for cls in type(value).__mro__:
            for name, descriptor in cls.__dict__.items():
                if not isinstance(descriptor, (types.GetSetDescriptorType,
                                               types.MemberDescriptorType)):
                    continue
                if name in allowed_descriptors or name in declared_slot_names:
                    continue
                raise _UnfreezableCallableState(
                    f"{where}: {'.'.join(_type_label(value)[1:])} exposes native "
                    f"descriptor {name!r} outside Python-visible state")
        if instance_dict is None and not declared_slots:
            raise _UnfreezableCallableState(
                f"{where}: {'.'.join(_type_label(value)[1:])} exposes no inspectable "
                "instance state")
        if slot_parts:
            parts.append(["__slots__", [[name, encoded]
                                        for name, encoded in sorted(slot_parts)]])
        elif declared_slots:
            parts.append(["__slots__", []])
        class_parts = _canonical_public_class_state(
            value, where=f"{where}.class", seen=seen, depth=depth + 1)
        if class_parts:
            parts.append(["class", class_parts])
        return parts
    finally:
        seen.remove(marker)


def _canonical_state(value, *, where, seen=None, depth=0):
    """Return an address-free, deterministic JSON value for live callable state.

    Type-tagged values keep ``bool`` distinct from ``int``, preserve ``Decimal``
    exactly, and make sets/frozensets/mappings order-independent. Unsupported or
    opaque values raise instead of silently collapsing distinct live state.
    """
    if depth > _MAX_STATE_DEPTH:
        raise _UnfreezableCallableState(
            f"{where}: nesting exceeds {_MAX_STATE_DEPTH} levels")
    if seen is None:
        seen = set()
    value_type = type(value)
    if value is None:
        return ["none"]
    if value_type is bool:
        return ["bool", value]
    if value_type is int:
        return ["int", str(value)]
    if value_type is float:
        return ["float", value.hex()]
    if value_type is complex:
        return ["complex", value.real.hex(), value.imag.hex()]
    if value_type is str:
        return ["str", value]
    if value_type is bytes:
        return ["bytes", value.hex()]
    if value_type is bytearray:
        return ["bytearray", bytes(value).hex()]
    if isinstance(value, Decimal):
        return ["decimal", str(value)]
    if isinstance(value, Fraction):
        return ["fraction", str(value.numerator), str(value.denominator)]
    if value_type is date:
        return ["date", value.isoformat()]
    if value_type is datetime:
        return ["datetime", value.isoformat()]
    if value_type is time:
        return ["time", value.isoformat()]
    if value_type is timedelta:
        return ["timedelta", value.days, value.seconds, value.microseconds]
    if value_type is range:
        return ["range", value.start, value.stop, value.step]
    if isinstance(value, type):
        return _type_label(value)
    if isinstance(value, (types.ModuleType, types.FunctionType, types.BuiltinFunctionType,
                          types.MethodType, types.GeneratorType, types.CoroutineType,
                          types.AsyncGeneratorType, partial)):
        raise _UnfreezableCallableState(
            f"{where}: {'.'.join(_type_label(value)[1:])} is opaque callable or module state")
    if isinstance(value, (tuple, list, set, frozenset)):
        marker = id(value)
        if marker in seen:
            raise _UnfreezableCallableState(f"{where}: cyclic state")
        seen.add(marker)
        try:
            if isinstance(value, (set, frozenset)):
                items = _canonical_items(value, where=where, seen=seen, depth=depth)
                tag = "set" if isinstance(value, set) else "frozenset"
            else:
                items = [_canonical_state(item, where=f"{where}[{index}]",
                                          seen=seen, depth=depth + 1)
                         for index, item in enumerate(value)]
                tag = "list" if isinstance(value, list) else "tuple"
        finally:
            seen.remove(marker)
        return [tag, _type_label(value), items]
    if isinstance(value, dict):
        marker = id(value)
        if marker in seen:
            raise _UnfreezableCallableState(f"{where}: cyclic state")
        seen.add(marker)
        try:
            pairs = _canonical_pairs(value, where=where, seen=seen, depth=depth)
        finally:
            seen.remove(marker)
        return ["dict", _type_label(value), pairs]
    if isinstance(value, MappingABC):
        marker = id(value)
        if marker in seen:
            raise _UnfreezableCallableState(f"{where}: cyclic state")
        seen.add(marker)
        try:
            pairs = _canonical_pairs(value, where=where, seen=seen, depth=depth)
        finally:
            seen.remove(marker)
        return ["mapping", _type_label(value), pairs]
    return ["instance", _type_label(value),
            _canonical_instance_state(value, where=where, seen=seen, depth=depth + 1)]


def _canonical_global_value(value):
    """Return an address-free JSON value for a safely immutable global, else None."""
    if value is None or type(value) in (bool, int, str):
        return [type(value).__name__, value]
    if isinstance(value, date) and type(value) is date:
        return ["date", value.isoformat()]
    if isinstance(value, type):
        module = getattr(value, "__module__", None)
        qualname = getattr(value, "__qualname__", None)
        if module in _SAFE_GLOBAL_TYPE_MODULES and isinstance(qualname, str):
            return ["type", module, qualname]
        return None
    if type(value) is float:
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return ["float", value.hex()]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if type(value) is tuple:
        items = [_canonical_global_value(item) for item in value]
        return None if any(item is None for item in items) else ["tuple", items]
    if type(value) is frozenset:
        items = [_canonical_global_value(item) for item in value]
        if any(item is None for item in items):
            return None
        return ["frozenset", sorted(items, key=lambda item: json.dumps(item, sort_keys=True))]
    return None


def _iter_code_objects(code):
    yield code
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from _iter_code_objects(constant)


def _global_dependencies(function) -> dict[str, object]:
    """Canonical values of every directly referenced global, or fail closed."""
    dependencies = {}
    for code in _iter_code_objects(function.__code__):
        for instruction in dis.get_instructions(code):
            if instruction.opname in ("IMPORT_NAME", "IMPORT_FROM"):
                raise _UnfreezableCallableState(
                    f"{function.__qualname__}: dynamic import {instruction.argval!r} "
                    "cannot be frozen")
            if instruction.opname not in ("LOAD_GLOBAL", "LOAD_NAME"):
                continue
            name = instruction.argval
            if name in _DYNAMIC_NAMESPACE_NAMES:
                raise _UnfreezableCallableState(
                    f"{function.__qualname__}: dynamic namespace access {name!r} "
                    "cannot be frozen")
            if name not in function.__globals__:
                if hasattr(builtins, name):
                    continue
                raise _UnfreezableCallableState(
                    f"{function.__qualname__}: global {name!r} is unresolved")
            value = function.__globals__[name]
            if isinstance(value, type) and value.__module__ in _SAFE_GLOBAL_TYPE_MODULES:
                continue
            frozen = _canonical_global_value(value)
            if frozen is None:
                raise _UnfreezableCallableState(
                    f"{function.__qualname__}: global {name!r} has opaque or mutable "
                    f"state of type {'.'.join(_type_label(value)[1:])}")
            dependencies[name] = frozen
    return dependencies


def _function_live_state(function):
    """Canonical closures, defaults and function attributes, or None when inert."""
    parts = []
    if function.__defaults__ is not None:
        parts.append(["defaults", _canonical_state(
            function.__defaults__, where=f"{function.__qualname__} defaults")])
    if function.__kwdefaults__ is not None:
        parts.append(["kwdefaults", _canonical_state(
            function.__kwdefaults__, where=f"{function.__qualname__} keyword defaults")])
    freevars = function.__code__.co_freevars
    if freevars:
        closure = function.__closure__
        if closure is None or len(closure) != len(freevars):
            raise _UnfreezableCallableState(
                f"{function.__qualname__}: closure cells do not match free variables")
        cells = []
        for name, cell in zip(freevars, closure):
            try:
                value = cell.cell_contents
            except ValueError:
                raise _UnfreezableCallableState(
                    f"{function.__qualname__}: closure cell {name!r} is empty") from None
            cells.append([name, _canonical_state(
                value, where=f"{function.__qualname__} closure {name}")])
        parts.append(["closures", sorted(cells, key=lambda item: item[0])])
    attributes = getattr(function, "__dict__", None)
    if attributes:
        if not isinstance(attributes, MappingABC):
            raise _UnfreezableCallableState(
                f"{function.__qualname__}: function attributes are not a mapping")
        parts.append(["attributes", _canonical_pairs(
            attributes, where=f"{function.__qualname__} attributes")])
    return ["function", parts] if parts else None


def _callable_function_and_state(basis_price):
    """Return the source function and canonical live state for a callable basis."""
    if isinstance(basis_price, partial):
        layers = []
        target = basis_price
        while isinstance(target, partial):
            attributes = getattr(target, "__dict__", None) or {}
            layers.append({"args": target.args, "keywords": target.keywords,
                           "attributes": dict(attributes)})
            target = target.func
        function, target_state = _callable_function_and_state(target)
        state = {"kind": "partial",
                 "layers": _canonical_state(layers, where="partial layers")}
        if target_state is not None:
            state["target"] = target_state
        return function, state
    if inspect.ismethod(basis_price):
        function = basis_price.__func__
        if not inspect.isfunction(function):
            raise _UnfreezableCallableState(
                f"{_callable_name(basis_price)}: bound method is not Python code")
        if isinstance(basis_price.__self__, type):
            raise _UnfreezableCallableState(
                f"{_callable_name(basis_price)}: class-bound method receiver is not freezeable")
        state = {"kind": "bound_method",
                 "receiver": _canonical_state(
                     basis_price.__self__,
                     where=f"{_callable_name(basis_price)} receiver")}
        function_state = _function_live_state(function)
        if function_state is not None:
            state["function"] = function_state
        return function, state
    if inspect.isfunction(basis_price):
        return basis_price, _function_live_state(basis_price)
    if inspect.isbuiltin(basis_price):
        raise _UnfreezableCallableState(
            f"{_callable_name(basis_price)}: built-in callables have no inspectable source")
    if callable(basis_price):
        call = getattr(type(basis_price), "__call__", None)
        if not inspect.isfunction(call):
            raise _UnfreezableCallableState(
                f"{_callable_name(basis_price)}: callable object __call__ is not Python code")
        state = {"kind": "callable_object",
                 "receiver": _canonical_state(
                     basis_price, where=f"{_callable_name(basis_price)} state")}
        function_state = _function_live_state(call)
        if function_state is not None:
            state["call"] = function_state
        return call, state
    raise _UnfreezableCallableState(
        f"{_callable_name(basis_price)}: basis_price is not callable")


def _normalized_source_digest(basis_price) -> str:
    """SHA-256 over normalized source, globals and frozen live callable state."""
    function, live_state = _callable_function_and_state(basis_price)
    try:
        source = inspect.getsource(function)
    except (OSError, TypeError) as exc:
        raise _UnfreezableCallableState(
            f"{_callable_name(basis_price)}: source is unavailable "
            f"({type(exc).__name__})") from None
    normalized = textwrap.dedent(source.replace("\r\n", "\n").replace("\r", "\n"))
    normalized = "\n".join(line.rstrip() for line in normalized.split("\n")).strip("\n") + "\n"
    source_digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    dependencies = _global_dependencies(function)
    if dependencies:
        encoded = json.dumps(dependencies, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True).encode("utf-8")
        global_digest = hashlib.sha256(encoded).hexdigest()
        source_digest = hashlib.sha256(
            f"{source_digest}:{global_digest}".encode("ascii")).hexdigest()
    if live_state is not None:
        try:
            encoded_state = json.dumps(live_state, sort_keys=True, separators=(",", ":"),
                                       ensure_ascii=True).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise _UnfreezableCallableState(
                f"{_callable_name(basis_price)}: live state cannot be encoded "
                f"({type(exc).__name__})") from None
        state_digest = hashlib.sha256(encoded_state).hexdigest()
        source_digest = hashlib.sha256(
            f"{source_digest}:{state_digest}".encode("ascii")).hexdigest()
    return source_digest


def basis_provenance(spec: MomentumSpec, basis_price: BasisPrice | None = None, *,
                     basis_id: str | None = None, basis_version: str | None = None,
                     basis_parameters: Mapping[str, object] | None = None) -> BasisProvenance:
    """Resolve the executed price basis before any bar is read.

    Built-ins keep spec.price_basis and have basis_id None. Callables always publish
    custom:<basis_id>, never the spec label. The source digest binds normalized source,
    inert immutable global dependencies, and canonical live callable state. Caller
    basis_id and basis_parameters are recorded metadata; they never substitute for
    state that cannot be frozen. A callable whose reachable state is opaque, mutable
    in an unsupported way, dynamically resolved, or has no inspectable source raises
    ``ValueError`` before any bar is read and directs the caller to the production
    ranks. Every admitted callable therefore carries a reproducible digest.
    """
    if not isinstance(spec, MomentumSpec):
        raise ValueError("Basis provenance needs a MomentumSpec")
    if basis_parameters is not None and not isinstance(basis_parameters, Mapping):
        raise ValueError("basis_parameters must be a mapping")
    parameters = {} if basis_parameters is None else basis_parameters
    if basis_price is None:
        if spec.price_basis not in _BUILTIN_BASES:
            raise ValueError(f"price_basis {spec.price_basis!r} requires a caller-supplied basis_price")
        if basis_id is not None:
            raise ValueError("A built-in basis must not carry a basis_id")
        if basis_parameters:
            raise ValueError("A built-in basis must not carry transformation parameters")
        return BasisProvenance("builtin", None, spec.price_basis, basis_version)
    if not callable(basis_price):
        raise ValueError("basis_price must be callable")
    try:
        source_digest = _normalized_source_digest(basis_price)
    except _UnfreezableCallableState as exc:
        reason = exc
    except Exception as exc:
        reason = _UnfreezableCallableState(
            f"{type(basis_price).__module__}.{type(basis_price).__qualname__}: "
            f"live state inspection failed ({type(exc).__name__})")
    else:
        reason = None
    if reason is not None:
        raise ValueError(
            "callable_basis_state_not_freezable: "
            f"{reason}. The legacy rank_momentum callable basis cannot publish an "
            "unambiguous provenance; use rank_admitted_momentum or "
            "rank_formula_momentum for production admission.") from None
    if basis_id is None:
        if _requires_basis_id(basis_price):
            raise ValueError("basis_id_required")
        basis_id = _callable_name(basis_price)
    else:
        identifier(basis_id)
    return BasisProvenance(kind="callable", basis_id=basis_id, name=_callable_name(basis_price),
                           version=basis_version, source_digest=source_digest,
                           parameters=parameters)


def _verify_callable_provenance_unchanged(spec, basis_price, provenance, *,
                                          basis_version, basis_parameters) -> None:
    """Fail closed if ranking left the admitted callable state or attestation changed."""
    try:
        current = basis_provenance(
            spec, basis_price, basis_id=provenance.basis_id,
            basis_version=basis_version, basis_parameters=basis_parameters)
    except ValueError as exc:
        raise ValueError(
            "callable_basis_state_changed_during_ranking: "
            f"{exc}; the legacy rank_momentum callable basis cannot publish a stale "
            "provenance; use rank_admitted_momentum or rank_formula_momentum for "
            "production admission.") from None
    if current != provenance:
        raise ValueError(
            "callable_basis_state_changed_during_ranking: the live callable state or "
            "attestation differs after ranking; the legacy rank_momentum callable basis "
            "cannot publish a stale provenance; use rank_admitted_momentum or "
            "rank_formula_momentum for production admission.")


@dataclass(frozen=True)
class KeyObservations:
    """Diagnostic counts for one eligible key; only the window count may gate ranking."""

    key: str
    anchor: date | None
    window_sessions: int
    window_valid_observations: int
    history_valid_observations: int
    history_left_censored: bool

    @property
    def valid_observations(self) -> int:
        return self.window_valid_observations


@dataclass(frozen=True)
class RankedKey:
    rank: int
    key: str
    momentum: Fraction
    price: Decimal | Fraction
    anchor_price: Decimal | Fraction
    valid_observations: int
    observations_lower_bound: bool
    history_valid_observations: int = 0


@dataclass(frozen=True)
class MomentumRanks:
    as_of: date
    anchor: date | None
    price_basis: str
    ranked: tuple[RankedKey, ...]
    unrankable: tuple[tuple[str, str], ...]
    window_sessions: int = 0
    basis_provenance: BasisProvenance | None = None
    observations: tuple[KeyObservations, ...] = ()
    formula_basis: FormulaBasis | None = None

    @property
    def order(self) -> tuple[str, ...]:
        return tuple(r.key for r in self.ranked)

    def count_for(self, key: str) -> KeyObservations:
        wanted = identifier(key)
        for row in self.observations:
            if row.key == wanted:
                return row
        raise KeyError(wanted)


def close_basis(ledger: ObservationLedger) -> BasisPrice:
    """Raw Bar.close of a valid bar on the session; refuses sessions after as_of."""
    def price(key: str, session: date, as_of: date) -> Decimal | None:
        if ledger.session_index(session) > ledger.session_index(as_of):
            raise ValueError("A basis price after the decision session is look-ahead")
        bar = ledger.bar(key, session)
        return bar.close if bar is not None and bar.valid_ohlcv else None
    return price


def _basis(basis_price, key, session, as_of) -> Decimal | None:
    value = basis_price(key, session, as_of)
    if value is not None and (type(value) is not Decimal or not value.is_finite()):
        raise ValueError("basis_price must return a finite Decimal or None")
    return value


def _anchor_exact_session_offset(ledger, as_of_index, lookback_sessions):
    """D-07: the anchor is exactly lookback_sessions sessions back, never a substitute."""
    return ledger.sessions[as_of_index - lookback_sessions] if as_of_index >= lookback_sessions else None


def _observation_valid_window(ledger, key, anchor, as_of, window_sessions):
    """D-06: count valid bars only in the closed observation window."""
    return 0 if anchor is None else ledger.window_valid_count(key, as_of, window_sessions)


def _tie_key_ascending(scored):
    """D-07: descending exact momentum, key ascending; input iterable order is irrelevant."""
    return sorted(scored, key=lambda row: (-row[0], row[1]))


_ANCHOR_RULES = {"exact_session_offset": _anchor_exact_session_offset}
_OBSERVATION_RULES = {"valid_observations": _observation_valid_window}
_TIE_BREAKS = {"key_asc": _tie_key_ascending}


def _dispatch(table, field_name, value):
    implementation = table.get(value)
    if implementation is None:
        raise ValueError(f"{field_name} {value!r} has no implemented dispatch")
    return implementation


def _reason(ledger, key, as_of, anchor, window_valid, minimum) -> str:
    if anchor is None:
        return "no_anchor_session"
    for session, name in ((as_of, "session"), (anchor, "anchor")):
        bar = ledger.bar(key, session)
        if bar is None or not bar.valid_ohlcv:
            return f"no_valid_bar_on_{name}"
    if window_valid < minimum:
        return "insufficient_window_observations"
    return ""


def _eligible_keys(eligible) -> tuple[str, ...]:
    if isinstance(eligible, (str, bytes)) or not hasattr(eligible, "__iter__"):
        raise ValueError("eligible must be an iterable of keys")
    keys = tuple(identifier(k) for k in eligible)
    if len(set(keys)) != len(keys):
        raise ValueError("eligible keys must be unique")
    return keys


def _validate_ranking_inputs(ledger, spec):
    if not isinstance(ledger, ObservationLedger) or not isinstance(spec, MomentumSpec):
        raise ValueError("Ranking needs an ObservationLedger and a MomentumSpec")


def _assemble_ranks(ledger, as_of, keys, spec, *, price_source, price_basis,
                    provenance, formula_basis=None, unavailable_reasons=None):
    """Shared canonical anchor/window/reason/tie economics for every admitted basis.

    ``price_source`` is module-owned and returns an exact number or None. Formula
    prices stay ``Fraction``; legacy built-in/callable prices stay ``Decimal``.
    """
    anchor_rule = _dispatch(_ANCHOR_RULES, "anchor_rule", spec.anchor_rule)
    observation_rule = _dispatch(_OBSERVATION_RULES, "observation_rule", spec.observation_rule)
    tie_break = _dispatch(_TIE_BREAKS, "tie_break", spec.tie_break)

    t = ledger.session_index(as_of)
    anchor = anchor_rule(ledger, t, spec.lookback_sessions)
    window_sessions = t + 1 if anchor is None else spec.lookback_sessions + 1
    minimum = window_sessions if spec.min_observations is None else spec.min_observations

    reasons = {} if unavailable_reasons is None else unavailable_reasons
    scored, unrankable, observations = [], [], []
    for key in sorted(keys):
        window_valid = observation_rule(ledger, key, anchor, as_of, window_sessions)
        history = ledger.history(key, as_of)
        observations.append(KeyObservations(key, anchor, window_sessions, window_valid,
                                            history.valid_observations, history.left_censored))
        reason = _reason(ledger, key, as_of, anchor, window_valid, minimum)
        if not reason:
            price, anchor_price = price_source(key, as_of), price_source(key, anchor)
            if price is None or anchor_price is None:
                failing = (key, as_of) if price is None else (key, anchor)
                reason = (reasons.get(failing)
                          or "price_unavailable_on_" + ("session" if price is None else "anchor"))
            elif price <= 0 or anchor_price <= 0:
                reason = "nonpositive_price"
        if reason:
            unrankable.append((key, reason))
            continue
        scored.append((Fraction(price) / Fraction(anchor_price) - 1, key, price, anchor_price,
                       window_valid, history.left_censored, history.valid_observations))
    ranked = tuple(RankedKey(i, key, momentum, price, anchor_price, window_valid, censored, history_valid)
                   for i, (momentum, key, price, anchor_price, window_valid, censored, history_valid)
                   in enumerate(tie_break(scored), 1))
    return MomentumRanks(as_of, anchor, price_basis, ranked, tuple(unrankable),
                         window_sessions=window_sessions, basis_provenance=provenance,
                         observations=tuple(observations), formula_basis=formula_basis)


def rank_momentum(ledger: ObservationLedger, as_of: date, eligible: Iterable[str],
                  spec: MomentumSpec | None = None, basis_price: BasisPrice | None = None, *,
                  basis_id: str | None = None, basis_version: str | None = None,
                  basis_parameters: Mapping[str, object] | None = None) -> MomentumRanks:
    """Legacy/experimental momentum ranking over a caller-supplied basis.

    Momentum = price(as_of) / price(anchor) - 1, anchored exactly lookback sessions
    back. The observation gate counts only [anchor, as_of]; None requires the full
    window. Ranks are descending momentum with key-ascending ties. The exact anchor is
    never substituted, and an unavailable basis is unrankable rather than filled.

    Caller callables and ``basis_id`` remain admitted here only for backward
    compatibility. They are explicitly experimental and are never production
    admission; use :func:`rank_admitted_momentum` or
    :func:`rank_formula_momentum` instead.
    """
    spec = MomentumSpec() if spec is None else spec
    _validate_ranking_inputs(ledger, spec)
    keys = _eligible_keys(eligible)
    if basis_price is None:
        if spec.price_basis != "close":
            raise ValueError(f"price_basis {spec.price_basis!r} requires a caller-supplied basis_price")
        provenance = basis_provenance(spec, None, basis_id=basis_id, basis_version=basis_version,
                                      basis_parameters=basis_parameters)
        source = close_basis(ledger)
    elif not callable(basis_price):
        raise ValueError("basis_price must be callable")
    else:
        provenance = basis_provenance(spec, basis_price, basis_id=basis_id,
                                      basis_version=basis_version,
                                      basis_parameters=basis_parameters)
        source = basis_price

    ranks = _assemble_ranks(
        ledger, as_of, keys, spec,
        price_source=lambda key, session: _basis(source, key, session, as_of),
        price_basis=provenance.price_basis, provenance=provenance)
    if basis_price is not None:
        _verify_callable_provenance_unchanged(
            spec, basis_price, provenance, basis_version=basis_version,
            basis_parameters=basis_parameters)
    return ranks


def rank_admitted_momentum(ledger: ObservationLedger, as_of: date, eligible: Iterable[str],
                           spec: MomentumSpec | None = None) -> MomentumRanks:
    """Production raw-close ranking: no caller callable or ``basis_id`` is admitted."""
    spec = MomentumSpec() if spec is None else spec
    _validate_ranking_inputs(ledger, spec)
    if spec.price_basis != "close":
        raise ValueError(
            "Production admission supports the builtin raw close basis only; "
            f"not {spec.price_basis!r}")
    keys = _eligible_keys(eligible)
    provenance = basis_provenance(spec, None)
    source = close_basis(ledger)
    return _assemble_ranks(
        ledger, as_of, keys, spec,
        price_source=lambda key, session: _basis(source, key, session, as_of),
        price_basis=provenance.price_basis, provenance=provenance)


def rank_formula_momentum(ledger: ObservationLedger, as_of: date, eligible: Iterable[str],
                          formula: Formula, *, spec: MomentumSpec | None = None,
                          adjustment_evidence=None) -> MomentumRanks:
    """Production ranking over one validated strict formula, with exact prices.

    The executed basis is intrinsic to the formula digest; there is no
    ``basis_id``/callable override. A raw-only formula requires
    ``spec.price_basis == "close"`` and an adjustment-using formula requires
    ``"adj_close"``. Formula evaluation must never be rounded through ``Decimal``.
    """
    spec = MomentumSpec() if spec is None else spec
    _validate_ranking_inputs(ledger, spec)
    if type(formula) is not Formula:
        raise TypeError("formula_ranking_requires_a_validated_formula")
    if formula.uses_raw and formula.uses_adjustment:
        raise ValueError(
            "A formula mixing raw and adjusted price leaves has no unambiguous "
            "momentum.price_basis")
    expected = "adj_close" if formula.uses_adjustment else "close"
    if spec.price_basis != expected:
        declared = "adjusted" if formula.uses_adjustment else "raw"
        raise ValueError(
            f"The declared {declared} formula requires momentum.price_basis "
            f"{expected!r}, not {spec.price_basis!r}")
    if type(as_of) is not date:
        raise ValueError("Ranking needs an explicit as_of date")
    ledger.session_index(as_of)
    keys = _eligible_keys(eligible)

    anchor = _anchor_exact_session_offset(ledger, ledger.session_index(as_of),
                                          spec.lookback_sessions)
    sessions = (as_of,) if anchor is None else (anchor, as_of)
    requests = ((key, session) for key in sorted(keys) for session in sessions)
    inputs = admit_price_inputs(evaluate_formula_prices(
        formula, ledger, requests, as_of, adjustment_evidence=adjustment_evidence))
    values = {(row[0], row[1]): row[2] for row in inputs.values}
    unavailable = {(row[0], row[1]): row[3] for row in inputs.values
                   if row[2] is None and row[3] is not None}
    return _assemble_ranks(
        ledger, as_of, keys, spec,
        price_source=lambda key, session: values.get((key, session)),
        price_basis=f"formula:{inputs.formula_sha256}", provenance=None,
        formula_basis=inputs.basis, unavailable_reasons=unavailable)
