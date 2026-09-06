"""Point d'entree unique du bot.

    python -m bot.cli selfplay --battles 5        bot contre bot, serveur local
    python -m bot.cli challenge <pseudo>          defier un utilisateur
    python -m bot.cli accept --battles 3          accepter les defis recus
    python -m bot.cli ladder --battles 10         ladder public (opt-in explicite)
    python -m bot.cli analyse [--all]             rattraper les analyses en attente
    python -m bot.cli consolidate                 mettre a jour la fiche de lecons
    python -m bot.cli tune [--apply]              proposer / appliquer les reglages
    python -m bot.cli status                      etat du bot, cout API, config
"""

import argparse
import asyncio
import logging
import sys
from typing import List, Optional

from bot.config import ConfigError, load_config, load_settings
from bot.connection import ConnectionSupervisor, RetryPolicy, build_player
from bot import termux

LOGGER = logging.getLogger("showdownbot")


def configure_logging(verbose: bool = False) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("poke-env").setLevel(logging.WARNING)


def player_log_level(verbose: bool) -> int:
    """Niveau des loggers de poke-env, qui tracent tout le protocole en INFO."""
    return logging.DEBUG if verbose else logging.WARNING


def make_battle_end_handler(settings):
    """Traitement de fin de combat: analyse, consolidation, reglage.

    Tout se produit APRES le combat. Une erreur ici ne peut pas affecter une
    partie en cours, et n'interrompt jamais la session de jeu.
    """
    from bot.battle_log import list_logs

    def handler(log):
        termux.notify_battle_result(log, settings.termux_notifications)

        if not settings.analysis_enabled:
            LOGGER.info(
                "Analyse desactivee (pas de cle API). Le combat est logge, "
                "rattrapable avec: python -m bot.cli analyse"
            )
            return

        from analysis.client import AnalysisUnavailable
        from analysis.post_battle import analyse_battle

        try:
            outcome = analyse_battle(
                log.path(),
                api_key=settings.anthropic_api_key,
                model=settings.analysis_model,
                effort=settings.analysis_effort,
                thinking=settings.analysis_thinking,
            )
            LOGGER.info(
                "Analyse ecrite: %s (%s observations, $%.4f)",
                outcome["markdown_path"], len(outcome["findings"]), outcome["cost_usd"],
            )
        except AnalysisUnavailable as error:
            LOGGER.warning("Analyse indisponible: %s", error)
            return
        except Exception:
            LOGGER.exception("L'analyse post-combat a echoue (le combat reste logge)")
            return

        finished = len(list_logs())
        if settings.consolidate_every and finished % settings.consolidate_every == 0:
            _consolidate_and_tune(settings)

    return handler


def _consolidate_and_tune(settings) -> None:
    from analysis.client import AnalysisUnavailable
    from analysis.consolidate import consolidate
    from analysis.tuner import apply_proposal, check_rollback, propose, save_pending

    try:
        summary = consolidate(
            api_key=settings.anthropic_api_key,
            window=settings.consolidate_every,
            model=settings.consolidation_model,
            effort=settings.analysis_effort,
            thinking=settings.analysis_thinking,
        )
        LOGGER.info("Fiche de lecons mise a jour: %s", summary["headline"])
    except AnalysisUnavailable as error:
        LOGGER.warning("Consolidation impossible: %s", error)
    except Exception:
        LOGGER.exception("La consolidation a echoue")

    # Le rollback passe avant toute nouvelle proposition: inutile d'empiler un
    # changement sur un changement qui vient de degrader le winrate.
    try:
        verdict = check_rollback()
        if verdict.get("rolled_back"):
            LOGGER.warning("Reglages annules: %s", "; ".join(verdict["reverted"]))
            return
    except Exception:
        LOGGER.exception("La verification de rollback a echoue")

    try:
        proposal = propose()
    except Exception:
        LOGGER.exception("Le calcul des ajustements a echoue")
        return

    if proposal.is_empty:
        LOGGER.info("Aucun ajustement de parametre propose sur cette fenetre.")
        return

    if settings.tuning_mode == "auto":
        result = apply_proposal(proposal)
        LOGGER.info("Reglages appliques (v%s): %s", result["version"], "; ".join(result["changes"]))
    else:
        path = save_pending(proposal)
        LOGGER.info(
            "%s ajustement(s) en attente de ton approbation (%s). "
            "Valider avec: python -m bot.cli tune --apply",
            len(proposal.changes), path,
        )


