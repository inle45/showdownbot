# Bot Pokemon Showdown auto-ameliorant

Un bot qui joue en **Gen 9 Random Battle**, tourne entierement sur un telephone
Android via Termux, et s'ameliore entre les parties.

**Le choix d'architecture central** : pendant un combat, toutes les decisions
sortent d'heuristiques codees en dur. **Zero appel API en jeu.** L'API Claude
n'intervient qu'apres coup, une fois par combat, pour l'analyse post-mortem, puis
une fois tous les N combats pour consolider les lecons. Ces lecons sont traduites
en parametres **avant** le combat suivant, jamais consultees en direct.

```
                    PENDANT LE COMBAT                      APRES LE COMBAT
   ┌──────────────────────────────────────────┐   ┌──────────────────────────┐
   │  moteur heuristique                      │   │  1 appel API par combat  │
   │  config/heuristics.json  ──> decision    │──>│  -> memory/battles/*.md  │
   │  zero reseau, zero API                   │   │                          │
   └──────────────────────────────────────────┘   │  1 appel tous les N      │
                     ▲                            │  -> lessons_learned.md   │
                     │                            └────────────┬─────────────┘
                     │         tuner deterministe              │
                     └─────────  (aucune API)  ◀───────────────┘
```

## Ce qui rend la boucle sure

Laisser un modele modifier la configuration d'un bot est le genre de boucle qui
part en vrille silencieusement. Quatre garde-fous :

1. **Vocabulaire ferme.** Le modele ne propose jamais de valeur de parametre. Il
   classe ce qu'il observe dans 27 tags figes (`config/finding_taxonomy.json`),
   et l'API elle-meme garantit ce vocabulaire via `output_config.format`.
2. **Table statique.** `config/tuning_rules.json` fait correspondre chaque tag a
   des parametres. Ce fichier est ecrit a la main. Chaque mouvement de reglage
   est donc explicable par une ligne de table et un compte d'occurrences.
3. **Controleur borne.** Netting des tags antagonistes, bande morte sur la
   recurrence, un pas de registre maximum par cycle, trois parametres au plus,
   valeurs clampees. Le moteur refuse de demarrer sur une config hors bornes.
4. **Rollback.** Si le winrate chute franchement apres un changement, retour a la
   version precedente et neutralisation du couple (tag, parametre) fautif.

## Installation sur Termux

```bash
pkg install git
git clone <url-du-depot> showdownbot && cd showdownbot

bash scripts/termux_setup.sh            # bot seul, aucune compilation
bash scripts/termux_setup.sh --llm      # + analyse (compile Rust une fois)
bash scripts/termux_setup.sh --server   # + serveur Showdown local (Node)
bash scripts/termux_setup.sh --all      # tout

cp .env.example .env    # deja fait par le script
nano .env               # renseigner ANTHROPIC_API_KEY, et le compte Showdown
```

Le script evite les pieges de Termux : poke-env est installe sans ses
dependances RL (numpy, gymnasium, pettingzoo, jamais importees), orjson est
remplace par un shim stdlib, et les wheels Rust du SDK Anthropic sont mis en
cache pour n'etre compiles qu'une fois. Detail complet dans
[`docs/termux.md`](docs/termux.md).

## Utilisation

```bash
source .venv/bin/activate

# Jouer
python -m bot.cli selfplay --battles 5      # contre un adversaire de reference (serveur local)
python -m bot.cli challenge <pseudo>        # defier quelqu'un
python -m bot.cli accept --battles 3        # accepter les defis recus
python -m bot.cli ladder --battles 10       # ladder public (opt-in, voir plus bas)

# Analyser
python -m bot.cli analyse --all             # rattraper les analyses en attente
python -m bot.cli consolidate               # mettre a jour la fiche de lecons

# Regler (aucun appel API)
python -m bot.cli tune                      # proposer des ajustements
python -m bot.cli tune --apply              # les appliquer
python -m bot.cli tune --check-rollback     # verifier une degradation

# Etat
python -m bot.cli status                    # combats, winrate, cout API reel
```

### Dashboard

```bash
python -m web.app        # puis http://127.0.0.1:5000 dans le navigateur du telephone
```

Quatre pages : tableau de bord (winrate glissant, erreurs les plus frequentes,
cout), liste des combats avec leur rapport, fiche de lecons, et l'ecran de
reglages qui porte la porte d'approbation du tuner.

