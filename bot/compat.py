"""Adaptations de plateforme, appliquees avant tout import de poke_env.

Sur Termux/Android, pip refuse les wheels manylinux (bionic n'est pas glibc).
orjson n'existe qu'en wheel manylinux ou en source Rust: plutot que d'imposer
une chaine Rust pour faire tourner le bot, on branche un shim stdlib.
"""

import importlib.util
import os
import sys

_COMPAT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "compat")

_applied = False


def ensure_orjson() -> bool:
    """Rend ``import orjson`` possible. Renvoie True si le shim a ete active.

    Idempotent, et sans effet si le vrai orjson est installe.
    """
    global _applied
    if _applied or importlib.util.find_spec("orjson") is not None:
        return _applied
    if _COMPAT_DIR not in sys.path:
        sys.path.append(_COMPAT_DIR)
    _applied = importlib.util.find_spec("orjson") is not None
    return _applied


def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "")
