# annotate-workflow

Successeur du pont papier de `../remarkable-sync`, sans tablette. **Le besoin** : quand une
session Claude Code produit un document HTML, son lecteur l'annote dans son navigateur, et les
annotations reviennent toutes seules **dans la conversation ouverte** qui l'a produit (ou, si
aucune n'attend, dans un onglet de terminal qui la reprend). Rester simple : faciliter l'échange
entre l'utilisateur et la session, via un document.

```
session Claude Code ── Write/Edit docs/x.html
        │  hook PostToolUse  hooks/register-on-write.py
        ▼
annotate register x.html --session <id> --cwd <dépôt> --hook
        │  ~/.local/share/annotate/registry.json
        │  et, à la session : « lance `annotate wait <doc>` en arrière-plan »
        ▼
session ── annotate wait <doc>  (commande d'arrière-plan : POST …/wait, tenu par le démon)
        ┊
démon `annotate serve` (127.0.0.1:8765, service systemd utilisateur)
        │  GET /docs/<id> : le HTML du disque + overlay (annotate.js)
        ▼
navigateur Windows ── Alt+clic → épingle + note ── PUT /api/docs/<id>/annotations
        │  « Send to session » (barre, pastille ou `annotate send <id>`)
        ▼
une session attend ? ── oui ─► `annotate wait` rend les notes et sort : Claude Code réveille
        │                     LA conversation ouverte, qui corrige x.html puis relance wait
        └─ non ─► onglet Windows Terminal : `claude --resume <session>` interactif, avec les
                  notes (session neuve si elle a disparu de cette machine)
```

**Deux voies d'entrée au registre, et il les faut toutes les deux** :

| Voie | Ce qu'elle voit | Ce qu'elle apporte |
|---|---|---|
| le hook (push) | les `Write` et `Edit` d'une session | la session à coup sûr, et la consigne `annotate wait` |
| le parcours du démon (toutes les 30 s) | tout `.html` écrit sous un `docs/` **depuis son premier passage** | ce que Bash écrit (`cp` depuis un scratchpad, scripts, sous-agents) ; la session est retrouvée dans les transcripts |

Mesuré le 2026-10-05 : `<repo>/docs/<name>.html`, composé par un
sous-agent dans son scratchpad puis copié par `cp`, n'est jamais passé par le hook. **Rien
d'antérieur au premier passage n'est repris** (décision du 2026-10-05) : `scan.json` garde le
début du dernier passage, si bien qu'un fichier écrit démon arrêté est trouvé au démarrage
suivant, et qu'un document oublié ne revient que s'il est réécrit. `figures/` est exclu des deux
voies : ce sont les sources HTML des images d'un document.

**La pastille et la page d'accueil classent les documents par session** : le titre de la
session (son `/rename`, sinon celui que Claude Code a généré, lus dans le transcript, de façon
incrémentale) et son dossier ; les documents sans session connue viennent en dernier.

L'étude qui a fixé ces choix, avec ce qui est repris de remarkable-sync et la phase 2, est
`docs/etude-reutilisation.html`.

## Commandes

```bash
uv sync                                  # environnement (aucune dépendance d'exécution)
uv run pytest -q                         # suite par défaut
ANNOTATE_PLAYWRIGHT_DIR=<dossier dont node_modules contient playwright> \
  uv run pytest -m browser -v            # l'overlay dans un vrai Chromium headless
uv run ruff check src tests tools hooks  # doit passer intégralement
uv run mypy                              # doit passer intégralement
uv run python tools/mutate.py            # campagne de mutation dirigée, sur une COPIE

uv run annotate register <chemin.html> [--session ID] [--cwd DIR]
uv run annotate wait <id>                # dans une session, EN ARRIÈRE-PLAN : rend les notes
uv run annotate list | open <id> | send <id> | new-session <id> | forget <id> [--delete]
uv run annotate serve                    # le démon, au premier plan
uv run annotate service                  # écrit ~/.config/systemd/user/annotate.service
uv run annotate tray [--start|--once|--uninstall]   # pastille Windows + raccourci Démarrage
uv run annotate status                   # le démon en une ligne
```

