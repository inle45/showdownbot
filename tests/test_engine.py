"""Tests du moteur heuristique, hors ligne et deterministes.

Deux familles:
  1. le moteur prend la bonne decision dans des situations evidentes;
  2. chaque parametre produit bien l'effet decrit dans heuristics.schema.json.

La famille (2) est ce qui rend le tuner automatique defendable: si un parametre
ne bouge pas le comportement dans le sens annonce, la table tag -> parametre de
config/tuning_rules.json est fausse, et ces tests le disent.

    python -m unittest discover -s tests -v
"""

import copy
import unittest

import bot  # applique le shim orjson avant poke_env  # noqa: F401
from bot.config import ConfigError, clamp, load_config, validate
from bot.engine import HeuristicEngine
from tests.harness import build_battle


def config_with(**overrides):
    config = load_config()
    config.params = dict(config.params)
    config.params.update(overrides)
    return config


def decide_with(battle, **overrides):
    return HeuristicEngine(config_with(**overrides)).decide(battle)


def favourable_battle(**kwargs):
    """Garchomp face a Heatran: Seisme est un KO garanti."""
    return build_battle(
        my_team=[
            {"species": "Garchomp", "moves": ["earthquake", "dragonclaw", "swordsdance", "firefang"]},
            {"species": "Corviknight", "moves": ["bravebird", "roost", "uturn", "bodypress"]},
        ],
        opponent_species="Heatran",
        opponent_moves=["lavaplume"],
        **kwargs,
    )


def unfavourable_battle(**kwargs):
    """Corviknight face a un Raichu electrique: matchup defavorable, pivot dispo."""
    return build_battle(
        my_team=[
            {"species": "Corviknight", "moves": ["bravebird", "roost", "uturn", "bodypress"]},
            {"species": "Garchomp", "moves": ["earthquake", "dragonclaw", "swordsdance", "firefang"]},
        ],
        opponent_species="Raichu",
        opponent_moves=["thunderbolt"],
        **kwargs,
    )


class TestDecisions(unittest.TestCase):
    def test_prend_le_ko_garanti(self):
        decision = decide_with(favourable_battle())
        self.assertEqual(decision.chosen.kind, "move")
        self.assertEqual(decision.chosen.name, "earthquake")
        self.assertIn("ko", decision.chosen.components)

    def test_matchup_defavorable_declenche_une_urgence(self):
        decision = decide_with(unfavourable_battle())
        self.assertLess(decision.context["matchup"], 0)
        self.assertGreater(decision.context["switch_urgency"], 0)

    def test_switch_choisi_quand_le_pivot_est_bien_meilleur(self):
        # Garchomp est immunise a l'Electrik: le switch doit l'emporter.
        decision = decide_with(unfavourable_battle(), **{"switch.momentum_cost": 0.0})
        switch = next(c for c in decision.candidates if c.kind == "switch")
        self.assertEqual(switch.name, "garchomp")
        self.assertEqual(decision.chosen.kind, "switch")

    def test_ne_switche_pas_dans_un_bon_matchup(self):
        decision = decide_with(favourable_battle())
        self.assertEqual(decision.chosen.kind, "move")

    def test_trace_contient_les_composantes_du_score(self):
        decision = decide_with(favourable_battle())
        payload = decision.to_dict()
        self.assertIsNotNone(payload["chosen"])
        self.assertTrue(payload["candidates"])
        self.assertIn("components", payload["candidates"][0])
        self.assertIn("matchup", payload["context"])
        self.assertIn("predicted_incoming_fraction", payload["context"])


