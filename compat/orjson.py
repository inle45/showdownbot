"""Repli stdlib pour ``orjson``, utilise uniquement sur les plateformes ou le
vrai paquet n'est pas installable sans chaine Rust (typiquement Termux/Android,
qui rejette les wheels manylinux).

poke-env 0.16.1 n'utilise d'orjson que ``loads`` et ``JSONDecodeError``
(poke_env/data/gen_data.py, poke_env/data/smogon.py, poke_env/player/player.py).
``dumps`` est fourni par completude, avec la meme signature de retour que le vrai
orjson: des ``bytes``, pas une ``str``.

Ce module n'est ajoute au sys.path que par bot.compat.ensure_orjson(), et
seulement si le vrai orjson est introuvable.
"""

import json as _json

__all__ = ["loads", "dumps", "JSONDecodeError", "OPT_INDENT_2", "OPT_SORT_KEYS"]

JSONDecodeError = _json.JSONDecodeError

OPT_INDENT_2 = 1
OPT_SORT_KEYS = 2


def loads(obj):
    """Accepte str, bytes ou bytearray, comme orjson."""
    if isinstance(obj, (bytes, bytearray, memoryview)):
        obj = bytes(obj).decode("utf-8")
    return _json.loads(obj)


def dumps(obj, default=None, option=0):
    """Renvoie des bytes, comme orjson."""
    return _json.dumps(
        obj,
        default=default,
        indent=2 if option & OPT_INDENT_2 else None,
        sort_keys=bool(option & OPT_SORT_KEYS),
        separators=None if option & OPT_INDENT_2 else (",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
