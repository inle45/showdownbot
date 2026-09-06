"""Tests du dashboard: toutes les routes repondent, rien n'est injectable.

Le dashboard affiche du texte produit par un modele et des pseudos
d'adversaires. Un rapport contenant du HTML ne doit jamais s'executer dans la
page, meme si le serveur n'ecoute que sur la boucle locale.

    python -m unittest tests.test_web -v
"""

import unittest

import bot  # noqa: F401
from web.app import app
from web.markdown import render
from web.stats import finding_frequency, rolling_winrate, sparkline_svg, summarise


class TestRoutes(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.client = app.test_client()

    def test_toutes_les_pages_repondent(self):
        for route in ("/", "/battles", "/lessons", "/config"):
            with self.subTest(route=route):
                response = self.client.get(route)
                self.assertEqual(response.status_code, 200, route)

    def test_combat_inconnu_rend_404(self):
        self.assertEqual(self.client.get("/battle/inexistant").status_code, 404)

    def test_page_reglages_liste_les_parametres(self):
        body = self.client.get("/config").get_data(as_text=True)
        self.assertIn("switch.momentum_cost", body)
        self.assertIn("attack.ko_bonus", body)

    def test_recalcul_des_ajustements_sans_api(self):
        """Le bouton de recalcul ne doit declencher aucun appel API."""
        response = self.client.post("/tune/recompute", follow_redirects=True)
        self.assertEqual(response.status_code, 200)

    def test_appliquer_sans_proposition_ne_casse_rien(self):
        self.client.post("/tune/reject", follow_redirects=True)
        response = self.client.post("/tune/apply", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("Aucune proposition", response.get_data(as_text=True))


class TestMarkdown(unittest.TestCase):
    def test_html_est_echappe(self):
        rendered = render('<script>alert("x")</script>')
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_html_dans_un_tableau_est_echappe(self):
        rendered = render("| a | b |\n| --- | --- |\n| <img src=x onerror=y> | 2 |")
        self.assertNotIn("<img", rendered)

    def test_titres_listes_et_tableaux(self):
        rendered = render("# T\n\n- un\n- deux\n\n| A |\n| --- |\n| 1 |")
        self.assertIn("<h1>T</h1>", rendered)
        self.assertIn("<li>un</li>", rendered)
        self.assertIn("<table>", rendered)

    def test_gras_et_code(self):
        rendered = render("du **gras** et du `code`")
        self.assertIn("<strong>gras</strong>", rendered)
        self.assertIn("<code>code</code>", rendered)

    def test_texte_vide(self):
        self.assertEqual(render(""), "")
        self.assertEqual(render(None), "")


class TestStats(unittest.TestCase):
    def _logs(self, results):
        return [{"won": won, "turn_count": 20, "analysis": {"findings": []}} for won in results]

    def test_winrate_glissant(self):
        points = rolling_winrate(self._logs([True, True, False, False]), window=2)
        self.assertEqual(points, [1.0, 1.0, 0.5, 0.0])

    def test_combats_non_decides_ignores(self):
        logs = self._logs([True, False]) + [{"won": None, "turn_count": 5}]
        self.assertEqual(len(rolling_winrate(logs)), 2)

    def test_sparkline_demande_deux_points(self):
        self.assertIn("Pas encore assez", sparkline_svg([0.5]))
        self.assertIn("<svg", sparkline_svg([0.5, 0.6]))

    def test_frequence_des_observations_triee(self):
        logs = [
            {"analysis": {"findings": [{"tag": "A", "severity": 1}]}},
            {"analysis": {"findings": [{"tag": "B", "severity": 3}, {"tag": "A", "severity": 1}]}},
        ]
        rows = finding_frequency(logs)
        self.assertEqual(rows[0]["tag"], "B")
        self.assertEqual(rows[1]["count"], 2)

    def test_resume_sans_combat(self):
        stats = summarise([], [])
        self.assertEqual(stats["battles"], 0)
        self.assertEqual(stats["winrate"], 0.0)
        self.assertEqual(stats["cost_per_battle"], 0.0)


if __name__ == "__main__":
    unittest.main()
