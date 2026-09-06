"""Integration Termux, entierement optionnelle.

Rien ici n'est requis pour jouer: si Termux:API n'est pas installe, ou si on
tourne ailleurs que sur Android, chaque fonction devient un no-op silencieux.
"""

import logging
import shutil
import subprocess
LOGGER = logging.getLogger("showdownbot.termux")

_wake_lock_held = False


def _run(command: list) -> bool:
    binary = shutil.which(command[0])
    if binary is None:
        return False
    try:
        subprocess.run(command, check=False, capture_output=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def acquire_wake_lock() -> bool:
    """Empeche Android d'endormir le CPU pendant une session de jeu.

    Indispensable ecran eteint: sans wake-lock, Doze suspend le processus et le
    websocket meurt au bout de quelques minutes. A completer cote systeme par une
    exemption d'optimisation de batterie pour Termux (voir docs/termux.md).
    """
    global _wake_lock_held
    if _run(["termux-wake-lock"]):
        _wake_lock_held = True
        LOGGER.info("Wake-lock Termux acquis.")
        return True
    LOGGER.debug("termux-wake-lock indisponible, ignore.")
    return False


def release_wake_lock() -> bool:
    global _wake_lock_held
    if not _wake_lock_held:
        return False
    _wake_lock_held = False
    return _run(["termux-wake-unlock"])


def notify(title: str, content: str, notification_id: str = "showdownbot") -> bool:
    """Notification Android de fin de combat. Necessite `pkg install termux-api`."""
    return _run(
        [
            "termux-notification",
            "--id", notification_id,
            "--title", title,
            "--content", content,
        ]
    )


def notify_battle_result(log, enabled: bool = True) -> None:
    if not enabled:
        return
    result = (log.result or "termine").capitalize()
    notify(
        f"Showdown: {result}",
        f"{log.turn_count} tours - {len(log.my_survivors)} survivants",
    )
