"""Tests de la chaine d'analyse, avec un client API simule.

Ces modules ne peuvent pas etre exerces sans cle API, et ils portent pourtant le
chemin critique du projet: si le .md n'est pas ecrit ou si les findings ne sont
pas reinjectes dans le log, le tuner ne voit plus rien. Le faux client permet de
verifier toute la plomberie, et surtout la garantie centrale du projet:
UN SEUL appel API par combat.

    python -m unittest tests.test_analysis -v
"""

import json
import os
import shutil
import tempfile
import unittest

import bot  # noqa: F401
from analysis.client import CallResult, estimate_cost
from analysis.consolidate import aggregate_findings
from analysis.post_battle import analyse_battle, pending_battles
from analysis.prompts import build_system_prompt, distill_battle
from bot.config import load_taxonomy


class FakeAnalyst:
    """Client API simule: compte les appels et rend une reponse conforme."""

    def __init__(self, payload=None):
        self.calls = []
        self.payload = payload or {
            "summary": "Defaite sur un mauvais matchup au milieu de partie.",
            "markdown_report": "## Resume\nLe bot reste trop longtemps face a Raichu.",
            "findings": [
                {
                    "tag": "STAYED_IN_TYPE_DISADVANTAGE",
                    "severity": 2,
                    "turns": [3],
                    "note": "switch a 5.28 contre attaque a 0.35",
                }
            ],
        }

    def structured_call(self, model, system, user_content, json_schema, max_tokens=16000):
        self.calls.append(
            {
                "model": model,
                "system": system,
                "user_content": user_content,
                "schema": json_schema,
            }
        )
        return CallResult(
            data=self.payload,
            model=model,
            input_tokens=6800,
            output_tokens=1200,
            cost_usd=0.0256,
            duration_s=4.2,
        )


def sample_log(analysed=False):
    log = {
        "battle_tag": "battle-gen9randombattle-42",
        "battle_format": "gen9randombattle",
        "started_at": "2026-09-06T16:00:00+00:00",
        "server": "local",
        "config_version": 1,
        "config_params": {"switch.momentum_cost": 0.8},
        "result": "defaite",
        "won": False,
        "turn_count": 3,
        "my_team": ["garchomp", "corviknight"],
        "opponent_team": ["raichu"],
        "my_survivors": [],
        "opponent_survivors": ["raichu"],
        "turns": [
            {
                "turn": 3,
                "decision": {
                    "chosen": {
                        "kind": "move",
                        "name": "bodypress",
                        "score": 0.35,
                        "tera": False,
                        "components": {"damage": 0.35},
                        "detail": {},
                    },
                    "candidates": [
                        {
                            "kind": "switch",
                            "name": "garchomp",
                            "score": 5.28,
                            "tera": False,
                            "components": {"matchup": 4.0},
                            "detail": {},
                        }
                    ],
                    "context": {
                        "turn": 3,
                        "active": "corviknight",
                        "active_hp_fraction": 0.54,
                        "opponent": "raichu",
                        "opponent_hp_fraction": 0.8,
                        "matchup": -2.0,
                        "faster": False,
                        "predicted_incoming_fraction": 1.17,
                        "switch_urgency": 2.34,
                        "opponent_revealed_moves": ["thunderbolt"],
                        "safety_override": True,
                    },
                },
            }
        ],
    }
    if analysed:
        log["analysis"] = {"summary": "deja fait", "findings": []}
    return log


class TempBattle:
    def __init__(self, log):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "20260906-1600-battle.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            json.dump(log, handle)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self):
        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle)


