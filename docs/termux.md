# Faire tourner le bot sur Termux

Notes specifiques a Android. Le reste est dans le [README](../README.md).

## Pourquoi ce n'est pas un `pip install` ordinaire

Termux utilise la libc d'Android (bionic), pas glibc. Son pip **refuse les
wheels manylinux**, qui sont le format de distribution binaire du reste de
l'ecosysteme Python. Tout paquet publie uniquement en manylinux doit donc etre
compile depuis les sources sur le telephone.

Voici ce que cela donne pour les dependances de ce projet :

| Paquet | Format publie | Consequence sur Termux |
| --- | --- | --- |
| `poke-env`, `Flask`, `requests`, `tabulate` | pur Python | s'installe directement |
| `websockets` | extension C optionnelle | repli pur Python automatique |
| `numpy`, `gymnasium`, `pettingzoo` | manylinux | **evites**, voir plus bas |
| `orjson` | Rust, manylinux | **evite**, voir plus bas |
| `pydantic-core`, `jiter` (via `anthropic`) | Rust, manylinux | compilation Rust, une seule fois |

### numpy, gymnasium, pettingzoo : jamais importes

Ces trois paquets ne servent qu'a `poke_env.environment`, l'enveloppe Gymnasium
pour l'apprentissage par renforcement. Ce projet utilise `poke_env.battle`,
`poke_env.player` et `poke_env.calc`, et `poke_env/__init__.py` n'importe pas le
module RL. On installe donc poke-env avec `--no-deps` et on ajoute a la main les
seules dependances reellement utilisees.

### orjson : remplace par un shim stdlib

poke-env n'utilise d'orjson que `loads()` et `JSONDecodeError`, a neuf endroits.
`compat/orjson.py` fournit ces symboles au-dessus du module `json` de la
bibliotheque standard. Il n'est ajoute au `sys.path` que si le vrai orjson est
introuvable (`bot/compat.py`), donc une installation classique sur PC continue
d'utiliser le vrai orjson et ses performances.

Resultat : **le bot n'a aucune extension native a compiler.**

### anthropic : la seule compilation Rust

Le SDK officiel est pur Python mais depend de `pydantic-core` et `jiter`, tous
deux ecrits en Rust. Il faut donc `pkg install rust` et compter 10 a 30 minutes
de compilation, une seule fois. `scripts/termux_setup.sh --llm` s'en charge et
met les wheels construits en cache dans `~/.cache/showdownbot-wheels`, ce qui
rend toute reinstallation instantanee.

Sur un telephone a faible RAM, la compilation peut echouer par manque de
memoire. Le script force deja `CARGO_BUILD_JOBS=1`. Si cela ne suffit pas,
fermez les autres applications, ou utilisez le mode differe ci-dessous.

### Jouer sans la couche LLM

Le bot joue et logge sans `anthropic`. Les logs de combat sont des JSON
autonomes : on peut jouer des heures sans cle API, puis rattraper toutes les
analyses d'un coup :

```bash
python -m bot.cli analyse --all
```

## Empecher Android d'endormir le bot

C'est la cause numero un de deconnexion silencieuse.

1. **Wake-lock** : acquis automatiquement par le CLI pendant une session de jeu.
   Manuellement : `termux-wake-lock` / `termux-wake-unlock`.
2. **Optimisation de batterie** : Parametres Android → Applications → Termux →
   Batterie → **Sans restriction**. Sans cette exemption, le wake-lock ne suffit
   pas sur beaucoup de surcouches constructeur (Xiaomi, Samsung, Huawei sont les
   plus agressives).
3. **Notification persistante** : gardez la notification Termux visible, c'est
   ce qui signale au systeme que la session est active.

Meme avec tout cela, une coupure reseau reste possible. Le superviseur de
`bot/connection.py` la rattrape : nouveau client, nouvelle authentification,
reprise au nombre de combats restants.

## Tourner en arriere-plan avec tmux

```bash
pkg install tmux

tmux new -s bot                 # nouvelle session
source .venv/bin/activate
python -m bot.cli ladder --battles 20
# Detacher: Ctrl-b puis d

tmux ls                         # lister les sessions
tmux attach -t bot              # revenir dessus
```

Deux fenetres, une pour le bot et une pour le dashboard :

```bash
tmux new -s bot
source .venv/bin/activate && python -m bot.cli accept --battles 10
# Ctrl-b puis c   (nouvelle fenetre)
source .venv/bin/activate && python -m web.app
# Ctrl-b puis 0/1 pour naviguer entre les fenetres
```

## Serveur Showdown local

```bash
bash scripts/termux_setup.sh --server
node ~/pokemon-showdown/pokemon-showdown start --no-security
```

Comptez 500 Mo a 1 Go d'espace disque et un pic de RAM pendant le build. Le
drapeau `--no-security` desactive l'authentification, ce qui est necessaire au
bot-vs-bot local et acceptable puisque le serveur n'ecoute que sur la boucle
locale.

Si Node.js ne passe pas sur votre appareil, le harness hors ligne
(`tests/harness.py`) permet quand meme de developper et tester les heuristiques
sans aucun serveur.

## Depannage

**`Reponse tronquee: le budget de X tokens a ete entierement consomme`**
Ne peut survenir qu'avec `ANALYSIS_THINKING=true` (desactive par defaut):
l'appel API a reussi (200 OK) mais le raisonnement adaptatif a consomme tout
le budget avant d'ecrire la reponse finale. Meme `ANALYSIS_EFFORT=low` s'est
avere insuffisant a l'usage (~12000-13000 tokens de sortie mesures) - le
reglage recommande est `ANALYSIS_THINKING=false`, qui evite completement ce
symptome. Voir la section "Cout API" du README pour le detail de cette
mesure.

**`ModuleNotFoundError: No module named 'orjson'`**
Le shim ne s'est pas active. Verifiez que vous importez bien `bot` avant
`poke_env` (tous les points d'entree du projet le font).

**`error: can't find Rust compiler`**
`pkg install rust binutils`, puis relancez `scripts/termux_setup.sh --llm`.

**Le bot se deconnecte apres quelques minutes ecran eteint**
Exemption d'optimisation de batterie manquante, voir plus haut.

**`Connection refused` sur `ws://localhost:8000`**
Le serveur Showdown local n'est pas demarre, ou `SHOWDOWN_SERVER=online` est
attendu dans le `.env`.

**Espace disque sature**
`node_modules` du serveur Showdown est le principal consommateur. Les logs de
combat font environ 30 a 80 Ko chacun.
