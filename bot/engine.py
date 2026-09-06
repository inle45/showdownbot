"""Moteur de decision heuristique.

AUCUN appel reseau, AUCUN appel API. Toute la decision de combat sort d'ici, a
partir de ``config/heuristics.json``. Les lecons tirees des combats passes
n'arrivent jamais en direct: elles ont deja ete traduites en valeurs de
parametres par analysis/tuner.py, avant le combat.

Le moteur produit aussi la trace du raisonnement (candidats, scores, detail des
composantes). C'est cette trace, et non le log Showdown brut, qui est envoyee a
l'analyse post-combat: sans elle le modele ne peut que commenter le resultat,
avec elle il peut pointer la composante de score qui a mal arbitre.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from poke_env.battle import Battle, Move, MoveCategory, Pokemon, Status

from bot.config import HeuristicConfig
from bot.scoring import (
    current_hp,
    damage_fraction,
    damage_range,
    hazard_layers,
    incoming_damage_fraction,
    is_heal_move,
    is_setup_move,
    matchup_score,
    outspeeds,
    real_max_hp,
    revealed_move_names,
    status_targets_opponent,
    type_chart_for,
    type_multiplier,
)

# Nombre de candidats conserves dans la trace. Assez pour expliquer l'arbitrage,
# assez peu pour que le prompt d'analyse reste court.
TRACE_CANDIDATES = 4

# Garde-fou non parametrable. Le tuner deplace les seuils du moteur; rien ne
# garantit qu'il n'atteindra pas un coin de l'espace de parametres ou le bot
# reste face a une immunite de type. Ces deux constantes definissent un plancher
# de jeu que AUCUN parametre ne peut desactiver: quand le pivot est massivement
# meilleur ET que l'actif prend des degats lourds, la marge switch.min_score_advantage
# est ignoree. C'est la contrepartie de securite de l'auto-ajustement.
SAFETY_MATCHUP_GAP = 3.0  # ~ un facteur 8 en efficacite de type
SAFETY_INCOMING_FRACTION = 0.5  # l'actif perd la moitie de ses PV par tour

# Statuts dont l'application est sans effet.
_BLOCKING_STATUS = {Status.SLP, Status.FRZ, Status.PSN, Status.TOX, Status.BRN, Status.PAR}


@dataclass
class Candidate:
    """Une action envisagee, avec le detail de son score."""

    kind: str  # "move" ou "switch"
    name: str
    score: float
    components: Dict[str, float] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    tera: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "name": self.name,
            "score": round(self.score, 3),
            "tera": self.tera,
            "components": {k: round(v, 3) for k, v in self.components.items()},
            "detail": self.detail,
        }


@dataclass
class Decision:
    """Le choix retenu, ses concurrents, et l'etat du tour."""

    chosen: Optional[Candidate]
    candidates: List[Candidate]
    context: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "chosen": self.chosen.to_dict() if self.chosen else None,
            "candidates": [c.to_dict() for c in self.candidates[:TRACE_CANDIDATES]],
            "context": self.context,
        }


