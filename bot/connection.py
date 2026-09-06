"""Connexion au serveur Showdown et superviseur de reconnexion.

poke-env n'a AUCUNE reconnexion integree: ``PSClient.listen()` se connecte une
fois et, a la fermeture du websocket, se contente de journaliser puis rend la
main. Sur un telephone (bascule wifi/4G, veille Android, perte de reseau dans le
metro), c'est la panne la plus frequente. Ce module ajoute donc la couche
manquante: nouvelle tentative avec backoff exponentiel, nouveau client, nouvelle
authentification, et reprise du nombre de combats restants.

Le superviseur est volontairement injectable (``player_factory``, ``sleep``)
pour pouvoir simuler des coupures dans les tests, sans serveur.
"""

import asyncio
import logging
import random
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from poke_env.ps_client import AccountConfiguration
from poke_env.ps_client.server_configuration import (
    LocalhostServerConfiguration,
    ServerConfiguration,
    ShowdownServerConfiguration,
)

from bot.config import HeuristicConfig, Settings
from bot.player import HeuristicPlayer

LOGGER = logging.getLogger("showdownbot.connection")

# Backoff: 2, 4, 8, 16, 32... plafonne. Le plafond est bas (5 min) car une
# coupure de reseau sur telephone se resout en general en quelques secondes.
INITIAL_BACKOFF = 2.0
MAX_BACKOFF = 300.0
BACKOFF_FACTOR = 2.0


def server_configuration(settings: Settings) -> ServerConfiguration:
    if settings.server == "online":
        return ShowdownServerConfiguration
    return LocalhostServerConfiguration


def spectator_url(battle_tag: str, server_label: str) -> str:
    """Lien pour suivre un combat en direct dans un navigateur.

    Sur le serveur officiel, un salon de combat est directement accessible a
    cette URL, sans compte requis pour observer. En local, le meme serveur
    Node sert aussi le client web sur le meme port (voir LocalhostServerConfiguration).
    """
    if server_label == "online":
        return f"https://play.pokemonshowdown.com/{battle_tag}"
    return f"http://localhost:8000/{battle_tag}"


def account_configuration(settings: Settings) -> Optional[AccountConfiguration]:
    """Compte Showdown, ou None pour un serveur local --no-security."""
    if not settings.username:
        return None
    return AccountConfiguration(settings.username, settings.password or None)


@dataclass
class RetryPolicy:
    max_attempts: int = 8
    initial_backoff: float = INITIAL_BACKOFF
    max_backoff: float = MAX_BACKOFF
    factor: float = BACKOFF_FACTOR
    jitter: float = 0.25

    def delay_for(self, attempt: int) -> float:
        """Delai avant la tentative ``attempt`` (1 = premiere reprise)."""
        base = min(self.initial_backoff * (self.factor ** (attempt - 1)), self.max_backoff)
        # Le jitter evite que plusieurs instances ne se reconnectent en meme
        # temps apres une coupure commune.
        return base * (1.0 + random.uniform(-self.jitter, self.jitter))


class ConnectionSupervisor:
    """Relance une tache de jeu tant qu'elle n'a pas atteint son objectif.

    ``player_factory`` doit rendre un joueur NEUF a chaque appel: apres une
    coupure, le websocket et la session d'authentification sont morts, et
    reutiliser l'ancien client echoue silencieusement.
    """

    def __init__(
        self,
        player_factory: Callable[[], HeuristicPlayer],
        policy: Optional[RetryPolicy] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.player_factory = player_factory
        self.policy = policy or RetryPolicy()
        self.sleep = sleep
        self.attempts = 0
        self.reconnections = 0
        self.total_finished = 0

    async def run(
        self,
        task: Callable[[HeuristicPlayer, int], Awaitable[None]],
        target_battles: int,
    ) -> int:
        """Execute ``task`` jusqu'a ``target_battles`` combats termines.

        Renvoie le nombre de combats effectivement joues. ``task`` recoit le
        joueur et le nombre de combats RESTANTS, pour qu'une reprise ne reparte
        pas de zero.
        """
        completed = 0
        attempt = 0

        while completed < target_battles and attempt < self.policy.max_attempts:
            attempt += 1
            self.attempts = attempt
            player = self.player_factory()
            remaining = target_battles - completed

            try:
                await task(player, remaining)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOGGER.warning(
                    "Connexion perdue (tentative %s/%s): %s: %s",
                    attempt, self.policy.max_attempts, type(error).__name__, error,
                )
            finally:
                done = getattr(player, "n_finished_battles", 0) or 0
                completed += done
                self.total_finished = completed
                await self._shutdown(player)

            if completed >= target_battles:
                break

            self.reconnections += 1
            delay = self.policy.delay_for(attempt)
            LOGGER.info(
                "%s/%s combats joues. Reconnexion dans %.1fs.",
                completed, target_battles, delay,
            )
            await self.sleep(delay)

        if completed < target_battles:
            LOGGER.error(
                "Abandon apres %s tentatives: %s/%s combats joues.",
                attempt, completed, target_battles,
            )
        return completed

    @staticmethod
    async def _shutdown(player) -> None:
        """Ferme proprement le websocket; une session morte n'est pas une erreur."""
        try:
            await player.ps_client.stop_listening()
        except Exception:  # pragma: no cover - la session est deja perdue
            pass


def build_player(
    settings: Settings,
    config: HeuristicConfig,
    on_battle_end=None,
    account: Optional[AccountConfiguration] = None,
    log_level: int = logging.WARNING,
) -> HeuristicPlayer:
    """Fabrique un joueur configure pour le serveur cible.

    ``log_level`` est a WARNING par defaut: poke-env journalise chaque message du
    protocole en INFO, ce qui noie completement la sortie utile sur l'ecran d'un
    telephone. On remonte a DEBUG avec l'option -v du CLI.
    """
    return HeuristicPlayer(
        config,
        account_configuration=account if account is not None else account_configuration(settings),
        server_configuration=server_configuration(settings),
        battle_format=settings.battle_format,
        max_concurrent_battles=1,
        start_timer_on_battle_start=(settings.server == "online"),
        log_level=log_level,
        on_battle_end=on_battle_end,
        server_label=settings.server,
        open_spectator=settings.termux_open_spectator,
    )