Configuration : `~/.config/annotate/config.toml` (`port`, `claude_bin`), surchargeable par
`ANNOTATE_PORT`, `ANNOTATE_CLAUDE` ; données sous
`ANNOTATE_DATA_DIR` (défaut `~/.local/share/annotate`). Le hook lit `ANNOTATE_WORKSPACE`
(défaut `~/workspace`).

## Langue

**Anglais** dans tout ce qui est code : identifiants, commentaires, docstrings, noms de tests,
messages de log, libellés de l'overlay et de la pastille. **Français** pour ce fichier, les
documents de `docs/` et les messages de commit. Aucune exception n'est déclarée : des libellés
d'interface en français en seraient une, à écrire ici avant de les écrire dans le code.

## Signature des commits

**Aucune mention de Claude, d'une IA ou d'un co-auteur automatique** dans les messages de
commit, les descriptions de MR/PR et les commentaires de code : ni `Co-Authored-By`, ni
`Generated with`, ni emoji de robot. Cette règle prime sur les valeurs par défaut de l'outil.

## Invariants — ils sont tenus par des tests, pas par la relecture

| Invariant | Ce qui le tient |
|---|---|
| aucune dépendance d'exécution hors bibliothèque standard | `dependencies = []` dans `pyproject.toml` |
| les annotations ne vont **jamais** à côté du document (il vit dans un dépôt) | `test_annotations_never_live_next_to_the_document` |
| aucun test ne lance `claude`, `ssh`, `cmd.exe`, `powershell.exe`, `wsl.exe`, `wt.exe`, `systemctl`…, ni n'émet un vrai signal, ni ne lit les transcripts de `~/.claude` | fixtures autouse de `tests/conftest.py`, armement vérifié par `tests/test_suite_guards.py` |
| une session qui attend reçoit les notes, et aucun onglet ne s'ouvre alors | `test_an_open_session_receives_only_the_unsent_notes`, `test_the_notes_reach_the_open_session_that_waits` |
| jamais de notes livrées à une attente dont le client est parti (session fermée) | `test_a_dead_client_is_never_handed_the_notes`, `test_a_wait_whose_client_left_stops_listening` |
| un double clic ne livre les notes qu'une fois | `test_a_double_click_hands_the_notes_over_once` |
| le prompt arrive à `claude` en UN argument, quoi qu'il contienne ; aucun `;` sur la ligne de `wt.exe` | `test_the_prompt_reaches_claude_as_one_argument_in_the_right_folder`, `test_the_tab_command_never_carries_a_semicolon` |
| `send` n'envoie que les annotations jamais envoyées ; le serveur fait autorité sur `sent_at` | `test_an_open_session_receives_only_the_unsent_notes`, `test_a_stale_browser_copy_cannot_unsend_a_note` |
| une annotation dont l'ancre ne résout plus n'est jamais perdue (barre « orphelines », prompt « Anchor: lost ») | `test_a_lost_anchor_is_still_sent_with_its_quote`, `test_browser.py` |
| le parcours ne reprend rien d'antérieur à son premier passage, et ne perd rien démon arrêté | `test_nothing_older_than_the_first_pass_is_taken`, `test_the_daemon_being_down_loses_nothing` |
| l'auteur d'un document est le DERNIER appel d'outil qui le nomme avant son écriture, sous-agent compris | `test_the_writer_is_the_last_call_naming_the_file_before_it_was_written` |
| le hook et le parcours s'accordent sur ce qu'est un document | `test_the_hook_and_the_scan_agree_on_what_a_document_is` |
| `/docs/<id>/files/` ne sert rien hors du dossier du document | `test_relative_files_are_served_and_nothing_outside_the_folder` |
| aucune valeur de déploiement en dur (home, distribution, IP, port hors `config.DEFAULT_PORT`) | `test_no_deployment_value_is_written_in_the_code`, avec son témoin |

Les propriétés centrales sont éprouvées par `tools/mutate.py`, qui casse chacune sur une
copie du dépôt et exige que la suite tombe.

## Ce qui n'est pas évident, et qui a coûté

