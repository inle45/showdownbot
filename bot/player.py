"""Le joueur poke-env: relie le moteur heuristique au protocole Showdown.

Aucun appel API n'est fait ici, ni ailleurs pendant un combat. La seule chose
qui se produit en fin de combat est l'ecriture du log JSON et, si l'analyse est
activee, la mise en file du combat pour un unique appel post-mortem.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from poke_env.battle import AbstractBattle, Battle
from poke_env.player import Player
from poke_env.player.battle_order import BattleOrder, DefaultBattleOrder

from bot.battle_log import BattleLog, finalize, new_log
from bot.config import HeuristicConfig
from bot.engine import HeuristicEngine

LOGGER = logging.getLogger("showdownbot.player")


class HeuristicPlayer(Player):
    """Joueur pilote par ``HeuristicEngine``.

    ``on_battle_end`` est appele une fois par combat termine, avec le log
    complet: c'est le point d'accroche de l'analyse post-combat.
    """

    def __init__(
        self,
        config: HeuristicConfig,
        *args,
        on_battle_end: Optional[Callable[[BattleLog], None]] = None,
        server_label: str = "local",
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.config = config
        self.engine = HeuristicEngine(config)
        self.on_battle_end = on_battle_end
        self.server_label = server_label
        self.logs: Dict[str, BattleLog] = {}
        self.finished_logs: List[BattleLog] = []

    # --------------------------------------------------------------- decision

    def _log_for(self, battle: AbstractBattle) -> BattleLog:
        log = self.logs.get(battle.battle_tag)
        if log is None:
            log = new_log(
                battle.battle_tag, self.format or "", self.config, server=self.server_label
            )
            self.logs[battle.battle_tag] = log
        return log

    def choose_move(self, battle: AbstractBattle) -> BattleOrder:
        if not isinstance(battle, Battle):
            return self.choose_random_move(battle)

        log = self._log_for(battle)
        try:
            decision = self.engine.decide(battle)
        except Exception:  # une erreur de scoring ne doit jamais perdre le combat
            LOGGER.exception("Echec du moteur au tour %s, repli sur un coup aleatoire", battle.turn)
            log.record_turn(battle.turn, {"error": "engine_failure", "fallback": "random"})
            return self.choose_random_move(battle)

        log.record_turn(battle.turn, decision.to_dict())

        chosen = decision.chosen
        if chosen is None:
            return self.choose_random_move(battle)

        if chosen.kind == "switch":
            target = next(
                (p for p in battle.available_switches if p.species == chosen.name), None
            )
            if target is not None:
                return self.create_order(target)
            return self.choose_random_move(battle)

        move = next((m for m in battle.available_moves if m.id == chosen.name), None)
        if move is None:
            return self.choose_random_move(battle)
        return self.create_order(move, terastallize=bool(chosen.tera) and battle.can_tera)

    def teampreview(self, battle: AbstractBattle) -> str:
        """Ordre d'equipe: les randbats n'ont pas de teampreview, on reste neutre."""
        return "/team " + "".join(str(i + 1) for i in range(len(battle.team)))

    # ------------------------------------------------------------- fin de combat

    def _battle_finished_callback(self, battle: AbstractBattle) -> None:
        log = self._log_for(battle)
        finalize(log, battle)
        try:
            log.protocol = ["|".join(parts) for parts in getattr(battle, "_replay_data", [])]
        except Exception:  # pragma: no cover - le log brut est un bonus
            log.protocol = []
        path = log.save()
        LOGGER.info(
            "Combat %s termine (%s) en %s tours -> %s",
            battle.battle_tag, log.result, log.turn_count, path,
        )
        self.finished_logs.append(log)
        self.logs.pop(battle.battle_tag, None)

        if self.on_battle_end is not None:
            try:
                self.on_battle_end(log)
            except Exception:  # l'analyse ne doit jamais interrompre le jeu
                LOGGER.exception("Le traitement de fin de combat a echoue")


class RandomBaselinePlayer(Player):
    """Adversaire aleatoire, pour le bot-vs-bot local et les mesures de reference."""

    def choose_move(self, battle: AbstractBattle) -> BattleOrder:
        return self.choose_random_move(battle)

    def teampreview(self, battle: AbstractBattle) -> str:
        return "/team " + "".join(str(i + 1) for i in range(len(battle.team)))