class TestPostBattle(unittest.TestCase):
    def setUp(self):
        # record_usage ecrit dans memory/api_usage.jsonl. Sans redirection, la
        # suite de tests polluerait le journal de couts reel, et le dashboard
        # afficherait des appels qui n'ont jamais eu lieu.
        import analysis.client

        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        handle.close()
        self.usage_path = handle.name
        self._real_usage_path = analysis.client.USAGE_PATH
        analysis.client.USAGE_PATH = self.usage_path

    def tearDown(self):
        import analysis.client

        analysis.client.USAGE_PATH = self._real_usage_path
        if os.path.exists(self.usage_path):
            os.unlink(self.usage_path)

    def test_appel_journalise_avec_son_cout(self):
        """Le journal d'usage est ce qui rend le cout verifiable."""
        from analysis.client import load_usage

        analyst = FakeAnalyst()
        with TempBattle(sample_log()) as battle:
            analyse_battle(battle.path, api_key="test", analyst=analyst)
        entries = load_usage(self.usage_path)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["kind"], "post_battle")
        self.assertEqual(entries[0]["cost_usd"], 0.0256)
        self.assertEqual(entries[0]["battle_tag"], "battle-gen9randombattle-42")

    def test_un_seul_appel_api_par_combat(self):
        """La garantie centrale du projet."""
        analyst = FakeAnalyst()
        with TempBattle(sample_log()) as battle:
            analyse_battle(battle.path, api_key="test", analyst=analyst)
        self.assertEqual(len(analyst.calls), 1)

    def test_rapport_markdown_ecrit(self):
        analyst = FakeAnalyst()
        with TempBattle(sample_log()) as battle:
            outcome = analyse_battle(battle.path, api_key="test", analyst=analyst)
            self.assertTrue(os.path.exists(outcome["markdown_path"]))
            with open(outcome["markdown_path"], "r", encoding="utf-8") as handle:
                content = handle.read()
        self.assertIn("battle-gen9randombattle-42", content)
        self.assertIn("STAYED_IN_TYPE_DISADVANTAGE", content)
        self.assertIn("Le bot reste trop longtemps", content)
        self.assertIn("$0.0256", content)

    def test_findings_reinjectes_dans_le_log(self):
        """Sans cela, le tuner ne verrait jamais les observations."""
        analyst = FakeAnalyst()
        with TempBattle(sample_log()) as battle:
            analyse_battle(battle.path, api_key="test", analyst=analyst)
            stored = battle.read()
        self.assertIn("analysis", stored)
        self.assertEqual(len(stored["analysis"]["findings"]), 1)
        self.assertEqual(stored["analysis"]["findings"][0]["tag"], "STAYED_IN_TYPE_DISADVANTAGE")
        self.assertEqual(stored["analysis"]["cost_usd"], 0.0256)

    def test_combat_deja_analyse_ne_rappelle_pas_lapi(self):
        """Relancer l'analyse ne doit pas repayer un combat deja traite."""
        analyst = FakeAnalyst()
        with TempBattle(sample_log(analysed=True)) as battle:
            outcome = analyse_battle(battle.path, api_key="test", analyst=analyst)
        self.assertEqual(len(analyst.calls), 0)
        self.assertTrue(outcome["skipped"])
        self.assertEqual(outcome["cost_usd"], 0.0)

    def test_le_prompt_contient_la_trace_de_decision(self):
        """C'est ce qui distingue cette analyse d'un commentaire de resultat."""
        analyst = FakeAnalyst()
        with TempBattle(sample_log()) as battle:
            analyse_battle(battle.path, api_key="test", analyst=analyst)
        sent = analyst.calls[0]["user_content"]
        self.assertIn("bodypress", sent)
        self.assertIn("5.28", sent)       # le score du switch ecarte
        self.assertIn("matchup -2.0", sent)
        self.assertIn("GARDE-FOU", sent)

    def test_le_schema_contraint_le_vocabulaire(self):
        analyst = FakeAnalyst()
        with TempBattle(sample_log()) as battle:
            analyse_battle(battle.path, api_key="test", analyst=analyst)
        schema = analyst.calls[0]["schema"]
        enum = schema["properties"]["findings"]["items"]["properties"]["tag"]["enum"]
        self.assertEqual(sorted(enum), sorted(load_taxonomy().keys()))
        self.assertFalse(schema["additionalProperties"])

    def test_pending_battles_ignore_les_combats_analyses(self):
        with TempBattle(sample_log()) as battle:
            self.assertEqual(len(pending_battles(battle.dir)), 1)
        with TempBattle(sample_log(analysed=True)) as battle:
            self.assertEqual(pending_battles(battle.dir), [])


class TestPrompts(unittest.TestCase):
    def test_prompt_systeme_liste_tout_le_vocabulaire(self):
        taxonomy = load_taxonomy()
        prompt = build_system_prompt(taxonomy)
        for tag in taxonomy:
            self.assertIn(tag, prompt)

    def test_prompt_systeme_interdit_le_biais_de_resultat(self):
        prompt = build_system_prompt(load_taxonomy())
        self.assertIn("A CE", prompt)
        self.assertIn("resultat du combat", prompt)

    def test_prompt_systeme_stable_donc_cachable(self):
        taxonomy = load_taxonomy()
        self.assertEqual(build_system_prompt(taxonomy), build_system_prompt(taxonomy))

    def test_distillation_reste_compacte(self):
        text = distill_battle(sample_log())
        self.assertLess(len(text), 4000)
        self.assertIn("Deroule tour par tour", text)

    def test_distillation_tronque_les_combats_tres_longs(self):
        log = sample_log()
        log["turns"] = log["turns"] * 200
        text = distill_battle(log)
        self.assertIn("tours intermediaires omis", text)