class HeuristicEngine:
    """Choisit une action a partir des seuls parametres de configuration."""

    def __init__(self, config: HeuristicConfig):
        self.config = config

    # ------------------------------------------------------------------ moves

    def _score_damaging_move(
        self, battle: Battle, me: Pokemon, opponent: Pokemon, move: Move, faster: bool
    ) -> Candidate:
        cfg = self.config
        components: Dict[str, float] = {}
        detail: Dict[str, Any] = {}

        rolls = damage_range(battle, me, opponent, move)
        fraction = damage_fraction(
            battle, me, opponent, move, cfg["risk.damage_roll_percentile"]
        )
        detail["damage_roll"] = list(rolls) if rolls else None
        detail["damage_fraction"] = round(fraction, 3)
        detail["type_multiplier"] = round(
            type_multiplier(move.type, opponent, type_chart_for(battle)), 2
        )
        detail["base_power"] = move.base_power
        detail["accuracy"] = move.accuracy

        components["damage"] = cfg["attack.expected_damage_weight"] * min(fraction, 1.0)

        if fraction >= 1.0:
            components["ko"] = cfg["attack.ko_bonus"]
            if faster:
                components["speed_control"] = cfg["attack.speed_control_bonus"]
        elif fraction >= 0.5:
            components["two_hko"] = cfg["attack.two_hko_bonus"]

        if move.priority > 0 and fraction >= 1.0 and not faster:
            # On est plus lent mais la priorite met KO: c'est exactement le cas
            # que attack.priority_bonus doit rendre visible.
            components["priority"] = cfg["attack.priority_bonus"]

        if move.recoil:
            components["recoil"] = -cfg["attack.recoil_penalty"] * float(move.recoil)

        subtotal = sum(components.values())
        # La precision multiplie le score, elle ne s'y ajoute pas: un move a 70%
        # doit perdre une fraction de sa valeur, pas une constante.
        accuracy = move.accuracy if move.accuracy is not None else 1.0
        accuracy_factor = accuracy ** cfg["attack.accuracy_weight"]
        components["accuracy_factor"] = accuracy_factor
        score = subtotal * accuracy_factor

        return Candidate("move", move.id, score, components, detail)

    def _score_status_move(
        self, battle: Battle, me: Pokemon, opponent: Pokemon, move: Move, incoming: float
    ) -> Candidate:
        cfg = self.config
        components: Dict[str, float] = {}
        detail: Dict[str, Any] = {"category": "status"}

        if is_heal_move(move):
            detail["kind"] = "heal"
            hp_fraction = me.current_hp_fraction or 0.0
            detail["own_hp_fraction"] = round(hp_fraction, 3)
            if hp_fraction <= cfg["heal.hp_threshold"]:
                # Se soigner sous une attaque qui remet plus bas que le soin est
                # une perte seche: on module par la marge restante.
                margin = max(0.0, 1.0 - hp_fraction - incoming)
                components["heal"] = cfg["heal.value"] * (1.0 + margin)
            else:
                components["heal_not_needed"] = 0.0

        elif is_setup_move(move):
            detail["kind"] = "setup"
            hp_fraction = me.current_hp_fraction or 0.0
            detail["own_hp_fraction"] = round(hp_fraction, 3)
            detail["incoming_fraction"] = round(incoming, 3)
            safe_hp = hp_fraction >= cfg["setup.min_hp_fraction"]
            safe_pressure = incoming <= cfg["setup.max_incoming_damage"]
            detail["conditions_met"] = bool(safe_hp and safe_pressure)
            if safe_hp and safe_pressure:
                stages = sum(v for v in (move.boosts or {}).values() if v > 0)
                components["setup"] = cfg["setup.value"] * (1.0 + 0.25 * (stages - 1))
            else:
                components["setup_unsafe"] = 0.0

        elif status_targets_opponent(move):
            detail["kind"] = "status"
            target_hp = opponent.current_hp_fraction or 0.0
            detail["target_hp_fraction"] = round(target_hp, 3)
            already = opponent.status is not None
            detail["target_already_statused"] = already
            immune = self._status_immune(opponent, move, battle)
            detail["target_immune"] = immune
            if already or immune or target_hp < cfg["status.min_target_hp_fraction"]:
                components["status_wasted"] = 0.0
            else:
                components["status"] = cfg["status.base_value"]

        else:
            detail["kind"] = "other_status"
            components["other"] = 0.0

        accuracy = move.accuracy if move.accuracy is not None else 1.0
        accuracy_factor = accuracy ** cfg["attack.accuracy_weight"]
        components["accuracy_factor"] = accuracy_factor
        score = sum(v for k, v in components.items() if k != "accuracy_factor") * accuracy_factor

        return Candidate("move", move.id, score, components, detail)

    @staticmethod
    def _status_immune(target: Pokemon, move: Move, battle: Battle) -> bool:
        """Immunites de statut les plus courantes (types, pas capacites)."""
        from poke_env.battle import PokemonType

        status = move.status
        if status is None:
            return False
        types = {t for t in target.types if t is not None}
        if status == Status.BRN and PokemonType.FIRE in types:
            return True
        if status in (Status.PSN, Status.TOX) and (
            PokemonType.POISON in types or PokemonType.STEEL in types
        ):
            return True
        if status == Status.PAR and PokemonType.ELECTRIC in types:
            return True
        if status == Status.FRZ and PokemonType.ICE in types:
            return True
        return False

    # --------------------------------------------------------------- switches

    def _score_switch(
        self, battle: Battle, candidate: Pokemon, opponent: Pokemon, urgency: float
    ) -> Candidate:
        cfg = self.config
        components: Dict[str, float] = {}
        detail: Dict[str, Any] = {}

        type_chart = type_chart_for(battle)
        matchup = matchup_score(candidate, opponent, type_chart)
        detail["matchup"] = round(matchup, 2)
        components["matchup"] = matchup

        # Urgence heritee de la situation actuelle: c'est ce terme qui fait de
        # switch.matchup_threshold un vrai declencheur, et non un seuil binaire.
        components["urgency"] = urgency
        components["momentum_cost"] = -cfg["switch.momentum_cost"]

        layers = hazard_layers(battle.side_conditions)
        detail["hazard_layers"] = round(layers, 2)
        if layers:
            components["hazards"] = -cfg["switch.hazard_penalty_per_layer"] * layers

        hp_fraction = candidate.current_hp_fraction or 0.0
        detail["hp_fraction"] = round(hp_fraction, 3)
        if hp_fraction < 0.5:
            components["low_hp"] = -cfg["switch.low_hp_reluctance"] * (1.0 - 2 * hp_fraction)

        # Degats encaisses a l'entree par le pivot envisage.
        entry_damage = incoming_damage_fraction(
            battle, candidate, opponent, cfg["risk.opponent_damage_roll_percentile"], type_chart
        )
        detail["entry_damage_fraction"] = round(entry_damage, 3)
        components["entry_damage"] = -2.0 * entry_damage

        return Candidate("switch", candidate.species, sum(components.values()), components, detail)

    # ------------------------------------------------------------------ tera

    def _tera_gain(self, battle: Battle, me: Pokemon, opponent: Pokemon, best: Candidate) -> float:
        """Gain de score approche d'une terastallisation immediate.

        Heuristique volontairement conservatrice: la tera est comptee comme un
        gain seulement quand elle donne le STAB a l'attaque deja retenue et que
        celle-ci ne mettait pas encore KO.
        """
        if not self.config["tera.enabled"] or not battle.can_tera:
            return 0.0
        if best.kind != "move" or me.tera_type is None:
            return 0.0
        move = next((m for m in battle.available_moves if m.id == best.name), None)
        if move is None or move.category == MoveCategory.STATUS:
            return 0.0
        if move.type != me.tera_type or me.tera_type in me.types:
            return 0.0

        fraction = best.detail.get("damage_fraction", 0.0)
        if fraction >= 1.0:
            return 0.0
        boosted = fraction * 1.5
        if boosted >= 1.0:
            # Le STAB tera transforme un non-KO en KO: c'est le cas qui justifie
            # de depenser la tera.
            return self.config["attack.ko_bonus"] * 0.75
        return self.config["attack.expected_damage_weight"] * (boosted - fraction)

    # --------------------------------------------------------------- decision

    def decide(self, battle: Battle) -> Decision:
        """Evalue toutes les actions legales et renvoie la meilleure."""
        cfg = self.config
        me = battle.active_pokemon
        opponent = battle.opponent_active_pokemon
        type_chart = type_chart_for(battle)

        if me is None or opponent is None:
            return Decision(None, [], {"turn": battle.turn, "reason": "etat incomplet"})

        faster = outspeeds(me, opponent, battle.gen)
        incoming = incoming_damage_fraction(
            battle, me, opponent, cfg["risk.opponent_damage_roll_percentile"], type_chart
        )
        current_matchup = matchup_score(me, opponent, type_chart)

        # Urgence de switch: somme des deux declencheurs, chacun continu pour que
        # le tuner puisse les deplacer par petits pas sans effet de falaise.
        urgency = max(0.0, cfg["switch.matchup_threshold"] - current_matchup)
        urgency += 2.0 * max(0.0, incoming - cfg["switch.incoming_damage_threshold"])

        context = {
            "turn": battle.turn,
            "active": me.species,
            "active_hp_fraction": round(me.current_hp_fraction or 0.0, 3),
            "active_status": me.status.name if me.status else None,
            "active_boosts": {k: v for k, v in me.boosts.items() if v},
            "opponent": opponent.species,
            "opponent_hp_fraction": round(opponent.current_hp_fraction or 0.0, 3),
            "opponent_status": opponent.status.name if opponent.status else None,
            "opponent_boosts": {k: v for k, v in opponent.boosts.items() if v},
            "opponent_revealed_moves": revealed_move_names(opponent),
            "matchup": round(current_matchup, 2),
            "faster": faster,
            "predicted_incoming_fraction": round(incoming, 3),
            "switch_urgency": round(urgency, 3),
            "hazards_own_side": round(hazard_layers(battle.side_conditions), 2),
            "remaining_team": [
                p.species for p in battle.team.values() if not p.fainted and p is not me
            ],
            "opponent_remaining": sum(
                1 for p in battle.opponent_team.values() if not p.fainted
            ),
            "can_tera": bool(battle.can_tera),
        }

        candidates: List[Candidate] = []

        for move in battle.available_moves:
            if move.category == MoveCategory.STATUS:
                candidates.append(self._score_status_move(battle, me, opponent, move, incoming))
            else:
                candidates.append(
                    self._score_damaging_move(battle, me, opponent, move, faster)
                )

        for switch in battle.available_switches:
            candidates.append(self._score_switch(battle, switch, opponent, urgency))

        if not candidates:
            return Decision(None, [], {**context, "reason": "aucune action legale"})

        candidates.sort(key=lambda c: c.score, reverse=True)

        best_move = next((c for c in candidates if c.kind == "move"), None)
        best_switch = next((c for c in candidates if c.kind == "switch"), None)

        # Un switch ne l'emporte que s'il bat la meilleure attaque d'une marge
        # explicite. Sans cette marge, le bot bascule au moindre bruit de score.
        safety_override = False
        if best_switch is not None and best_move is not None:
            matchup_gap = best_switch.detail.get("matchup", 0.0) - current_matchup
            safety_override = (
                matchup_gap >= SAFETY_MATCHUP_GAP and incoming >= SAFETY_INCOMING_FRACTION
            )
            margin = 0.0 if safety_override else cfg["switch.min_score_advantage"]
            chosen = best_switch if best_switch.score >= best_move.score + margin else best_move
        else:
            chosen = best_switch or best_move

        context["safety_override"] = safety_override

        if chosen is not None and chosen.kind == "move":
            gain = self._tera_gain(battle, me, opponent, chosen)
            if gain >= cfg["tera.min_score_gain"]:
                chosen.tera = True
                chosen.components["tera_gain"] = gain
                chosen.score += gain

        context["decision_margin"] = round(
            chosen.score - (candidates[1].score if len(candidates) > 1 else chosen.score), 3
        )

        return Decision(chosen, candidates, context)
