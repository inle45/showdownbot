"""Tests du tuner lecons -> parametres.

C'est la partie la plus delicate du systeme: du code deterministe deplace les
reglages du bot a partir d'observations produites par un modele. Les tests
couvrent surtout les modes d'echec de ce genre de boucle:

  - oscillation (un tag et son antagoniste se relancent de cycle en cycle);
  - sur-reaction a une observation isolee;
  - derive hors des bornes du registre;
  - impossibilite de remonter la cause d'un mouvement de parametre.

    python -m unittest tests.test_tuner -v
"""

import json
import os
import shutil
import tempfile
import unittest

import bot  # noqa: F401
from analysis.consolidate import aggregate_findings
from analysis.tuner import check_rollback, net_scores, propose
from bot.config import load_config, load_taxonomy, load_tuning_rules, validate


def make_log(index, won=True, findings=(), started="2026-01-01T00:00:00+00:00"):
    return {
        "battle_tag": f"battle-gen9randombattle-{index}",
        "started_at": started,
        "result": "victoire" if won else "defaite",
        "won": won,
        "turn_count": 20,
        "my_team": ["garchomp"],
        "opponent_team": ["heatran"],
        "my_survivors": [],
        "opponent_survivors": [],
        "turns": [],
        "analysis": {
            "summary": "combat de test",
            "findings": [
                {"tag": tag, "severity": severity, "turns": [3], "note": "test"}
                for tag, severity in findings
            ],
        },
    }


class TempBattles:
    """Repertoire de combats jetable."""

    def __init__(self, logs):
        self.dir = tempfile.mkdtemp()
        for index, log in enumerate(logs):
            path = os.path.join(self.dir, f"{index:04d}.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(log, handle)

    def __enter__(self):
        return self.dir

    def __exit__(self, *args):
        shutil.rmtree(self.dir, ignore_errors=True)


class TestNetting(unittest.TestCase):
    """Le netting des antagonistes est le garde-fou anti-oscillation."""

    def setUp(self):
        self.taxonomy = load_taxonomy()
        self.controller = load_tuning_rules()["controller"]

    def _aggregate(self, logs):
        return aggregate_findings(logs)

    def test_tags_antagonistes_sannulent(self):
        logs = [make_log(i, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 2)]) for i in range(4)]
        logs += [make_log(i + 4, findings=[("SWITCHED_TOO_EAGERLY", 2)]) for i in range(4)]
        retained, ignored = net_scores(self._aggregate(logs), self.taxonomy, self.controller)
        self.assertNotIn("STAYED_IN_TYPE_DISADVANTAGE", retained)
        self.assertNotIn("SWITCHED_TOO_EAGERLY", retained)
        self.assertTrue(any("annule par" in reason for reason in ignored))

    def test_le_tag_dominant_survit_au_netting_avec_sa_severite_nette(self):
        logs = [make_log(i, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)]) for i in range(5)]
        logs += [make_log(i + 5, findings=[("SWITCHED_TOO_EAGERLY", 1)]) for i in range(2)]
        retained, _ = net_scores(self._aggregate(logs), self.taxonomy, self.controller)
        # 5 x severite 3 = 15, moins 2 x severite 1 = 2 -> 13
        self.assertEqual(retained["STAYED_IN_TYPE_DISADVANTAGE"], 13.0)

    def test_bande_morte_ignore_une_observation_isolee(self):
        logs = [make_log(0, findings=[("MISSED_KO_OPPORTUNITY", 3)])]
        logs += [make_log(i + 1) for i in range(9)]
        retained, ignored = net_scores(self._aggregate(logs), self.taxonomy, self.controller)
        self.assertEqual(retained, {})
        self.assertTrue(any("minimum requis" in reason for reason in ignored))

    def test_severite_nette_insuffisante_ignoree(self):
        # Vu dans 3 combats (seuil atteint) mais severite 1 chacun = 3.0.
        logs = [make_log(i, findings=[("IGNORED_HAZARDS", 1)]) for i in range(3)]
        logs += [make_log(i + 3) for i in range(7)]
        aggregate = self._aggregate(logs)
        controller = dict(self.controller, min_net_severity=5.0)
        retained, ignored = net_scores(aggregate, self.taxonomy, controller)
        self.assertEqual(retained, {})
        self.assertTrue(any("sous le seuil" in reason for reason in ignored))

    def test_tags_positifs_ne_declenchent_rien(self):
        logs = [make_log(i, findings=[("GOOD_DECISION", 3)]) for i in range(10)]
        retained, _ = net_scores(self._aggregate(logs), self.taxonomy, self.controller)
        self.assertEqual(retained, {})


