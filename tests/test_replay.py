"""Tests du generateur de replay anime.

    python -m unittest tests.test_replay -v
"""

import unittest

import bot  # noqa: F401
from web.replay import build_replay_html


class TestBuildReplayHtml(unittest.TestCase):
    def test_aucun_protocole_rend_none(self):
        self.assertIsNone(build_replay_html({"battle_tag": "battle-x"}))
        self.assertIsNone(build_replay_html({"battle_tag": "battle-x", "protocol": []}))

    def test_protocole_present_produit_du_html(self):
        html = build_replay_html(
            {
                "battle_tag": "battle-gen9randombattle-1",
                "opponent_username": "Rival",
                "protocol": ["|player|p1|Bot|1|", "|player|p2|Rival|1|", "|win|Bot"],
            }
        )
        self.assertIsNotNone(html)
        self.assertIn("battle-gen9randombattle-1", html)
        self.assertIn("|win|Bot", html)
        self.assertIn("Rival", html)
        # Le moteur d'animation officiel, charge cote client.
        self.assertIn("play.pokemonshowdown.com/js/replay-embed.js", html)

    def test_pseudo_joueur_par_defaut_si_absent(self):
        html = build_replay_html(
            {"battle_tag": "battle-x", "protocol": ["|win|Bot"]}, player_username=None
        )
        self.assertIn("Bot", html)

    def test_caracteres_speciaux_dans_le_tag_ne_font_pas_planter_le_rendu(self):
        """battle_tag vient du serveur Showdown (jamais d'un utilisateur), donc
        ce n'est pas une frontiere de securite a durcir - juste une garantie
        que des accolades presentes dedans ne cassent pas le rendu."""
        html = build_replay_html(
            {"battle_tag": "battle-{REPLAY_LOG}-x", "protocol": ["|win|Bot"]}
        )
        self.assertIsNotNone(html)


if __name__ == "__main__":
    unittest.main()