Le serveur n'ecoute que sur `127.0.0.1`. Ne passez `--host 0.0.0.0` que sur un
reseau de confiance.

## Evaluer une modification du moteur

Avant de toucher aux reglages, mesurez. Trois adversaires de reference sont
disponibles en local, sans cle API et sans partie de ladder :

```bash
python -m bot.cli selfplay --battles 40 --opponent random      # coups au hasard
python -m bot.cli selfplay --battles 40 --opponent maxpower    # puissance brute maximale
python -m bot.cli selfplay --battles 40 --opponent heuristic   # heuristiques de poke-env (defaut)
```

Reference mesuree avec la configuration livree (v1, valeurs par defaut) :

| Adversaire | Resultat |
| --- | --- |
| `random` | 30 victoires / 30 |
| `heuristic` (`SimpleHeuristicsPlayer`) | 27 victoires / 40, soit 68% |

Sur 40 combats, l'incertitude est d'environ 7 points : 68% signifie « nettement
au-dessus de 50% », pas « exactement 68% ». Pour comparer deux configurations,
il faut plusieurs centaines de combats — c'est justement pourquoi le tuner ne se
fie pas au winrate pour decider, seulement pour freiner.

## Lire les resultats

```
memory/
├── battles/
│   ├── 20260906-1642-gen9randombattle-123.json   log complet + trace de decision
│   └── 20260906-1642-gen9randombattle-123.md     le rapport lisible
├── lessons_learned.md                            la fiche consolidee
├── unmapped_findings.md                          tags observes sans regle associee
└── api_usage.jsonl                               un appel API par ligne, avec son cout
```

Chaque rapport contient un resume, les moments cles, les erreurs strategiques
avec le tour et la composante de score fautive, les bonnes decisions, et un
tableau des observations classees.

Le `.json` conserve la **trace de raisonnement** : pour chaque tour, l'etat vu
par le bot et le score de chaque action envisagee avec le detail de ses
composantes. C'est cette trace, et non le log Showdown brut, qui est envoyee a
l'analyse. Sans elle, le modele ne pourrait que commenter le resultat ; avec
elle, il peut dire « le switch etait a 5.28 contre 0.35 pour l'attaque, c'est le
cout de momentum qui a mal arbitre » — une phrase directement traduisible en
parametre.

## Cout API

Deux appels seulement : un par combat, un tous les N combats.

**Mesure reelle** (2 analyses, combats de 22-25 tours, `claude-sonnet-5`,
`ANALYSIS_EFFORT=medium`) : ~6 000 tokens en entree, mais **12 000 a 13 000
tokens en sortie** — nettement plus que ce qu'on pourrait attendre d'un
rapport de cette taille. Le raisonnement adaptatif du modele domine largement
le cout, bien plus que le texte du rapport lui-meme, meme a effort "medium".
Cout observe : **~0,14 $ par combat**.

| Modele (effort medium) | Par combat (mesure) | 100 combats + 10 consolidations |
| --- | --- | --- |
| `claude-sonnet-5` (defaut) | ~0,14 $ | ~15 $ |
| `claude-opus-5` | ~0,35 $ (estime, meme volume de sortie) | ~38 $ |
| `claude-haiku-4-5` | ~0,07 $ (estime) | ~8 $ |

`ANALYSIS_EFFORT=high` a ete teste et **echoue systematiquement** sur cette
tache (le raisonnement consomme a lui seul plus de 24 000 tokens sans jamais
atteindre la reponse finale) : ne pas l'utiliser pour ces deux appels.
`low` reduirait probablement le cout mais sa qualite n'a pas encore ete
validee - a tester avant de l'adopter par defaut.

Ce tableau vient d'une mesure reelle sur un petit echantillon, pas d'un calcul
theorique. Le **cout reel de vos propres combats** est mesure a chaque appel
depuis `response.usage` et cumule dans `memory/api_usage.jsonl` :

```bash
python -m bot.cli status     # cout cumule et cout par combat observes
```

Le prompt systeme etant identique d'un appel a l'autre, le cache de prompt reduit
le cout d'entree sur les combats enchaines.

## Comment fonctionne le reglage automatique

C'est la partie la plus delicate du projet. Le pipeline :

