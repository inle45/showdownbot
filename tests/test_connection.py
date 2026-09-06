"""Tests du superviseur de reconnexion, avec coupures simulees.

poke-env ne reconnecte pas: c'est ce module qui porte toute la robustesse
reseau du bot sur telephone. On simule ici les pannes reelles (coupure en plein
combat, serveur injoignable, reprise partielle) sans serveur Showdown.

    python -m unittest tests.test_connection -v
"""

import asyncio
import unittest

import bot  # noqa: F401
from bot.connection import ConnectionSupervisor, RetryPolicy


class FakePlayer:
    """Joueur minimal: expose ce que le superviseur lit reellement."""

    def __init__(self, battles_before_failure):
        self.n_finished_battles = 0
        self._budget = battles_before_failure
        self.stopped = False

        class _Client:
            def __init__(self, outer):
                self._outer = outer

            async def stop_listening(self):
                self._outer.stopped = True

        self.ps_client = _Client(self)

    async def play(self, remaining):
        """Joue jusqu'a epuisement du budget, puis simule une coupure."""
        played = min(remaining, self._budget)
        self.n_finished_battles = played
        if played < remaining:
            raise ConnectionResetError("websocket ferme par le pair")


class NoSleep:
    """Remplace asyncio.sleep et enregistre les delais demandes."""

    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


def run(coro):
    return asyncio.run(coro)


class TestRetryPolicy(unittest.TestCase):
    def test_backoff_exponentiel_et_plafonne(self):
        policy = RetryPolicy(initial_backoff=2.0, factor=2.0, max_backoff=60.0, jitter=0.0)
        self.assertEqual(policy.delay_for(1), 2.0)
        self.assertEqual(policy.delay_for(2), 4.0)
        self.assertEqual(policy.delay_for(3), 8.0)
        self.assertEqual(policy.delay_for(10), 60.0)

    def test_jitter_reste_dans_la_fenetre(self):
        policy = RetryPolicy(initial_backoff=10.0, factor=1.0, jitter=0.25)
        for _ in range(50):
            self.assertTrue(7.5 <= policy.delay_for(1) <= 12.5)


class TestSupervisor(unittest.TestCase):
    def test_aucune_coupure_aucune_reconnexion(self):
        sleep = NoSleep()
        supervisor = ConnectionSupervisor(lambda: FakePlayer(5), sleep=sleep)
        played = run(supervisor.run(lambda p, r: p.play(r), target_battles=5))
        self.assertEqual(played, 5)
        self.assertEqual(supervisor.reconnections, 0)
        self.assertEqual(sleep.delays, [])

    def test_reprend_apres_une_coupure_en_plein_combat(self):
        """Chaque session joue 2 combats puis coupe: 5 combats = 3 sessions."""
        sleep = NoSleep()
        supervisor = ConnectionSupervisor(lambda: FakePlayer(2), sleep=sleep)
        played = run(supervisor.run(lambda p, r: p.play(r), target_battles=5))
        self.assertEqual(played, 5)
        self.assertEqual(supervisor.reconnections, 2)

    def test_ne_recompte_pas_les_combats_deja_joues(self):
        """Le compteur est cumulatif: une reprise ne repart pas de zero."""
        seen_remaining = []

        async def task(player, remaining):
            seen_remaining.append(remaining)
            await player.play(remaining)

        supervisor = ConnectionSupervisor(lambda: FakePlayer(3), sleep=NoSleep())
        run(supervisor.run(task, target_battles=7))
        self.assertEqual(seen_remaining, [7, 4, 1])

    def test_abandonne_apres_le_nombre_max_de_tentatives(self):
        """Serveur totalement injoignable: on s'arrete au lieu de boucler."""
        sleep = NoSleep()
        supervisor = ConnectionSupervisor(
            lambda: FakePlayer(0), policy=RetryPolicy(max_attempts=4, jitter=0.0), sleep=sleep
        )
        played = run(supervisor.run(lambda p, r: p.play(r), target_battles=3))
        self.assertEqual(played, 0)
        self.assertEqual(supervisor.attempts, 4)
        self.assertEqual(len(sleep.delays), 4)

    def test_delais_croissants_entre_les_tentatives(self):
        sleep = NoSleep()
        supervisor = ConnectionSupervisor(
            lambda: FakePlayer(0),
            policy=RetryPolicy(max_attempts=4, initial_backoff=2.0, factor=2.0, jitter=0.0),
            sleep=sleep,
        )
        run(supervisor.run(lambda p, r: p.play(r), target_battles=3))
        self.assertEqual(sleep.delays, [2.0, 4.0, 8.0, 16.0])

    def test_websocket_ferme_a_chaque_tentative(self):
        """Une session morte doit etre fermee, sinon les sockets s'accumulent."""
        created = []

        def factory():
            player = FakePlayer(1)
            created.append(player)
            return player

        supervisor = ConnectionSupervisor(factory, sleep=NoSleep())
        run(supervisor.run(lambda p, r: p.play(r), target_battles=3))
        self.assertEqual(len(created), 3)
        self.assertTrue(all(p.stopped for p in created))

    def test_joueur_neuf_a_chaque_reconnexion(self):
        """Reutiliser le client mort echoue silencieusement: on en refait un."""
        created = []
        supervisor = ConnectionSupervisor(
            lambda: created.append(FakePlayer(1)) or created[-1], sleep=NoSleep()
        )
        run(supervisor.run(lambda p, r: p.play(r), target_battles=3))
        self.assertEqual(len(set(id(p) for p in created)), 3)

    def test_erreur_inattendue_traitee_comme_une_coupure(self):
        class Exploding(FakePlayer):
            async def play(self, remaining):
                raise RuntimeError("erreur inattendue du protocole")

        supervisor = ConnectionSupervisor(
            lambda: Exploding(0), policy=RetryPolicy(max_attempts=2, jitter=0.0), sleep=NoSleep()
        )
        played = run(supervisor.run(lambda p, r: p.play(r), target_battles=1))
        self.assertEqual(played, 0)

    def test_annulation_propagee(self):
        """Un Ctrl-C ne doit pas etre avale par la boucle de reprise."""

        async def cancelling(player, remaining):
            raise asyncio.CancelledError()

        supervisor = ConnectionSupervisor(lambda: FakePlayer(0), sleep=NoSleep())
        with self.assertRaises(asyncio.CancelledError):
            run(supervisor.run(cancelling, target_battles=1))


if __name__ == "__main__":
    unittest.main()
