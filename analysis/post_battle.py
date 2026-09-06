"""Analyse post-combat: UN appel API, une fois le combat termine.

Le combat est deja fini et deja logge quand cette fonction s'execute. Elle ne
peut donc pas influencer une decision de jeu, par construction.

Produit:
  - memory/battles/<slug>.md   le rapport lisible
  - les findings, reinjectes dans le JSON du combat pour le tuner
  - une ligne dans memory/api_usage.jsonl (cout reel de l'appel)
"""

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from analysis.client import AnthropicAnalyst, record_usage
from analysis.prompts import build_findings_schema, build_system_prompt, distill_battle
from bot.config import BATTLES_DIR, UNMAPPED_PATH, load_taxonomy, load_tuning_rules

LOGGER = logging.getLogger("showdownbot.analysis")


def markdown_path_for(battle_path: str) -> str:
    return os.path.splitext(battle_path)[0] + ".md"


def _write_report(battle_path: str, log: Dict[str, Any], result) -> str:
    """Ecrit le .md, en-tete factuel puis rapport du modele."""
    findings = result.data.get("findings", [])
    header = [
        f"# {log.get('battle_tag')}",
        "",
        f"- **Resultat**: {log.get('result')} en {log.get('turn_count')} tours",
        f"- **Date**: {log.get('started_at')}",
        f"- **Serveur**: {log.get('server')}",
        f"- **Config moteur**: version {log.get('config_version')}",
        f"- **Mon equipe**: {', '.join(log.get('my_team') or [])}",
        f"- **Equipe adverse**: {', '.join(log.get('opponent_team') or [])}",
        f"- **Cout de cette analyse**: ${result.cost_usd:.4f} "
        f"({result.input_tokens} tokens entree, {result.output_tokens} sortie, "
        f"modele {result.model})",
        "",
        f"> {result.data.get('summary', '')}",
        "",
        "---",
        "",
        result.data.get("markdown_report", ""),
        "",
        "---",
        "",
        "## Observations classees",
        "",
        "Ces tags alimentent le reglage automatique des heuristiques.",
        "",
    ]
    if findings:
        header.append("| Tag | Severite | Tours | Note |")
        header.append("| --- | --- | --- | --- |")
        for finding in findings:
            turns = ", ".join(str(t) for t in finding.get("turns", []))
            note = str(finding.get("note", "")).replace("|", "/")
            header.append(
                f"| `{finding['tag']}` | {finding.get('severity')} | {turns} | {note} |"
            )
    else:
        header.append("_Aucune erreur systematique identifiee sur ce combat._")

    path = markdown_path_for(battle_path)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(header) + "\n")
    return path


def _record_unmapped(findings: List[Dict[str, Any]]) -> List[str]:
    """Signale les tags valides mais sans regle d'ajustement.

    L'enum du schema empeche un tag inconnu. Le cas restant est un tag du
    vocabulaire qui n'a pas d'entree dans config/tuning_rules.json: il sera
    visible dans le rapport mais ne bougera aucun parametre. On le note pour
    qu'une regle soit ajoutee a la main, plutot que de deviner.
    """
    rules = load_tuning_rules()["rules"]
    taxonomy = load_taxonomy()
    unmapped = sorted(
        {
            f["tag"]
            for f in findings
            if f["tag"] not in rules and taxonomy.get(f["tag"], {}).get("polarity") == "negative"
        }
    )
    if unmapped:
        os.makedirs(os.path.dirname(UNMAPPED_PATH), exist_ok=True)
        with open(UNMAPPED_PATH, "a", encoding="utf-8") as handle:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for tag in unmapped:
                handle.write(f"- {stamp} `{tag}` observe mais sans regle dans tuning_rules.json\n")
    return unmapped


def analyse_battle(
    battle_path: str,
    api_key: str,
    model: str = "claude-sonnet-5",
    effort: str = "high",
    analyst: Optional[AnthropicAnalyst] = None,
) -> Dict[str, Any]:
    """Analyse un combat deja joue. UN seul appel API.

    Renvoie un dict {markdown_path, findings, cost_usd}.
    """
    with open(battle_path, "r", encoding="utf-8") as handle:
        log = json.load(handle)

    if log.get("analysis"):
        LOGGER.info("Combat deja analyse, ignore: %s", battle_path)
        return {
            "markdown_path": markdown_path_for(battle_path),
            "findings": log["analysis"].get("findings", []),
            "cost_usd": 0.0,
            "skipped": True,
        }

    taxonomy = load_taxonomy()
    analyst = analyst or AnthropicAnalyst(api_key=api_key, effort=effort)

    result = analyst.structured_call(
        model=model,
        system=build_system_prompt(taxonomy),
        user_content=distill_battle(log),
        json_schema=build_findings_schema(taxonomy),
    )

    findings = result.data.get("findings", [])
    unmapped = _record_unmapped(findings)

    log["analysis"] = {
        "analysed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": result.model,
        "summary": result.data.get("summary", ""),
        "findings": findings,
        "cost_usd": round(result.cost_usd, 6),
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "unmapped_tags": unmapped,
    }
    with open(battle_path, "w", encoding="utf-8") as handle:
        json.dump(log, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    markdown_path = _write_report(battle_path, log, result)

    record_usage(
        "post_battle",
        result,
        {"battle_tag": log.get("battle_tag"), "findings": len(findings)},
    )

    LOGGER.info(
        "Analyse de %s: %s observations, $%.4f",
        log.get("battle_tag"), len(findings), result.cost_usd,
    )
    return {
        "markdown_path": markdown_path,
        "findings": findings,
        "cost_usd": result.cost_usd,
        "skipped": False,
    }


def pending_battles(directory: str = BATTLES_DIR) -> List[str]:
    """Combats logges mais pas encore analyses.

    Permet de jouer sans cle API puis de rattraper les analyses plus tard.
    """
    from bot.battle_log import list_logs

    pending = []
    for path in list_logs(directory):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                log = json.load(handle)
        except (OSError, json.JSONDecodeError):
            continue
        if not log.get("analysis"):
            pending.append(path)
    return pending