# --------------------------------------------------------------- commandes jeu


def _run_session(settings, config, task, target_battles: int, label: str,
                 log_level: int = logging.WARNING) -> int:
    handler = make_battle_end_handler(settings)
    termux.acquire_wake_lock()
    try:
        supervisor = ConnectionSupervisor(
            player_factory=lambda: build_player(
                settings, config, on_battle_end=handler, log_level=log_level
            ),
            policy=RetryPolicy(),
        )
        played = asyncio.run(supervisor.run(task, target_battles))
        LOGGER.info("%s termine: %s/%s combats.", label, played, target_battles)
        if supervisor.reconnections:
            LOGGER.info("Reconnexions durant la session: %s", supervisor.reconnections)
        return played
    finally:
        termux.release_wake_lock()


# Adversaires de reference, tous fournis par poke-env. Comparer le moteur a une
# reference fixe est le seul moyen d'evaluer un changement de reglage sans
# depenser d'appels API ni de parties de ladder.
BASELINES = {
    "random": ("bot.player", "RandomBaselinePlayer", "coups au hasard"),
    "maxpower": ("poke_env.player", "MaxBasePowerPlayer", "toujours la puissance brute maximale"),
    "heuristic": ("poke_env.player", "SimpleHeuristicsPlayer", "heuristiques de reference de poke-env"),
}


def cmd_selfplay(args, settings, config) -> int:
    """Bot contre un adversaire de reference, sur le serveur local."""
    import importlib

    from bot.connection import server_configuration

    module_name, class_name, description = BASELINES[args.opponent]
    opponent_class = getattr(importlib.import_module(module_name), class_name)

    handler = make_battle_end_handler(settings)
    level = player_log_level(args.verbose)
    LOGGER.info("Adversaire: %s (%s)", args.opponent, description)

    async def run():
        player = build_player(settings, config, on_battle_end=handler, log_level=level)
        opponent = opponent_class(
            server_configuration=server_configuration(settings),
            battle_format=settings.battle_format,
            max_concurrent_battles=1,
            log_level=level,
        )
        await player.battle_against(opponent, n_battles=args.battles)
        won, total = player.n_won_battles, player.n_finished_battles
        LOGGER.info(
            "Bilan contre %s: %s victoires / %s combats (%.0f%%)",
            args.opponent, won, total, 100 * won / total if total else 0,
        )
        return total

    termux.acquire_wake_lock()
    try:
        return asyncio.run(run())
    finally:
        termux.release_wake_lock()


def cmd_challenge(args, settings, config) -> int:
    async def task(player, remaining):
        await player.send_challenges(args.opponent, n_challenges=remaining)

    return _run_session(
        settings, config, task, args.battles, f"Defis a {args.opponent}",
        log_level=player_log_level(args.verbose),
    )


def cmd_accept(args, settings, config) -> int:
    async def task(player, remaining):
        await player.accept_challenges(args.opponent, remaining)

    return _run_session(
        settings, config, task, args.battles, "Defis acceptes",
        log_level=player_log_level(args.verbose),
    )


def cmd_ladder(args, settings, config) -> int:
    """Ladder public. Volontairement derriere un opt-in explicite."""
    if not settings.enable_ladder and not args.force:
        print(
            "Le ladder public est desactive.\n"
            "Pour l'activer: ENABLE_LADDER=true dans .env, ou --force.\n"
            "\n"
            "A savoir avant: jouer en ladder avec un bot est une zone grise du\n"
            "reglement Showdown. Utilise un compte dedie, un seul, et n'enchaine\n"
            "pas les parties sans surveillance.",
            file=sys.stderr,
        )
        return 1

    if settings.server != "online":
        LOGGER.warning("SHOWDOWN_SERVER=%s: le ladder n'a de sens qu'en 'online'.", settings.server)

    async def task(player, remaining):
        await player.ladder(remaining)

    return _run_session(
        settings, config, task, args.battles, "Ladder",
        log_level=player_log_level(args.verbose),
    )