```
findings (vocabulaire ferme)
  -> agregation sur les N derniers combats
  -> netting des paires antagonistes         anti-oscillation
  -> bande morte (>= 3 combats concernes)    anti-bruit
  -> votes via la table statique
  -> un pas de registre max, valeurs clampees
  -> proposition versionnee, approuvee ou automatique
  -> surveillance du winrate, rollback si degradation
```

**Le netting est le coeur du dispositif.** Les tags vont par paires opposees
(`STAYED_IN_TYPE_DISADVANTAGE` ↔ `SWITCHED_TOO_EAGERLY`). Sans compensation, une
fenetre contenant les deux ferait bouger le meme parametre dans un sens puis dans
l'autre de cycle en cycle, et le reglage oscillerait indefiniment. Un test simule
dix cycles de defauts alternes et verifie que le parametre reste dans un pas de
sa valeur de depart.

**Mode d'approbation.** Par defaut `TUNING_MODE=propose` : les ajustements
attendent votre validation, avec le diff avant/apres, le tag declencheur et la
raison. Passez a `auto` quand les propositions vous paraissent sensees.

### Ajouter ou modifier une regle

Tout se fait dans les fichiers de configuration, sans toucher au code :

- **un nouveau seuil** : ajoutez-le a `config/heuristics.schema.json` (type,
  bornes, pas, defaut, effet documente) et lisez-le dans `bot/engine.py` ;
- **une nouvelle observation** : ajoutez le tag a
  `config/finding_taxonomy.json` avec sa description et son antagoniste ;
- **une correspondance** : ajoutez une entree dans `config/tuning_rules.json`.

Un tag observe sans regle associee est note dans `memory/unmapped_findings.md`
plutot que devine : c'est un signal qu'une regle vous manque.

## Limites, dites franchement

**Le winrate ne mesure pas si un ajustement a aide.** En Random Battle il est
tres bruite : dix combats ne suffisent pas a conclure, et meme trente restent
fragiles. Les ajustements sont donc pilotes par le jugement du modele sur le
**processus** de decision (« etait-ce defendable avec l'information disponible au
tour 7 ? »), et le winrate ne sert que de **frein de securite** sur une fenetre
plus longue. Le rollback rattrape les changements qui ont visiblement nui ; il ne
pretend pas identifier ceux qui ont aide.

**Le moteur reste une V1.** Il ne fait pas de prediction du coup adverse, pas de
raisonnement multi-tours, et sa gestion de la terastallisation est volontairement
conservatrice. Les stats adverses sont estimees selon la convention randbats
(31 IVs, 85 EVs, nature neutre) : l'erreur est petite devant l'incertitude sur le
set adverse, mais elle existe.

**La qualite de l'analyse depend du modele.** Un modele moins capable produit des
observations plus generiques, donc un reglage moins pertinent. C'est le vrai
arbitrage derriere le choix de modele, davantage que le cout par combat.

## Jouer sur le serveur officiel

Utiliser un bot sur le ladder de Pokemon Showdown est une zone grise de leur
reglement. Le ladder est donc desactive par defaut et demande un opt-in explicite
(`ENABLE_LADDER=true` ou `--force`). Si vous l'activez : un seul compte dedie, et
ne laissez pas tourner sans surveillance. Les modes `challenge` et `accept`, entre
joueurs consentants, ne posent pas ce probleme.

## Developpement

```bash
python -m unittest discover -s tests -t .     # 73 tests, sans serveur ni cle API
```

Les tests tournent entierement hors ligne. `tests/harness.py` construit de vrais
objets `Battle` de poke-env en leur injectant des messages du protocole, ce qui
permet de tester le moteur sans serveur Showdown — utile sur un telephone ou
Node.js ne passe pas.

Deux familles de tests meritent l'attention :

- **effets des parametres** : un test par parametre verifie que l'effet annonce
  dans `heuristics.schema.json` est bien celui obtenu. Si un parametre ne bouge
  pas le comportement dans le sens documente, la table de correspondance est
  fausse, et ces tests le disent ;
- **modes d'echec du tuner** : oscillation, sur-reaction a une observation
  isolee, derive hors bornes, tracabilite des mouvements.

### Structure

```
bot/          moteur, joueur poke-env, connexion, logging, CLI    (aucune API)
analysis/     les deux appels API, et le tuner qui n'en fait aucun
config/       registre des parametres, taxonomie, table de correspondance
web/          dashboard Flask
tests/        harness hors ligne et suite de tests
compat/       shim orjson pour Termux
```