class TestAggregate(unittest.TestCase):
    def test_agregat_compte_par_combat_et_par_severite(self):
        logs = [
            {"won": True, "analysis": {"findings": [{"tag": "A", "severity": 2}]}},
            {"won": False, "analysis": {"findings": [
                {"tag": "A", "severity": 3}, {"tag": "B", "severity": 1}]}},
        ]
        aggregate = aggregate_findings(logs)
        self.assertEqual(aggregate["counts"]["A"], 2)
        self.assertEqual(aggregate["severity"]["A"], 5.0)
        self.assertEqual(aggregate["battles_with_tag"]["A"], 2)
        self.assertEqual(aggregate["winrate"], 0.5)

    def test_agregat_vide(self):
        aggregate = aggregate_findings([])
        self.assertEqual(aggregate["battles"], 0)
        self.assertEqual(aggregate["winrate"], 0.0)


class TestCost(unittest.TestCase):
    def test_lecture_de_cache_moins_chere_que_lentree(self):
        class Cached:
            input_tokens = 0
            output_tokens = 0
            cache_read_input_tokens = 10000
            cache_creation_input_tokens = 0

        class Plain:
            input_tokens = 10000
            output_tokens = 0
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        self.assertLess(
            estimate_cost("claude-sonnet-5", Cached()),
            estimate_cost("claude-sonnet-5", Plain()),
        )

    def test_modele_inconnu_ne_leve_pas(self):
        class U:
            input_tokens = 100
            output_tokens = 100
            cache_read_input_tokens = 0
            cache_creation_input_tokens = 0

        self.assertEqual(estimate_cost("modele-inexistant", U()), 0.0)


class _FakeUsage:
    input_tokens = 10
    output_tokens = 10
    cache_read_input_tokens = 0
    cache_creation_input_tokens = 0


class _FakeStream:
    """Simule client.messages.stream(...) as ... / .get_final_message()."""

    def __init__(self, captured_kwargs, response):
        self._kwargs = captured_kwargs
        self._response = response

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return self._response


class _FakeMessage:
    def __init__(self, text):
        block = type("Block", (), {"type": "text", "text": text})()
        self.content = [block]
        self.stop_reason = "end_turn"
        self.usage = _FakeUsage()
        self._request_id = "req_test"


class TestThinkingConfiguration(unittest.TestCase):
    """Le raisonnement adaptatif etait le vrai cout cache (mesure en conditions
    reelles: ~12000-13000 tokens de sortie meme a effort 'low'). Ces tests
    figent le comportement corrige: desactive par defaut, sans effort envoye
    tant qu'il l'est."""

    def _analyst_with_fake_client(self, **kwargs):
        from analysis.client import AnthropicAnalyst

        analyst = AnthropicAnalyst(api_key="test", **kwargs)
        captured = {}

        class _FakeMessagesResource:
            def stream(self, **call_kwargs):
                captured.update(call_kwargs)
                return _FakeStream(captured, _FakeMessage('{"a": 1}'))

        class _FakeClient:
            messages = _FakeMessagesResource()

        analyst._client = _FakeClient()
        return analyst, captured

    def test_raisonnement_desactive_par_defaut(self):
        analyst, captured = self._analyst_with_fake_client()
        analyst.structured_call("claude-sonnet-5", "system", "user", {"type": "object"})
        self.assertEqual(captured["thinking"], {"type": "disabled"})
        self.assertNotIn("effort", captured["output_config"])

    def test_effort_non_envoye_sans_raisonnement(self):
        analyst, captured = self._analyst_with_fake_client(effort="high")
        analyst.structured_call("claude-sonnet-5", "system", "user", {"type": "object"})
        self.assertNotIn("effort", captured["output_config"])

    def test_raisonnement_active_explicitement(self):
        analyst, captured = self._analyst_with_fake_client(thinking=True, effort="low")
        analyst.structured_call("claude-sonnet-5", "system", "user", {"type": "object"})
        self.assertEqual(captured["thinking"], {"type": "adaptive"})
        self.assertEqual(captured["output_config"]["effort"], "low")


if __name__ == "__main__":
    unittest.main()