# ----------------------------------------------------------- commandes analyse


def cmd_analyse(args, settings, config) -> int:
    from analysis.client import AnalysisUnavailable
    from analysis.post_battle import analyse_battle, pending_battles

    pending = pending_battles()
    if not pending:
        print("Aucun combat en attente d'analyse.")
        return 0

    targets = pending if args.all else pending[: args.limit]
    print(f"{len(pending)} combat(s) en attente, {len(targets)} a analyser.")

    total = 0.0
    for path in targets:
        try:
            outcome = analyse_battle(
                path,
                api_key=settings.anthropic_api_key,
                model=settings.analysis_model,
                effort=settings.analysis_effort,
                thinking=settings.analysis_thinking,
            )
        except AnalysisUnavailable as error:
            print(f"Analyse impossible: {error}", file=sys.stderr)
            return 1
        total += outcome["cost_usd"]
        print(
            f"  {path} -> {outcome['markdown_path']} "
            f"({len(outcome['findings'])} observations, ${outcome['cost_usd']:.4f})"
        )
    print(f"Cout total: ${total:.4f}")
    return 0


def cmd_consolidate(args, settings, config) -> int:
    from analysis.client import AnalysisUnavailable
    from analysis.consolidate import consolidate

    try:
        summary = consolidate(
            api_key=settings.anthropic_api_key,
            window=args.window or settings.consolidate_every,
            model=settings.consolidation_model,
            effort=settings.analysis_effort,
            thinking=settings.analysis_thinking,
        )
    except AnalysisUnavailable as error:
        print(f"Consolidation impossible: {error}", file=sys.stderr)
        return 1
    print(f"Fiche mise a jour: {summary['path']}")
    print(f"Defaut principal: {summary['headline']}")
    print(f"Cout: ${summary['cost_usd']:.4f}")
    return 0


def cmd_tune(args, settings, config) -> int:
    """Propose ou applique les ajustements. Aucun appel API."""
    from analysis.tuner import apply_proposal, check_rollback, load_pending, propose, save_pending

    if args.check_rollback:
        verdict = check_rollback(config=config)
        print(json_like(verdict))
        return 0

    if args.apply and not args.recompute:
        pending = load_pending()
        if pending is None:
            print("Aucune proposition en attente. Lance d'abord: python -m bot.cli tune")
            return 1
        proposal = propose(config=config, window=args.window)
    else:
        proposal = propose(config=config, window=args.window)

    if proposal.is_empty:
        print(f"Fenetre de {proposal.window_battles} combats (winrate {proposal.winrate:.0%}).")
        print("Aucun ajustement propose.")
        if proposal.ignored:
            print("\nObservations ecartees:")
            for reason in proposal.ignored:
                print(f"  - {reason}")
        return 0

    print(
        f"Fenetre de {proposal.window_battles} combats "
        f"(winrate {proposal.winrate:.0%}), config v{proposal.from_version}.\n"
    )
    print("Ajustements proposes:")
    for change in proposal.changes:
        print(f"  - {change.describe()}")
        if change.rationale:
            print(f"      raison: {change.rationale}")
    if proposal.net_scores:
        print("\nSeverites nettes retenues:")
        for tag, score in sorted(proposal.net_scores.items(), key=lambda i: -i[1]):
            print(f"  {tag}: {score:.1f}")
    if proposal.ignored:
        print("\nObservations ecartees:")
        for reason in proposal.ignored:
            print(f"  - {reason}")

    if args.apply:
        result = apply_proposal(proposal, config=config)
        print(f"\nApplique. Nouvelle version: v{result['version']} ({result['snapshot']})")
    else:
        path = save_pending(proposal)
        print(f"\nProposition enregistree: {path}")
        print("Pour appliquer: python -m bot.cli tune --apply")
    return 0