class TestProposal(unittest.TestCase):
    def test_un_defaut_recurrent_produit_un_ajustement(self):
        logs = [make_log(i, won=False, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)])
                for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        self.assertTrue(proposal.changes)
        params = {c.param for c in proposal.changes}
        self.assertIn("switch.matchup_threshold", params)

    def test_le_sens_de_lajustement_suit_la_table(self):
        """STAYED_IN_TYPE_DISADVANTAGE doit faire switcher PLUS."""
        logs = [make_log(i, won=False, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)])
                for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        threshold = next(c for c in proposal.changes if c.param == "switch.matchup_threshold")
        # "Augmenter => le bot switche plus tot face a un mauvais matchup."
        self.assertGreater(threshold.after, threshold.before)
        momentum = next((c for c in proposal.changes if c.param == "switch.momentum_cost"), None)
        if momentum:
            self.assertLess(momentum.after, momentum.before)

    def test_le_sens_sinverse_avec_le_tag_oppose(self):
        logs = [make_log(i, won=False, findings=[("SWITCHED_TOO_EAGERLY", 3)])
                for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        threshold = next(c for c in proposal.changes if c.param == "switch.matchup_threshold")
        self.assertLess(threshold.after, threshold.before)

    def test_un_pas_de_registre_maximum_par_cycle(self):
        """Meme avec une severite ecrasante, on ne bouge que d'un pas."""
        logs = [make_log(i, won=False, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)])
                for i in range(30)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory, window=30)
        config = load_config()
        for change in proposal.changes:
            step = config.schema[change.param]["step"]
            self.assertLessEqual(abs(change.delta), step + 1e-9, change.param)

    def test_plafond_de_parametres_modifies_par_cycle(self):
        findings = [
            ("STAYED_IN_TYPE_DISADVANTAGE", 3),
            ("MISSED_KO_OPPORTUNITY", 3),
            ("HEALED_TOO_LATE", 3),
            ("IGNORED_HAZARDS", 3),
            ("LOW_ACCURACY_GAMBLE", 3),
        ]
        logs = [make_log(i, won=False, findings=findings) for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        limit = load_tuning_rules()["controller"]["max_params_changed_per_cycle"]
        self.assertLessEqual(len(proposal.changes), limit)
        self.assertTrue(any("plafond" in reason for reason in proposal.ignored))

    def test_aucun_changement_sur_des_combats_propres(self):
        logs = [make_log(i, findings=[("GOOD_DECISION", 2)]) for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        self.assertTrue(proposal.is_empty)

    def test_chaque_changement_est_attribuable(self):
        """Sans tracabilite, aucun ajustement n'est debuggable."""
        logs = [make_log(i, won=False, findings=[("HEALED_TOO_LATE", 3)]) for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        for change in proposal.changes:
            self.assertTrue(change.votes, "changement sans tag declencheur")
            self.assertIn("HEALED_TOO_LATE", change.votes)
            self.assertIn("->", change.describe())

    def test_proposition_serialisable(self):
        logs = [make_log(i, won=False, findings=[("HEALED_TOO_LATE", 3)]) for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)
        payload = json.loads(json.dumps(proposal.to_dict()))
        self.assertIn("changes", payload)
        self.assertIn("ignored", payload)
        self.assertIn("net_scores", payload)


class TestApplyAndBounds(unittest.TestCase):
    def test_config_reste_valide_apres_application(self):
        logs = [make_log(i, won=False, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)])
                for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(directory=directory)

        config = load_config()
        config.params = dict(config.params)
        for change in proposal.changes:
            config.params[change.param] = change.after
        self.assertEqual(validate(config.params, config.schema), [])

    def test_un_parametre_a_sa_borne_ne_bouge_plus(self):
        config = load_config()
        config.params = dict(config.params)
        config.params["switch.matchup_threshold"] = config.schema["switch.matchup_threshold"]["max"]
        logs = [make_log(i, won=False, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)])
                for i in range(10)]
        with TempBattles(logs) as directory:
            proposal = propose(config=config, directory=directory)
        moved = {c.param for c in proposal.changes}
        self.assertNotIn("switch.matchup_threshold", moved)
        self.assertTrue(any("borne" in reason for reason in proposal.ignored))

    def test_cycles_repetes_convergent_vers_la_borne_sans_la_depasser(self):
        """40 cycles du meme defaut: la valeur sature, elle ne diverge pas."""
        config = load_config()
        config.params = dict(config.params)
        logs = [make_log(i, won=False, findings=[("LOW_ACCURACY_GAMBLE", 3)])
                for i in range(10)]
        maximum = config.schema["attack.accuracy_weight"]["max"]
        with TempBattles(logs) as directory:
            for _ in range(40):
                proposal = propose(config=config, directory=directory)
                for change in proposal.changes:
                    config.params[change.param] = change.after
        self.assertLessEqual(config.params["attack.accuracy_weight"], maximum)
        self.assertEqual(validate(config.params, config.schema), [])

    def test_alternance_de_defauts_opposes_ne_diverge_pas(self):
        """Le scenario d'oscillation: un cycle 'reste trop', le suivant 'switche trop'."""
        config = load_config()
        config.params = dict(config.params)
        start = config.params["switch.momentum_cost"]
        step = config.schema["switch.momentum_cost"]["step"]

        stay = [make_log(i, won=False, findings=[("STAYED_IN_TYPE_DISADVANTAGE", 3)])
                for i in range(10)]
        switch = [make_log(i, won=False, findings=[("SWITCHED_TOO_EAGERLY", 3)])
                  for i in range(10)]

        for cycle in range(10):
            logs = stay if cycle % 2 == 0 else switch
            with TempBattles(logs) as directory:
                proposal = propose(config=config, directory=directory)
            for change in proposal.changes:
                config.params[change.param] = change.after

        # L'alternance fait osciller d'au plus un pas autour du point de depart,
        # elle ne peut pas emporter le parametre a une extremite.
        self.assertLessEqual(abs(config.params["switch.momentum_cost"] - start), step + 1e-9)


