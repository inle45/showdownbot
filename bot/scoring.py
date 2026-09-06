"""Primitives de calcul du moteur heuristique: stats, matchups, degats.

Tout est en Python pur. Le calcul de degats s'appuie sur
``poke_env.calc.calculate_damage``, portage du damage-calc de Smogon, qui rend
une fourchette (roll minimum, roll maximum).

Piege important traite ici: poke-env ne remplit ``Pokemon.stats`` que pour NOTRE
equipe (via le message |request| du serveur). Les Pokemon adverses gardent
``{hp: None, atk: None, ...}``, et ``calculate_damage`` echoue alors sur son
assertion. On estime donc les stats adverses avec l'approximation standard des
randbats (31 IVs, 85 EVs, nature neutre) avant tout calcul.
"""

import math
from typing import Dict, List, Optional, Tuple

from poke_env.battle import Battle, Move, MoveCategory, Pokemon, PokemonType, Status, Target
from poke_env.calc import calculate_damage
from poke_env.data import GenData
from poke_env.stats import compute_raw_stats

# Approximation des sets de Gen 9 Random Battle: IVs max, 85 EVs partout,
# nature neutre. C'est la convention utilisee par la plupart des bots poke-env,
# et l'erreur resultante est petite devant l'incertitude sur le set adverse.
RANDBATS_EVS = [85] * 6
RANDBATS_IVS = [31] * 6
RANDBATS_NATURE = "serious"

STAT_ORDER = ["hp", "atk", "def", "spa", "spd", "spe"]

# Multiplicateurs de boost standards (-6 a +6).
BOOST_MULTIPLIERS = {
    -6: 2 / 8, -5: 2 / 7, -4: 2 / 6, -3: 2 / 5, -2: 2 / 4, -1: 2 / 3,
    0: 1.0,
    1: 3 / 2, 2: 4 / 2, 3: 5 / 2, 4: 6 / 2, 5: 7 / 2, 6: 8 / 2,
}

# Puissance supposee d'une attaque adverse inconnue, quand aucun move n'a encore
# ete revele. Volontairement au-dessus de la moyenne: mieux vaut surestimer une
# menace inconnue que se faire surprendre.
UNKNOWN_MOVE_BASE_POWER = 85


def type_chart_for(battle: Battle):
    """Table des types de la generation du combat.

    ``GenData.from_gen`` est mise en cache par poke-env: l'appel est gratuit.
    """
    return GenData.from_gen(battle.gen).type_chart


def ensure_stats(pokemon: Pokemon, gen: int = 9) -> Dict[str, int]:
    """Garantit que ``pokemon.stats`` est exploitable, en estimant si besoin.

    Renvoie le dictionnaire de stats (jamais de None).
    """
    stats = pokemon.stats or {}
    if stats and all(stats.get(s) for s in STAT_ORDER):
        return {s: int(stats[s]) for s in STAT_ORDER}

    raw = compute_raw_stats(
        pokemon.species,
        RANDBATS_EVS,
        RANDBATS_IVS,
        pokemon.level,
        RANDBATS_NATURE,
        GenData.from_gen(gen),
    )
    estimated = {stat: int(value) for stat, value in zip(STAT_ORDER, raw)}
    # On conserve ce que le serveur a reellement communique.
    for stat in STAT_ORDER:
        known = stats.get(stat)
        if known:
            estimated[stat] = int(known)
    pokemon.stats = dict(estimated)
    return estimated


def effective_stat(pokemon: Pokemon, stat: str, gen: int = 9) -> float:
    """Stat apres application des boosts en cours."""
    base = ensure_stats(pokemon, gen)[stat]
    return base * BOOST_MULTIPLIERS.get(pokemon.boosts.get(stat, 0), 1.0)


def real_max_hp(pokemon: Pokemon, gen: int = 9) -> int:
    """PV maximum reels.

    Pour l'adversaire, poke-env expose ``max_hp = 100`` (pourcentages): on rend
    la stat estimee, seule comparable aux degats bruts du calculateur.
    """
    return ensure_stats(pokemon, gen)["hp"]


def current_hp(pokemon: Pokemon, gen: int = 9) -> float:
    return max(1.0, real_max_hp(pokemon, gen) * (pokemon.current_hp_fraction or 0.0))


def type_multiplier(move_type: PokemonType, defender: Pokemon, type_chart) -> float:
    """Multiplicateur de type d'une attaque contre un defenseur."""
    types = [t for t in defender.types if t is not None]
    if not types:
        return 1.0
    return move_type.damage_multiplier(*types[:2], type_chart=type_chart)


def offensive_pressure(attacker: Pokemon, defender: Pokemon, type_chart) -> float:
    """log2 du meilleur multiplicateur STAB de l'attaquant contre le defenseur.

    Echelle lisible: +2 pour du x4, +1 pour du x2, 0 pour du neutre,
    -1 pour du x0.5, et une valeur plancher pour l'immunite.
    """
    best = 0.0
    for attack_type in attacker.types:
        if attack_type is None:
            continue
        multiplier = type_multiplier(attack_type, defender, type_chart)
        best = max(best, multiplier)
    if best <= 0:
        return -3.0
    return math.log2(best)


def matchup_score(mine: Pokemon, theirs: Pokemon, type_chart) -> float:
    """Score de matchup du point de vue de ``mine``, dans [-6, +6] environ.

    Positif = on frappe plus fort qu'on ne se fait frapper.
    """
    return offensive_pressure(mine, theirs, type_chart) - offensive_pressure(
        theirs, mine, type_chart
    )