def cmd_status(args, settings, config) -> int:
    from analysis.client import load_usage
    from bot.battle_log import load_all
    from analysis.tuner import load_pending

    logs = load_all()
    decided = [log for log in logs if log.get("won") is not None]
    wins = sum(1 for log in decided if log["won"])
    analysed = sum(1 for log in logs if log.get("analysis"))
    usage = load_usage()
    cost = sum(entry.get("cost_usd", 0.0) for entry in usage)

    print(f"Serveur          : {settings.server} ({settings.battle_format})")
    print(f"Compte           : {settings.username or '(anonyme, serveur local)'}")
    print(f"Ladder           : {'active' if settings.enable_ladder else 'desactive'}")
    print(f"Analyse          : {'active' if settings.analysis_enabled else 'desactivee (pas de cle)'}")
    print(f"Modele analyse   : {settings.analysis_model}")
    print(f"Mode de reglage  : {settings.tuning_mode}")
    print()
    print(f"Combats joues    : {len(logs)} ({wins} victoires, "
          f"winrate {(wins / len(decided) if decided else 0):.0%})")
    print(f"Combats analyses : {analysed}/{len(logs)}")
    print(f"Config heuristique: v{config.config_version} ({len(config.params)} parametres)")
    print()
    print(f"Appels API       : {len(usage)}")
    print(f"Cout cumule      : ${cost:.4f}")
    if logs:
        print(f"Cout par combat  : ${cost / len(logs):.4f}")

    pending = load_pending()
    if pending:
        print()
        print(f"{len(pending['changes'])} ajustement(s) en attente d'approbation:")
        for change in pending["changes"]:
            print(f"  - {change['description']}")
    return 0


def json_like(payload) -> str:
    import json

    return json.dumps(payload, indent=2, ensure_ascii=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bot.cli", description="Bot Pokemon Showdown heuristique auto-ameliorant."
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="journalisation detaillee")
    sub = parser.add_subparsers(dest="command", required=True)

    selfplay = sub.add_parser(
        "selfplay", help="bot contre un adversaire de reference (serveur local)"
    )
    selfplay.add_argument("--battles", type=int, default=1)
    selfplay.add_argument(
        "--opponent", choices=sorted(BASELINES), default="heuristic",
        help="adversaire de reference (defaut: heuristic, le plus exigeant)",
    )
    selfplay.set_defaults(func=cmd_selfplay)

    challenge = sub.add_parser("challenge", help="defier un utilisateur")
    challenge.add_argument("opponent")
    challenge.add_argument("--battles", type=int, default=1)
    challenge.set_defaults(func=cmd_challenge)

    accept = sub.add_parser("accept", help="accepter les defis recus")
    accept.add_argument("--opponent", default=None, help="limiter a un adversaire")
    accept.add_argument("--battles", type=int, default=1)
    accept.set_defaults(func=cmd_accept)

    ladder = sub.add_parser("ladder", help="ladder public (necessite un opt-in explicite)")
    ladder.add_argument("--battles", type=int, default=1)
    ladder.add_argument("--force", action="store_true", help="passer outre ENABLE_LADDER")
    ladder.set_defaults(func=cmd_ladder)

    analyse = sub.add_parser("analyse", help="analyser les combats en attente")
    analyse.add_argument("--all", action="store_true", help="tout analyser")
    analyse.add_argument("--limit", type=int, default=1)
    analyse.set_defaults(func=cmd_analyse)

    consolidate = sub.add_parser("consolidate", help="mettre a jour la fiche de lecons")
    consolidate.add_argument("--window", type=int, default=None)
    consolidate.set_defaults(func=cmd_consolidate)

    tune = sub.add_parser("tune", help="proposer ou appliquer les ajustements (sans API)")
    tune.add_argument("--apply", action="store_true", help="appliquer la proposition")
    tune.add_argument("--recompute", action="store_true", help="recalculer avant d'appliquer")
    tune.add_argument("--window", type=int, default=None)
    tune.add_argument("--check-rollback", action="store_true", help="verifier une degradation")
    tune.set_defaults(func=cmd_tune)

    status = sub.add_parser("status", help="etat du bot, cout API, config")
    status.set_defaults(func=cmd_status)

    return parser


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging(args.verbose)
    settings = load_settings()
    try:
        config = load_config()
    except ConfigError as error:
        print(str(error), file=sys.stderr)
        print(
            "\nLe moteur refuse de demarrer sur une config invalide. "
            "Restaure une version depuis config/history/.",
            file=sys.stderr,
        )
        return 2
    return args.func(args, settings, config) or 0


if __name__ == "__main__":
    sys.exit(main())
