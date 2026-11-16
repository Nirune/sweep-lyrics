"""Switchable lyric animations.

The registry is the single place that knows which styles exist. Adding a
style means writing one module and adding its class to :data:`REGISTRY` —
nothing in the render engine changes.

Selection order (highest first): explicit argument, ``SWEEP_ANIMATION``
environment variable, :data:`DEFAULT_ANIMATION`. Unknown names fall back to
the default instead of raising, so a bad setting can never blank the screen.
"""

from __future__ import annotations

import os
from typing import Dict, Tuple, Type

from .base import AnimFrame, AnimState, LyricAnimation, WordSpan, resolve_word_spans
from .dot import BouncingDot
from .none import NoneAnimation
from .wipe import Wipe

__all__ = [
    "AnimFrame",
    "AnimState",
    "LyricAnimation",
    "WordSpan",
    "resolve_word_spans",
    "REGISTRY",
    "DEFAULT_ANIMATION",
    "ENV_VAR",
    "register",
    "animation_names",
    "get_animation",
    "resolve_animation",
    "env_animation",
    "next_animation",
]

ENV_VAR = "SWEEP_ANIMATION"
DEFAULT_ANIMATION = "wipe"

# name -> class. Ordered: this is also the runtime cycle order.
REGISTRY: Dict[str, Type[LyricAnimation]] = {
    a.name: a for a in (Wipe, BouncingDot, NoneAnimation)
}


def register(cls: Type[LyricAnimation]) -> Type[LyricAnimation]:
    """Add an animation class. Returns it so this can be used as a decorator."""
    if not getattr(cls, "name", None):
        raise ValueError("animation class needs a non-empty `name`")
    REGISTRY[cls.name] = cls
    return cls


def animation_names() -> Tuple[str, ...]:
    return tuple(REGISTRY)


def _normalize(name) -> str:
    return (name or "").strip().lower()


def get_animation(name, default: str = DEFAULT_ANIMATION) -> LyricAnimation:
    """Instantiate the named animation, falling back to ``default``."""
    key = _normalize(name)
    cls = REGISTRY.get(key) or REGISTRY.get(_normalize(default)) or REGISTRY[DEFAULT_ANIMATION]
    return cls()


def env_animation() -> str:
    """The animation named by ``SWEEP_ANIMATION`` (validated), else the default."""
    name = _normalize(os.environ.get(ENV_VAR, ""))
    return name if name in REGISTRY else DEFAULT_ANIMATION


def resolve_animation(name=None) -> LyricAnimation:
    """Pick an animation: explicit name, then env var, then default."""
    if _normalize(name) in REGISTRY:
        return get_animation(name)
    if _normalize(name):
        return get_animation(name)  # unknown explicit -> default, never crash
    return get_animation(env_animation())


def next_animation(name) -> str:
    """Name of the next style in cycle order (wraps around)."""
    names = animation_names()
    key = _normalize(name)
    if key not in REGISTRY:
        return names[0]
    return names[(names.index(key) + 1) % len(names)]