def _fallback_damage(attacker: Pokemon, defender: Pokemon, type_chart, gen: int = 9) -> Tuple[int, int]:
    """Estimation de degats quand aucun move adverse n'est connu.

    Applique la formule standard avec une attaque STAB fictive de puissance
    ``UNKNOWN_MOVE_BASE_POWER``, en retenant la meilleure categorie offensive de
    l'attaquant. Sert uniquement a evaluer une menace non revelee.
    """
    atk_stats = ensure_stats(attacker, gen)
    physical = atk_stats["atk"] >= atk_stats["spa"]
    attack = effective_stat(attacker, "atk" if physical else "spa", gen)
    defense = effective_stat(defender, "def" if physical else "spd", gen)

    best_multiplier = 1.0
    for attack_type in attacker.types:
        if attack_type is None:
            continue
        best_multiplier = max(best_multiplier, type_multiplier(attack_type, defender, type_chart))

    level = attacker.level
    base = math.floor(
        math.floor(
            math.floor(2 * level / 5 + 2) * UNKNOWN_MOVE_BASE_POWER * attack / max(1.0, defense)
        )
        / 50
    ) + 2
    damage = base * 1.5 * best_multiplier  # 1.5 = STAB suppose
    return int(damage * 0.85), int(damage)


def damage_range(
    battle: Battle, attacker: Pokemon, defender: Pokemon, move: Move
) -> Optional[Tuple[int, int]]:
    """Fourchette de degats bruts, ou None si le calcul n'est pas applicable."""
    if move.category == MoveCategory.STATUS:
        return None
    if battle.player_role is None or battle.opponent_role is None:
        return None

    ensure_stats(attacker, battle.gen)
    ensure_stats(defender, battle.gen)

    attacker_is_mine = attacker in battle.team.values()
    attacker_id = attacker.identifier(
        battle.player_role if attacker_is_mine else battle.opponent_role
    )
    defender_id = defender.identifier(
        battle.opponent_role if attacker_is_mine else battle.player_role
    )
    try:
        result = calculate_damage(attacker_id, defender_id, move, battle)
    except (AssertionError, KeyError, TypeError, ValueError, ZeroDivisionError):
        # Le calculateur documente plusieurs cas non geres; on ne fait jamais
        # tomber un combat pour une estimation de degats.
        return None
    if not result:
        return None
    low, high = result
    hits = move.expected_hits or 1
    return int(low * hits), int(high * hits)


def damage_fraction(
    battle: Battle,
    attacker: Pokemon,
    defender: Pokemon,
    move: Move,
    percentile: float,
) -> float:
    """Fraction des PV RESTANTS du defenseur retiree par ``move``.

    ``percentile`` choisit le point de la fourchette: 0 = roll minimum
    (pessimiste), 1 = roll maximum (optimiste).
    """
    rolls = damage_range(battle, attacker, defender, move)
    if rolls is None:
        return 0.0
    low, high = rolls
    planned = low + (high - low) * percentile
    return planned / max(1.0, current_hp(defender, battle.gen))


def incoming_damage_fraction(
    battle: Battle, defender: Pokemon, attacker: Pokemon, percentile: float, type_chart
) -> float:
    """Pire fraction de PV que ``attacker`` peut retirer a ``defender`` en un tour.

    N'utilise que les moves REVELES. Si l'adversaire n'a rien montre, retombe sur
    une estimation par types plutot que de supposer l'absence de menace.
    """
    worst = 0.0
    known_damaging = [
        move for move in attacker.moves.values() if move.category != MoveCategory.STATUS
    ]
    for move in known_damaging:
        worst = max(worst, damage_fraction(battle, attacker, defender, move, percentile))

    if not known_damaging:
        low, high = _fallback_damage(attacker, defender, type_chart, battle.gen)
        planned = low + (high - low) * percentile
        worst = planned / max(1.0, current_hp(defender, battle.gen))

    return worst


def outspeeds(mine: Pokemon, theirs: Pokemon, gen: int = 9) -> bool:
    """Vrai si notre Pokemon agit avant, en tenant compte de la paralysie."""
    my_speed = effective_stat(mine, "spe", gen)
    their_speed = effective_stat(theirs, "spe", gen)
    if mine.status == Status.PAR:
        my_speed *= 0.5
    if theirs.status == Status.PAR:
        their_speed *= 0.5
    return my_speed > their_speed


def hazard_layers(side_conditions) -> float:
    """Nombre de couches de hazards ponderees, cote indique."""
    from poke_env.battle import SideCondition

    layers = 0.0
    for condition, count in side_conditions.items():
        if condition == SideCondition.STEALTH_ROCK:
            layers += 1.0
        elif condition == SideCondition.SPIKES:
            layers += count * 0.6
        elif condition == SideCondition.TOXIC_SPIKES:
            layers += count * 0.5
        elif condition == SideCondition.STICKY_WEB:
            layers += 0.5
    return layers


def is_setup_move(move: Move) -> bool:
    """Move de boost sur soi (Danse Lames, Calme Mental...)."""
    if move.category != MoveCategory.STATUS:
        return False
    boosts = move.boosts or {}
    return bool(boosts) and move.target == Target.SELF and any(v > 0 for v in boosts.values())


def is_heal_move(move: Move) -> bool:
    return move.category == MoveCategory.STATUS and (move.heal or 0) > 0


def status_targets_opponent(move: Move) -> bool:
    return move.category == MoveCategory.STATUS and move.status is not None


def revealed_move_names(pokemon: Pokemon) -> List[str]:
    return sorted(move.id for move in pokemon.moves.values())
