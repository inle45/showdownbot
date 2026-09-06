"""Enregistrement complet du deroule d'un combat.

Un fichier JSON autonome par combat, dans memory/battles/. Autonome au sens
strict: il contient tout ce dont l'analyse a besoin (equipes, tours, traces de
decision, resultat, version de config utilisee), donc il peut etre analyse plus
tard, ou depuis une autre machine, sans rejouer quoi que ce soit.

C'est aussi ce qui permet de dissocier completement le jeu de l'analyse: le bot
peut tourner des heures sans cle API, les analyses se rattrapent ensuite.
"""

import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.config import BATTLES_DIR


@dataclass
class TurnRecord:
    """Un tour: l'etat vu par le bot, et le raisonnement qui a suivi."""

    turn: int
    decision: Dict[str, Any]
    timestamp: float = field(default_factory=time.time)


@dataclass
class BattleLog:
    """Le combat complet, serialisable tel quel."""

    battle_tag: str
    battle_format: str
    started_at: str
    config_version: int
    config_params: Dict[str, Any]
    opponent_username: str = ""
    server: str = "local"
    turns: List[TurnRecord] = field(default_factory=list)
    protocol: List[str] = field(default_factory=list)
    result: Optional[str] = None
    won: Optional[bool] = None
    finished_at: Optional[str] = None
    turn_count: int = 0
    my_team: List[str] = field(default_factory=list)
    opponent_team: List[str] = field(default_factory=list)
    my_survivors: List[str] = field(default_factory=list)
    opponent_survivors: List[str] = field(default_factory=list)
    rating: Optional[int] = None
    analysis: Optional[Dict[str, Any]] = None

    def record_turn(self, turn: int, decision: Dict[str, Any]) -> None:
        self.turns.append(TurnRecord(turn=turn, decision=decision))

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        return payload

    @property
    def slug(self) -> str:
        """Nom de fichier stable et triable pour ce combat."""
        stamp = self.started_at.replace(":", "").replace("-", "").replace("T", "-")[:15]
        # removeprefix, pas replace: "battle-gen9randombattle-123" contient
        # "battle-" deux fois et un replace global mange le nom du format.
        tag = self.battle_tag.removeprefix("battle-").replace("/", "-")
        return f"{stamp}-{tag}"

    def path(self, directory: str = BATTLES_DIR) -> str:
        return os.path.join(directory, f"{self.slug}.json")

    def save(self, directory: str = BATTLES_DIR) -> str:
        os.makedirs(directory, exist_ok=True)
        target = self.path(directory)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        return target


def new_log(battle_tag: str, battle_format: str, config, server: str = "local") -> BattleLog:
    return BattleLog(
        battle_tag=battle_tag,
        battle_format=battle_format,
        started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        config_version=config.config_version,
        config_params=dict(config.params),
        server=server,
    )


def finalize(log: BattleLog, battle) -> BattleLog:
    """Complete le log avec l'issue du combat."""
    log.finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log.turn_count = battle.turn
    log.won = battle.won
    log.result = "victoire" if battle.won else ("defaite" if battle.won is False else "inconnu")
    log.my_team = sorted(p.species for p in battle.team.values())
    log.opponent_team = sorted(p.species for p in battle.opponent_team.values())
    log.my_survivors = sorted(p.species for p in battle.team.values() if not p.fainted)
    log.opponent_survivors = sorted(
        p.species for p in battle.opponent_team.values() if not p.fainted
    )
    log.opponent_username = getattr(battle, "opponent_username", "") or ""
    log.rating = getattr(battle, "rating", None)
    return log


def load_log(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def list_logs(directory: str = BATTLES_DIR) -> List[str]:
    """Chemins des logs de combat, du plus ancien au plus recent."""
    if not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(".json")
    )


def load_all(directory: str = BATTLES_DIR) -> List[Dict[str, Any]]:
    logs = []
    for path in list_logs(directory):
        try:
            payload = load_log(path)
        except (OSError, json.JSONDecodeError):
            continue
        payload["_path"] = path
        logs.append(payload)
    return logs