class TestParameterEffects(unittest.TestCase):
    """Verifie le champ 'effect' de chaque parametre du registre."""

    def _switch_score(self, battle, **overrides):
        decision = decide_with(battle, **overrides)
        return next(c.score for c in decision.candidates if c.kind == "switch")

    def test_momentum_cost_reduit_lattrait_du_switch(self):
        # "Augmenter => le bot switche moins souvent."
        low = self._switch_score(unfavourable_battle(), **{"switch.momentum_cost": 0.0})
        high = self._switch_score(unfavourable_battle(), **{"switch.momentum_cost": 2.0})
        self.assertGreater(low, high)

    def test_matchup_threshold_augmente_lurgence(self):
        # "Augmenter => le bot switche plus tot face a un mauvais matchup."
        strict = decide_with(unfavourable_battle(), **{"switch.matchup_threshold": -3.0})
        lax = decide_with(unfavourable_battle(), **{"switch.matchup_threshold": 0.5})
        self.assertGreater(lax.context["switch_urgency"], strict.context["switch_urgency"])

    def test_incoming_damage_threshold_reduit_lurgence(self):
        # "Augmenter => le bot tolere plus de degats avant de switcher."
        tolerant = decide_with(unfavourable_battle(), **{"switch.incoming_damage_threshold": 1.0})
        nervous = decide_with(unfavourable_battle(), **{"switch.incoming_damage_threshold": 0.15})
        self.assertGreaterEqual(nervous.context["switch_urgency"], tolerant.context["switch_urgency"])

    def test_min_score_advantage_freine_la_bascule(self):
        """"Augmenter => le bot switche moins souvent."

        Le scenario est volontairement marginal (pivot legerement meilleur, pas
        d'immunite): c'est la seule facon de tester le seuil lui-meme. Avec un
        pivot ecrasant, aucune marge admissible ne doit empecher le switch, et
        c'est le comportement voulu.
        """
        def marginal():
            return build_battle(
                my_team=[
                    {"species": "Corviknight", "moves": ["bravebird", "roost", "uturn", "bodypress"]},
                    {"species": "Blissey", "moves": ["seismictoss", "softboiled"]},
                ],
                opponent_species="Raichu",
                opponent_moves=["thunderbolt"],
            )

        base = decide_with(marginal(), **{"switch.momentum_cost": 0.0, "switch.min_score_advantage": 0.0})
        best_move = next(c for c in base.candidates if c.kind == "move")
        best_switch = next(c for c in base.candidates if c.kind == "switch")
        gap = best_switch.score - best_move.score
        self.assertGreater(gap, 0.0, "scenario mal choisi: le switch doit etre legerement meilleur")

        below = clamp("switch.min_score_advantage", gap * 0.5, base and load_config().schema)
        above = clamp("switch.min_score_advantage", gap * 1.5, load_config().schema)

        eager = decide_with(marginal(), **{"switch.momentum_cost": 0.0, "switch.min_score_advantage": below})
        reluctant = decide_with(marginal(), **{"switch.momentum_cost": 0.0, "switch.min_score_advantage": above})
        self.assertEqual(eager.chosen.kind, "switch")
        self.assertEqual(reluctant.chosen.kind, "move")

    def test_garde_fou_ignore_la_marge_dans_un_coin_de_parametres(self):
        """Aux valeurs extremes des deux freins au switch, le garde-fou prime.

        Sans lui, le tuner pourrait atteindre une configuration ou le bot reste
        face a une immunite de type en prenant 64% de ses PV par tour.
        """
        decision = decide_with(
            unfavourable_battle(),
            **{"switch.momentum_cost": 3.0, "switch.min_score_advantage": 2.0},
        )
        self.assertTrue(decision.context["safety_override"])
        self.assertEqual(decision.chosen.kind, "switch")
        self.assertEqual(decision.chosen.name, "garchomp")

    def test_garde_fou_inactif_en_situation_normale(self):
        """Le garde-fou ne doit pas se declencher hors danger reel."""
        decision = decide_with(favourable_battle())
        self.assertFalse(decision.context["safety_override"])
        self.assertEqual(decision.chosen.kind, "move")

    def test_hazard_penalty_penalise_le_switch(self):
        from poke_env.battle import SideCondition

        battle = unfavourable_battle()
        battle.side_conditions[SideCondition.STEALTH_ROCK] = 1
        without = self._switch_score(battle, **{"switch.hazard_penalty_per_layer": 0.0})
        with_penalty = self._switch_score(battle, **{"switch.hazard_penalty_per_layer": 1.5})
        self.assertGreater(without, with_penalty)

    def test_damage_roll_percentile_change_les_degats_planifies(self):
        # "Augmenter => le bot est plus optimiste sur ses propres degats."
        battle = build_battle(
            my_team=[{"species": "Corviknight", "moves": ["bravebird", "roost"]},
                     {"species": "Garchomp", "moves": ["earthquake"]}],
            opponent_species="Heatran",
        )
        pessimistic = decide_with(battle, **{"risk.damage_roll_percentile": 0.0})
        optimistic = decide_with(battle, **{"risk.damage_roll_percentile": 1.0})
        low = next(c for c in pessimistic.candidates if c.name == "bravebird")
        high = next(c for c in optimistic.candidates if c.name == "bravebird")
        self.assertGreater(high.detail["damage_fraction"], low.detail["damage_fraction"])

    def test_accuracy_weight_penalise_les_moves_imprecis(self):
        # "Augmenter => le bot devient plus prudent sur les moves imprecis."
        battle = build_battle(
            my_team=[{"species": "Chandelure", "moves": ["focusblast", "shadowball"]},
                     {"species": "Garchomp", "moves": ["earthquake"]}],
            opponent_species="Blissey",
        )
        lenient = decide_with(battle, **{"attack.accuracy_weight": 0.0})
        strict = decide_with(battle, **{"attack.accuracy_weight": 3.0})
        lenient_focus = next(c for c in lenient.candidates if c.name == "focusblast")
        strict_focus = next(c for c in strict.candidates if c.name == "focusblast")
        self.assertGreater(lenient_focus.score, strict_focus.score)

    def test_ko_bonus_pousse_vers_le_ko(self):
        battle = favourable_battle()
        small = decide_with(battle, **{"attack.ko_bonus": 0.0})
        large = decide_with(battle, **{"attack.ko_bonus": 6.0})
        small_eq = next(c for c in small.candidates if c.name == "earthquake")
        large_eq = next(c for c in large.candidates if c.name == "earthquake")
        self.assertGreater(large_eq.score, small_eq.score)

    def test_setup_bloque_sous_pression(self):
        # "setup.max_incoming_damage: augmenter => le bot se boost meme sous pression."
        battle = favourable_battle()
        unsafe = decide_with(battle, **{"setup.max_incoming_damage": 0.0, "setup.min_hp_fraction": 0.0})
        safe = decide_with(battle, **{"setup.max_incoming_damage": 1.0, "setup.min_hp_fraction": 0.0})
        unsafe_sd = next(c for c in unsafe.candidates if c.name == "swordsdance")
        safe_sd = next(c for c in safe.candidates if c.name == "swordsdance")
        self.assertEqual(unsafe_sd.score, 0.0)
        self.assertGreater(safe_sd.score, 0.0)

    def test_setup_bloque_a_bas_pv(self):
        battle = build_battle(
            my_team=[{"species": "Garchomp", "moves": ["swordsdance", "earthquake"], "hp_fraction": 0.2},
                     {"species": "Corviknight", "moves": ["roost"]}],
            opponent_species="Blissey",
        )
        decision = decide_with(battle, **{"setup.min_hp_fraction": 0.7})
        sd = next(c for c in decision.candidates if c.name == "swordsdance")
        self.assertFalse(sd.detail["conditions_met"])
        self.assertEqual(sd.score, 0.0)

    def test_soin_sans_interet_a_pleine_vie(self):
        battle = build_battle(
            my_team=[{"species": "Corviknight", "moves": ["roost", "bravebird"], "hp_fraction": 1.0},
                     {"species": "Garchomp", "moves": ["earthquake"]}],
            opponent_species="Blissey",
        )
        decision = decide_with(battle)
        roost = next(c for c in decision.candidates if c.name == "roost")
        self.assertEqual(roost.score, 0.0)

    def test_soin_valorise_a_bas_pv(self):
        battle = build_battle(
            my_team=[{"species": "Corviknight", "moves": ["roost", "bravebird"], "hp_fraction": 0.3},
                     {"species": "Garchomp", "moves": ["earthquake"]}],
            opponent_species="Blissey",
        )
        decision = decide_with(battle, **{"heal.hp_threshold": 0.45})
        roost = next(c for c in decision.candidates if c.name == "roost")
        self.assertGreater(roost.score, 0.0)

    def test_statut_gaspille_sur_cible_immunisee(self):
        battle = build_battle(
            my_team=[{"species": "Raichu", "moves": ["thunderwave", "thunderbolt"]},
                     {"species": "Garchomp", "moves": ["earthquake"]}],
            opponent_species="Zapdos",  # Electrik: immunise a la paralysie electrique
        )
        decision = decide_with(battle)
        twave = next(c for c in decision.candidates if c.name == "thunderwave")
        self.assertTrue(twave.detail["target_immune"])
        self.assertEqual(twave.score, 0.0)

    def test_statut_utile_sur_cible_saine(self):
        battle = build_battle(
            my_team=[{"species": "Raichu", "moves": ["thunderwave", "thunderbolt"]},
                     {"species": "Garchomp", "moves": ["earthquake"]}],
            opponent_species="Dragonite",
        )
        decision = decide_with(battle, **{"status.base_value": 1.5})
        twave = next(c for c in decision.candidates if c.name == "thunderwave")
        self.assertGreater(twave.score, 0.0)


