"""Client Anthropic partage par les deux seuls appels API du projet.

Il n'y a QUE deux appels dans tout le systeme:
  1. analysis/post_battle.py  - une fois, apres chaque combat;
  2. analysis/consolidate.py  - une fois tous les N combats.

Rien d'autre n'appelle l'API, et surtout rien pendant un combat. Ce module est
le seul point de passage, ce qui rend cette contrainte verifiable d'un coup
d'oeil (et mesurable: chaque appel est journalise dans memory/api_usage.jsonl).
"""

import json
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from bot.config import USAGE_PATH

# Tarifs publics en dollars par million de tokens, pour estimer le cout reel.
# Le cout affiche dans le dashboard vient de l'usage reellement retourne par
# l'API, pas d'une estimation a priori.
PRICING = {
    "claude-opus-5": {"input": 5.00, "output": 25.00},
    "claude-sonnet-5": {"input": 2.00, "output": 10.00},
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00},
}

# Les lectures de cache sont facturees environ 10% du tarif d'entree, et les
# ecritures environ 125%.
CACHE_READ_RATE = 0.10
CACHE_WRITE_RATE = 1.25


class AnalysisUnavailable(RuntimeError):
    """Le SDK Anthropic n'est pas installe, ou aucune cle n'est configuree."""


@dataclass
class CallResult:
    """Reponse d'un appel, avec son cout mesure."""

    data: Dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    duration_s: float = 0.0
    request_id: Optional[str] = None


def estimate_cost(model: str, usage) -> float:
    prices = PRICING.get(model)
    if prices is None:
        return 0.0
    read = getattr(usage, "cache_read_input_tokens", 0) or 0
    write = getattr(usage, "cache_creation_input_tokens", 0) or 0
    plain = getattr(usage, "input_tokens", 0) or 0
    output = getattr(usage, "output_tokens", 0) or 0
    cost = (
        plain * prices["input"]
        + read * prices["input"] * CACHE_READ_RATE
        + write * prices["input"] * CACHE_WRITE_RATE
        + output * prices["output"]
    )
    return cost / 1_000_000


def record_usage(kind: str, result: CallResult, extra: Optional[Dict[str, Any]] = None) -> None:
    """Ajoute une ligne a memory/api_usage.jsonl.

    Permet au dashboard d'afficher un cout REEL par combat, plutot qu'une
    estimation, et de verifier qu'il n'y a bien qu'un appel par combat.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "kind": kind,
        "model": result.model,
        "input_tokens": result.input_tokens,
        "output_tokens": result.output_tokens,
        "cache_read_tokens": result.cache_read_tokens,
        "cache_write_tokens": result.cache_write_tokens,
        "cost_usd": round(result.cost_usd, 6),
        "duration_s": round(result.duration_s, 2),
        "request_id": result.request_id,
    }
    if extra:
        entry.update(extra)
    os.makedirs(os.path.dirname(USAGE_PATH), exist_ok=True)
    with open(USAGE_PATH, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_usage(path: str = USAGE_PATH) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return entries


class AnthropicAnalyst:
    """Enveloppe minimale autour du SDK officiel.

    Le SDK est importe paresseusement: sur Termux, la couche LLM est optionnelle
    et le bot doit demarrer meme sans elle.
    """

    def __init__(self, api_key: str = "", effort: str = "high"):
        self.api_key = api_key
        self.effort = effort
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as error:
                raise AnalysisUnavailable(
                    "Le SDK anthropic n'est pas installe. Sur Termux: "
                    "bash scripts/termux_setup.sh --llm"
                ) from error
            if not self.api_key:
                raise AnalysisUnavailable(
                    "ANTHROPIC_API_KEY absent du .env: l'analyse est desactivee."
                )
            self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    def structured_call(
        self,
        model: str,
        system: str,
        user_content: str,
        json_schema: Dict[str, Any],
        max_tokens: int = 24000,
    ) -> CallResult:
        """Un appel, une reponse JSON conforme au schema.

        ``output_config.format`` fait valider le schema par l'API elle-meme: les
        tags renvoyes ne peuvent PAS sortir du vocabulaire ferme, sans parsing
        fragile de notre cote. C'est le pilier du systeme de tuning.

        Le raisonnement adaptatif (``thinking``) partage le meme budget que la
        reponse finale: sur un effort eleve, il peut a lui seul consommer tout
        ``max_tokens`` et laisser la reponse tronquee, sans bloc de texte. D'ou
        une marge large, et l'appel en streaming plutot qu'un appel bloquant -
        indispensable des que ``max_tokens`` depasse ~16000, pour ne pas risquer
        un timeout HTTP en cours de generation sur une connexion lente.
        """
        started = time.monotonic()

        output_config: Dict[str, Any] = {
            "format": {"type": "json_schema", "schema": json_schema}
        }
        if self.effort:
            output_config["effort"] = self.effort

        with self.client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            # Le prompt systeme (consignes + taxonomie) est identique d'un appel
            # a l'autre: on le met en cache pour ne pas le repayer a chaque combat.
            system=[
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ],
            messages=[{"role": "user", "content": user_content}],
            thinking={"type": "adaptive"},
            output_config=output_config,
        ) as stream:
            response = stream.get_final_message()

        duration = time.monotonic() - started

        if response.stop_reason == "refusal":
            raise AnalysisUnavailable(
                "L'API a refuse la requete (stop_reason=refusal): "
                f"{getattr(response.stop_details, 'explanation', '')}"
            )

        if response.stop_reason == "max_tokens":
            raise AnalysisUnavailable(
                f"Reponse tronquee: le budget de {max_tokens} tokens a ete "
                "entierement consomme (raisonnement inclus) avant la reponse "
                "finale. Reessaie - le raisonnement adaptatif varie d'un appel "
                "a l'autre - ou reduis ANALYSIS_EFFORT dans .env (high -> medium)."
            )

        text = next((block.text for block in response.content if block.type == "text"), None)
        if text is None:
            block_types = [block.type for block in response.content]
            raise AnalysisUnavailable(
                f"Reponse sans bloc texte exploitable (stop_reason={response.stop_reason}, "
                f"blocs recus: {block_types})."
            )

        usage = response.usage
        result = CallResult(
            data=json.loads(text),
            model=model,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            duration_s=duration,
            request_id=getattr(response, "_request_id", None),
        )
        result.cost_usd = estimate_cost(model, usage)
        return result