class TestRollback(unittest.TestCase):
    def setUp(self):
        self.history = tempfile.mkdtemp()
        # check_rollback ecrit la config via save_config(config), qui suit
        # config.path. Sans redirection, les tests ecraseraient le fichier de
        # configuration livre avec le projet.
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        handle.close()
        self.config_path = handle.name

    def tearDown(self):
        shutil.rmtree(self.history, ignore_errors=True)
        if os.path.exists(self.config_path):
            os.unlink(self.config_path)

    def _prepare(self, winrate_before, results_after):
        config = load_config()
        config.params = dict(config.params)
        config.path = self.config_path
        snapshot = {
            **config.to_dict(),
            "proposal": {
                "changes": [
                    {
                        "param": "switch.momentum_cost",
                        "before": 0.8,
                        "after": 0.9,
                        "votes": ["SWITCHED_TOO_EAGERLY"],
                        "description": "test",
                    }
                ]
            },
            "winrate_before": winrate_before,
            "battles_before": 0,
        }
        with open(os.path.join(self.history, f"{config.config_version:04d}.json"), "w") as handle:
            json.dump(snapshot, handle)
        previous = config.config_version - 1
        with open(os.path.join(self.history, f"{previous:04d}.json"), "w") as handle:
            json.dump(config.to_dict(), handle)
        logs = [make_log(i, won=won) for i, won in enumerate(results_after)]
        return config, logs

    def test_pas_de_verdict_sur_trop_peu_de_combats(self):
        config, logs = self._prepare(0.6, [False] * 5)
        with TempBattles(logs) as directory:
            result = check_rollback(config=config, directory=directory, history_dir=self.history)
        self.assertFalse(result["rolled_back"])
        self.assertIn("minimum", result["reason"])

    def test_pas_de_rollback_sans_degradation_franche(self):
        config, logs = self._prepare(0.5, [True] * 12 + [False] * 12)
        with TempBattles(logs) as directory:
            result = check_rollback(config=config, directory=directory, history_dir=self.history)
        self.assertFalse(result["rolled_back"])
        self.assertIn("pas de degradation", result["reason"])

    def test_chute_franche_du_winrate_declenche_le_rollback(self):
        config, logs = self._prepare(0.7, [False] * 22 + [True] * 2)
        with TempBattles(logs) as directory:
            result = check_rollback(config=config, directory=directory, history_dir=self.history)
        self.assertTrue(result["rolled_back"])
        self.assertLess(result["winrate_after"], result["winrate_before"])
        marker = [n for n in os.listdir(self.history) if n.endswith(".rollback.json")]
        self.assertEqual(len(marker), 1)


class TestShippedConfig(unittest.TestCase):
    """La config versionnee doit rester les valeurs par defaut du registre.

    Sans ce test, une suite mal isolee peut ecrire dans config/heuristics.json
    et faire partir tout le monde de reglages deja derives, en silence.
    """

    def test_config_livree_est_la_v1_par_defaut(self):
        config = load_config()
        self.assertEqual(config.config_version, 1)
        self.assertIsNone(config.derived_from)
        defaults = {key: spec["default"] for key, spec in config.schema.items()}
        self.assertEqual(config.params, defaults)


if __name__ == "__main__":
    unittest.main()