class TestConfigGuards(unittest.TestCase):
    def test_valeur_hors_bornes_rejetee(self):
        config = load_config()
        params = copy.deepcopy(config.params)
        params["switch.momentum_cost"] = 99.0
        problems = validate(params, config.schema)
        self.assertTrue(any("hors bornes" in p for p in problems))

    def test_cle_inconnue_rejetee(self):
        config = load_config()
        params = copy.deepcopy(config.params)
        params["switch.inexistant"] = 1.0
        problems = validate(params, config.schema)
        self.assertTrue(any("inconnu" in p for p in problems))

    def test_clamp_respecte_bornes_et_pas(self):
        config = load_config()
        self.assertEqual(clamp("switch.momentum_cost", 99.0, config.schema), 3.0)
        self.assertEqual(clamp("switch.momentum_cost", -5.0, config.schema), 0.0)
        self.assertEqual(clamp("switch.momentum_cost", 0.83, config.schema), 0.8)

    def test_config_livree_est_valide(self):
        config = load_config()
        self.assertEqual(validate(config.params, config.schema), [])

    def test_chargement_config_invalide_leve(self):
        import json
        import os
        import tempfile

        config = load_config()
        broken = {"params": {**config.params, "attack.ko_bonus": 999}}
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as handle:
            json.dump(broken, handle)
            path = handle.name
        try:
            with self.assertRaises(ConfigError):
                load_config(path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
