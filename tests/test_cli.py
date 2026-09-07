"""Tests de l'orchestration automatique consolidation+tuning du CLI.

Cible specifiquement le bug trouve en conditions reelles: pendant une longue
session sans surveillance (ladder toute la nuit), chaque declenchement
automatique (tous les CONSOLIDATE_EVERY combats) ecrasait silencieusement
toute proposition deja calculee et pas encore validee. Sur ~70 combats
(~7 declenchements possibles), seules les propositions attrapees a temps par
l'utilisateur ont fini par s'appliquer - les autres etaient perdues sans
qu'il le sache.

    python -m unittest tests.test_cli -v
"""

import unittest
from unittest.mock import MagicMock, patch

import bot  # noqa: F401
from bot.cli import _consolidate_and_tune
from bot.config import Settings


class TestAutoTuneDoesNotClobberPending(unittest.TestCase):
    def setUp(self):
        # La consolidation (fiche de lecons) est independante du bug vise ici:
        # on la neutralise pour isoler le comportement du tuner.
        patcher = patch("analysis.consolidate.consolidate", return_value={"headline": "t"})
        self.addCleanup(patcher.stop)
        patcher.start()

        rollback_patcher = patch(
            "analysis.tuner.check_rollback", return_value={"rolled_back": False}
        )
        self.addCleanup(rollback_patcher.stop)
        rollback_patcher.start()

    def test_ne_recalcule_pas_si_une_proposition_attend_deja(self):
        """Le coeur du correctif: propose() ne doit meme pas etre appele."""
        with patch("analysis.tuner.load_pending", return_value={"changes": [{}]}) as pending, \
             patch("analysis.tuner.propose") as propose_mock, \
             patch("analysis.tuner.save_pending") as save_mock:
            _consolidate_and_tune(Settings(tuning_mode="propose", anthropic_api_key="x"))

        pending.assert_called_once()
        propose_mock.assert_not_called()
        save_mock.assert_not_called()

    def test_recalcule_normalement_sans_proposition_en_attente(self):
        fake_proposal = MagicMock(is_empty=False, changes=[MagicMock()])
        with patch("analysis.tuner.load_pending", return_value=None), \
             patch("analysis.tuner.propose", return_value=fake_proposal) as propose_mock, \
             patch("analysis.tuner.save_pending") as save_mock:
            _consolidate_and_tune(Settings(tuning_mode="propose", anthropic_api_key="x"))

        propose_mock.assert_called_once()
        save_mock.assert_called_once_with(fake_proposal)

    def test_mode_auto_ignore_la_proposition_en_attente(self):
        """En mode auto il n'y a jamais de proposition qui 'attend': chaque
        cycle s'applique directement, donc le garde-fou ne doit pas le bloquer."""
        fake_proposal = MagicMock(is_empty=False, changes=[MagicMock()])
        with patch("analysis.tuner.load_pending", return_value={"changes": [{}]}), \
             patch("analysis.tuner.propose", return_value=fake_proposal) as propose_mock, \
             patch("analysis.tuner.apply_proposal", return_value={"version": 5, "changes": []}) as apply_mock:
            _consolidate_and_tune(Settings(tuning_mode="auto", anthropic_api_key="x"))

        propose_mock.assert_called_once()
        apply_mock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
