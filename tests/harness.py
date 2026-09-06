"""Harness de combats synthetiques: teste le moteur sans serveur Showdown.

On construit un objet ``Battle`` reel de poke-env et on lui injecte un message
``|request|`` (notre equipe, nos moves) et des messages ``|switch|`` (le Pokemon
adverse), exactement comme le ferait le serveur. Le moteur voit donc le meme
etat qu'en combat reel, mais de maniere deterministe et hors ligne.

Utile pour: valider une heuristique, verifier qu'un changement de parametre
produit bien l'effet annonce dans heuristics.schema.json, et faire tourner les
tests sur Termux sans installer Node.
"""

import logging
from typing import Any, Dict, List, Optional

from poke_env.battle import Battle
from poke_env.data import GenData, to_id_str
from poke_env.stats import compute_raw_stats

from bot.scoring import RANDBATS_EVS, RANDBATS_IVS, RANDBATS_NATURE, STAT_ORDER

_LOGGER = logging.getLogger("harness")
_LOGGER.addHandler(logging.NullHandler())


def _stats_for(species: str, level: int, gen: int) -> Dict[str, int]:
    # Le pokedex est indexe par id-string (minuscules, sans espace ni tiret).
    raw = compute_raw_stats(
        to_id_str(species), RANDBATS_EVS, RANDBATS_IVS, level, RANDBATS_NATURE,
        GenData.from_gen(gen),
    )
    return {stat: int(value) for stat, value in zip(STAT_ORDER, raw)}


def _request_pokemon(
    species: str, moves: List[str], level: int, gen: int, active: bool, hp_fraction: float
) -> Dict[str, Any]:
    stats = _stats_for(species, level, gen)
    max_hp = stats["hp"]
    current = max(1, int(max_hp * hp_fraction))
    return {
        "ident": f"p1: {species}",
        "details": f"{species}, L{level}",
        "condition": f"{current}/{max_hp}",
        "active": active,
        "stats": {k: v for k, v in stats.items() if k != "hp"},
        "moves": moves,
        "baseAbility": "noability",
        "item": "",
        "pokeball": "pokeball",
        "ability": "noability",
    }


def build_battle(
    my_team: List[Dict[str, Any]],
    opponent_species: str,
    opponent_level: int = 82,
    opponent_hp_fraction: float = 1.0,
    opponent_moves: Optional[List[str]] = None,
    gen: int = 9,
    can_tera: bool = False,
    tera_type: Optional[str] = None,
    turn: int = 1,
) -> Battle:
    """Construit un combat jouable a partir d'une description compacte.

    ``my_team`` est une liste de dicts ``{"species", "moves", "level"?,
    "hp_fraction"?}``; le premier element est l'actif.
    """
    battle = Battle("battle-gen9randombattle-test", "bot", _LOGGER, gen=gen)
    battle.player_role = "p1"

    side_pokemon = []
    for index, entry in enumerate(my_team):
        side_pokemon.append(
            _request_pokemon(
                entry["species"],
                entry.get("moves", []),
                entry.get("level", 82),
                gen,
                active=(index == 0),
                hp_fraction=entry.get("hp_fraction", 1.0),
            )
        )

    active_entry: Dict[str, Any] = {
        "moves": [
            {"move": move, "id": move, "pp": 16, "maxpp": 16, "target": "normal", "disabled": False}
            for move in my_team[0].get("moves", [])
        ]
    }
    if can_tera:
        active_entry["canTerastallize"] = tera_type or "Normal"

    battle.parse_request(
        {
            "active": [active_entry],
            "side": {"name": "bot", "id": "p1", "pokemon": side_pokemon},
            "rqid": 1,
            "wait": False,
        }
    )

    # Le Pokemon adverse arrive comme un switch normal du serveur.
    opponent_stats = _stats_for(opponent_species, opponent_level, gen)
    hp_percent = int(round(opponent_hp_fraction * 100))
    battle.parse_message(
        [
            "",
            "switch",
            f"p2a: {opponent_species}",
            f"{opponent_species}, L{opponent_level}",
            f"{hp_percent}/100",
        ]
    )

    # Moves adverses "reveles": on les declare via un message |move|, ce qui est
    # la seule facon dont le bot en prend connaissance en vrai.
    for move in opponent_moves or []:
        battle.parse_message(["", "move", f"p2a: {opponent_species}", move, f"p1a: {my_team[0]['species']}"])

    if turn:
        battle.parse_message(["", "turn", str(turn)])

    _ = opponent_stats  # les stats adverses sont estimees par bot.scoring
    return battle


def decide(battle: Battle, config=None):
    """Raccourci: lance le moteur sur un combat construit."""
    from bot.config import load_config
    from bot.engine import HeuristicEngine

    return HeuristicEngine(config or load_config()).decide(battle)
