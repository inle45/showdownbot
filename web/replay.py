"""Genere un replay anime a partir du protocole d'un combat deja logge.

Reutilise le meme moteur de rendu que le site officiel Showdown
(poke_env.data.replay_template): la page produite embarque le protocole brut
du combat et charge le script d'animation depuis play.pokemonshowdown.com (le
telephone a besoin d'une connexion pour l'affichage, comme pour tout le reste
du projet). Fonctionne pour tous les combats deja joues, local comme officiel:
tout ce dont ce module a besoin (``protocol``) est deja dans chaque log JSON,
rien a rejouer.
"""

from typing import Any, Dict, Optional

from poke_env.data.replay_template import REPLAY_TEMPLATE


def build_replay_html(
    log: Dict[str, Any], player_username: Optional[str] = None
) -> Optional[str]:
    """Rend le HTML du replay, ou None si ce combat n'a pas de protocole stocke.

    Les combats loggues avant l'ajout de la capture du protocole (ou dont la
    capture a echoue) n'ont pas ce champ: on prefere le dire clairement plutot
    que d'afficher une page vide qui ressemblerait a un bug.
    """
    protocol = log.get("protocol") or []
    if not protocol:
        return None

    battle_tag = log.get("battle_tag", "")
    replay_log = f">{battle_tag}\n" + "\n".join(protocol)

    html = REPLAY_TEMPLATE
    html = html.replace("{BATTLE_TAG}", battle_tag)
    html = html.replace("{PLAYER_USERNAME}", player_username or "Bot")
    html = html.replace("{OPPONENT_USERNAME}", log.get("opponent_username") or "Adversaire")
    html = html.replace("{REPLAY_LOG}", replay_log)
    return html