- **L'unité systemd lance `.venv/bin/annotate serve` directement, jamais `uv run`** : sous
  systemd, `uv run` retransmet le SIGTERM que systemd envoie déjà à tout le groupe (mesuré dans
  remarkable-sync le 2026-09-15 : un arrêt sur trois finissait en 143 ou en SIGKILL).
- **Le serveur injecte un `<base href="/docs/<id>/files/">`** pour que les images relatives se
  chargent. Effet de bord : un lien `href="#section"` quitterait la page ; l'overlay intercepte
  les liens de fragment et pose `location.hash` lui-même.
- **Les URL données à Windows sont en `127.0.0.1`, jamais `localhost`** : avec
  `networkingMode=mirrored`, Windows essaie `localhost` en `::1` d'abord, et une socket liée à
  `127.0.0.1` dans WSL ne le reçoit pas. Mesuré le 2026-10-05 : 22 ms contre un délai expiré.
- **Toute requête qui modifie exige l'en-tête `X-Annotate`** et refuse un `Origin` étranger :
  une page web quelconque peut viser `localhost`, et `send` lance une session autorisée à
  éditer des fichiers. Le `Host` doit nommer localhost (rebinding DNS).
- **Les épingles sont mises à jour en place, jamais recréées** : les recréer à chaque
  redimensionnement faisait disparaître l'élément survolé (surlignage collé) et rendait le test
  navigateur rouge deux fois sur huit.
- **Plus aucune session en arrière-plan, et c'est une mesure.** Le premier envoi réel
  (2026-10-05) reprenait la session par `claude -p`, sans écran : 2 min 45 s, 1,71 $, l'étude
  corrigée et commitée, et rien de visible pour l'utilisateur qui attendait devant la session
  ouverte. L'envoi va désormais à la session qui attend (`annotate wait`), sinon dans un
  **onglet** Windows Terminal qui la reprend. Un onglet et non une fenêtre : `launchMode:
  fullscreen` ne vaut que pour les fenêtres neuves (remarkable-sync, 2026-09-17).
- **`wt.exe` coupe sa ligne de commande au `;`** (mesuré le 2026-10-05 : un `bash -c 'a; b'`
  n'a exécuté que la première moitié). L'onglet lance donc un script écrit sous le dossier de
  données, et le prompt passe par un fichier, jamais par la ligne de commande.
- **Le démon tourne sous systemd sans `WSL_DISTRO_NAME` ni le dossier de `wt.exe`** :
  la distribution est lue sur `wslpath -w /`, et `annotate service` met `WindowsApps` dans
  le `PATH` de l'unité (la regénérer après mise à jour).
- **`annotate` n'est pas sur le PATH des sessions** : la consigne donnée par le hook et le
  prompt portent le chemin absolu de l'entrée du virtualenv (`config.annotate_command`).
- **Un envoi qui échoue est défait, pas rejoué** : les notes ne sont marquées envoyées
  qu'une fois une session en main ; un onglet qui ne s'ouvre pas les laisse envoyables, avec
  la raison dans `last_error`. C'est l'utilisateur qui reclique.
- **Un témoin de garde doit rester inoffensif quand la garde est désarmée** : c'est exactement
  ce que fait la mutation. Le premier `test_suite_guards.py` appelait le vrai `explorer.exe` ;
  la campagne du 2026-10-05 l'a lancé pour de bon. Les témoins sont désormais des exécutables
  inertes qui portent le nom interdit.
- **La session qui répond suit les règles globales de l'utilisateur** : elle commite, et
  pousse s'il y a un remote — y compris dans un dépôt d'équipe.
- **Une correction après livraison vaut réponse** : le hook ré-enregistre le document à chaque
  écriture, et `registry.register` fait alors passer `delivered` à `answered`. La barre du
  navigateur compare de son côté la date du fichier et propose « Reload ».

## Phase 2 — étudiée, pas codée

Surligner, entourer, barrer à la souris et y attacher une note (sélection `Range` + CSS Custom
Highlight API ; rectangle sur un `<canvas>` superposé ; même ancrage). Pas d'extension Chrome :
l'overlay injecté par le démon couvre le besoin sans installation ; une extension ne redevient
utile que pour annoter des pages que le démon ne sert pas. Détail dans
`docs/etude-reutilisation.html`.
