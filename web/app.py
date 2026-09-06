"""Dashboard local, servi depuis Termux et consulte depuis le navigateur du
telephone.

    python -m web.app          puis http://127.0.0.1:5000

Pense mobile d'abord: une colonne, gros boutons, tableaux qui defilent
horizontalement plutot que de deborder. N'ecoute que sur la boucle locale par
defaut, pour ne rien exposer sur le reseau wifi.
"""

import json
import os
from typing import Any, Dict, List, Optional

from flask import Flask, abort, flash, redirect, render_template, url_for

import bot  # applique le shim orjson  # noqa: F401
from analysis.client import load_usage
from analysis.tuner import apply_proposal, clear_pending, load_pending, propose, save_pending
from bot.battle_log import load_all
from bot.config import HISTORY_DIR, LESSONS_PATH, load_config, load_settings, load_taxonomy
from web.markdown import render as render_markdown
from web.stats import finding_frequency, rolling_winrate, sparkline_svg, summarise

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "showdownbot-local")


def _battles(newest_first: bool = True) -> List[Dict[str, Any]]:
    logs = load_all()
    return list(reversed(logs)) if newest_first else logs


def _find_battle(slug: str) -> Optional[Dict[str, Any]]:
    for log in load_all():
        if os.path.splitext(os.path.basename(log["_path"]))[0] == slug:
            return log
    return None


@app.template_filter("pct")
def _pct(value: float) -> str:
    return f"{value:.0%}"


@app.template_filter("money")
def _money(value: float) -> str:
    return f"${value:.4f}"


@app.route("/")
def dashboard():
    logs = load_all()
    usage = load_usage()
    stats = summarise(logs, usage)
    settings = load_settings()
    config = load_config()

    return render_template(
        "dashboard.html",
        stats=stats,
        settings=settings,
        config=config,
        sparkline=sparkline_svg(rolling_winrate(logs)),
        findings=finding_frequency(logs)[:8],
        taxonomy=load_taxonomy(),
        recent=list(reversed(logs))[:5],
        pending=load_pending(),
        lessons_exists=os.path.exists(LESSONS_PATH),
    )


@app.route("/battles")
def battles():
    return render_template("battles.html", battles=_battles())


@app.route("/battle/<slug>")
def battle(slug: str):
    log = _find_battle(slug)
    if log is None:
        abort(404)
    markdown_path = os.path.splitext(log["_path"])[0] + ".md"
    report = ""
    if os.path.exists(markdown_path):
        with open(markdown_path, "r", encoding="utf-8") as handle:
            report = handle.read()
    return render_template(
        "battle.html",
        log=log,
        slug=slug,
        report=render_markdown(report),
        has_report=bool(report),
        turns=log.get("turns", []),
    )


@app.route("/lessons")
def lessons():
    content = ""
    if os.path.exists(LESSONS_PATH):
        with open(LESSONS_PATH, "r", encoding="utf-8") as handle:
            content = handle.read()
    return render_template(
        "lessons.html", lessons=render_markdown(content), exists=bool(content)
    )


@app.route("/config")
def configuration():
    config = load_config()
    history = []
    if os.path.isdir(HISTORY_DIR):
        for name in sorted(os.listdir(HISTORY_DIR), reverse=True):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(HISTORY_DIR, name), "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, json.JSONDecodeError):
                continue
            history.append({"name": name, "payload": payload})
    return render_template(
        "config.html",
        config=config,
        params=sorted(config.params.items()),
        history=history,
        pending=load_pending(),
    )


@app.route("/tune/recompute", methods=["POST"])
def tune_recompute():
    """Recalcule une proposition. Aucun appel API: pure agregation locale."""
    proposal = propose()
    if proposal.is_empty:
        clear_pending()
        flash("Aucun ajustement propose sur la fenetre courante.", "info")
    else:
        save_pending(proposal)
        flash(f"{len(proposal.changes)} ajustement(s) proposes.", "info")
    return redirect(url_for("configuration"))


@app.route("/tune/apply", methods=["POST"])
def tune_apply():
    """Applique la proposition en attente, apres approbation explicite."""
    if load_pending() is None:
        flash("Aucune proposition en attente.", "error")
        return redirect(url_for("configuration"))
    proposal = propose()
    if proposal.is_empty:
        clear_pending()
        flash("La proposition n'est plus d'actualite, elle a ete annulee.", "error")
        return redirect(url_for("configuration"))
    result = apply_proposal(proposal)
    flash(
        f"Applique en v{result['version']}: " + " ; ".join(result["changes"]),
        "success",
    )
    return redirect(url_for("configuration"))


@app.route("/tune/reject", methods=["POST"])
def tune_reject():
    clear_pending()
    flash("Proposition rejetee. Les reglages restent inchanges.", "info")
    return redirect(url_for("configuration"))


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="Dashboard local du bot Showdown.")
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="127.0.0.1 par defaut. Ne mettre 0.0.0.0 que sur un reseau de confiance.",
    )
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()

    print(f"Dashboard: http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
