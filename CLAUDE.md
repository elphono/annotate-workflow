# docpin

**Le dépôt est public** (<https://github.com/elphono/docpin>). Il s'est appelé `annotate-workflow`, puis `redmargin`
(2026-10-06), avant `docpin` : un nom qui dit ce que l'outil fait. La commande reste `annotate` et le paquet Python aussi ; le dossier local garde l'ancien
nom, parce que le renommer casserait ce qui en porte le chemin — shebangs du `.venv`, unité systemd,
hook de `~/.claude/settings.json`, raccourci Démarrage de la pastille. Le README et la ROADMAP sont
la vitrine ; ce fichier est le mode d'emploi de qui modifie le code.

Successeur du pont papier de remarkable-sync (dépôt privé de l'auteur), sans tablette. **Le besoin** : quand une
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
        ├─ non, mais la session est OUVERTE ─► les notes sont postées dans sa boîte de
        │                     réception (socket de messagerie entre sessions) ; elle les
        │                     reçoit derrière un dialogue Approve / Deny, puis relance wait
        └─ non, et elle n'est ouverte nulle part ─► onglet Windows Terminal :
                  `claude --resume <session>` interactif (session neuve si elle a disparu)
```

**Deux voies d'entrée au registre, et il les faut toutes les deux** :

| Voie | Ce qu'elle voit | Ce qu'elle apporte |
|---|---|---|
| le hook (push) | les `Write` et `Edit` d'une session | la session à coup sûr, et la consigne `annotate wait` |
| le parcours du démon (toutes les 30 s) | tout document des dossiers suivis écrit **depuis son premier passage** | ce que Bash écrit (`cp` depuis un scratchpad, scripts, sous-agents) ; la session est retrouvée dans les transcripts |

Mesuré le 2026-10-05 : un `docs/<nom>.html` d'un dépôt du workspace, composé par un
sous-agent dans son scratchpad puis copié par `cp`, n'est jamais passé par le hook. **Rien
d'antérieur au premier passage n'est repris** (décision du 2026-10-05) : `scan.json` garde le
début du dernier passage, si bien qu'un fichier écrit démon arrêté est trouvé au démarrage
suivant, et qu'un document oublié ne revient que s'il est réécrit. `figures/` est exclu des deux
voies : ce sont les sources HTML des images d'un document.

**Les dossiers suivis se choisissent sur la page d'accueil** (décision du 2026-10-07, `folders.py`,
`~/.local/share/annotate/folders.json`). Par défaut, un seul : les `docs/` du workspace. Un dossier
ajouté — n'importe lequel sous `~` — suit **tout** `.html` en dessous, hors `build/`,
`node_modules/`, `figures/`, `.claude/`… Mesuré ce jour-là : ce qui échappait était hors de la règle
`docs/` (`~/.claude/sync/docs/`, un `rapports/` de dépôt), jamais un raté du parcours. **La règle ne
vit qu'à un endroit** : le hook passe tout `.html` à `annotate register --hook`, qui demande
`folders.wanted` ; il n'en garde aucune copie.

**Le rattrapage est celui de l'utilisateur, et il est borné** : « Rescan » (page, pastille,
`annotate rescan`) et l'ajout d'un dossier prennent les fichiers des N derniers jours (7 par défaut)
— un rattrapage complet aurait ramené 100 documents d'un coup. Il ne ramène pas un document passé en
« Unmanage » tant qu'il n'a pas été réécrit (`forgotten.json`, date du fichier au moment de
l'oubli). Le passage régulier, lui, ne rattrape toujours rien.

**La page d'accueil (`/`, ouverte d'un clic gauche sur la pastille) porte tous les contrôles
de la pastille** : par document Open, Send to session, New session, Unmanage, Delete file ; les
dossiers suivis (ajout avec suggestions, retrait) et Rescan ; pour le démon Restart et Stop, demandés à systemd (`service.control`, `--no-block`, refusés si le
démon n'a pas été lancé par systemd). **Start n'y est pas, et ne peut pas y être** : un démon
arrêté ne sert plus de page où cliquer ; il reste dans la pastille. La page passe par la même
API que la pastille, avec la même garde (`X-Annotate`, origine locale).
**Attach to session… / Detach** (2026-10-08) corrigent la session d'un document : un sélecteur
filtrable liste les conversations reprenables de la machine (`claude.sessions`, ouvertes
d'abord, au plus `SESSION_LIMIT` = 50), et `registry.attach` refuse en 400 un id que
`claude --resume` ne trouverait pas, sans toucher ni au statut ni aux notes.

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
uv run annotate sessions                 # les conversations auxquelles rattacher un document
uv run annotate attach <id> <session> [--cwd DIR] | detach <id>
uv run annotate folders [add|remove <dossier>]   # les dossiers suivis
uv run annotate rescan [--days N] [--folder D]   # rattrape les N derniers jours
uv run annotate serve                    # le démon, au premier plan
uv run annotate service                  # écrit ~/.config/systemd/user/annotate.service
uv run annotate tray [--start|--once|--uninstall]   # pastille Windows + raccourci Démarrage
uv run annotate status                   # le démon en une ligne
```

Configuration : `~/.config/annotate/config.toml` (`port`, `claude_bin`), surchargeable par
`ANNOTATE_PORT`, `ANNOTATE_CLAUDE` ; données sous
`ANNOTATE_DATA_DIR` (défaut `~/.local/share/annotate`). `ANNOTATE_WORKSPACE` (défaut
`~/workspace`) donne le dossier suivi par défaut, tant que la liste n'a jamais été modifiée.

## Langue

**Anglais** dans tout ce qui est code : identifiants, commentaires, docstrings, noms de tests,
messages de log, libellés de l'overlay et de la pastille. **Français** pour ce fichier, les
documents de `docs/` et les messages de commit.

**Une exception, déclarée le 2026-10-06 : `README.md` et `ROADMAP.md` sont en anglais**, parce que
ce sont la vitrine d'un dépôt public. Des libellés d'interface en français seraient une autre
exception, à écrire ici avant de les écrire dans le code.

## Ce dépôt est public

Rien de ce qui appartient à un projet d'employeur n'y entre : ni nom de dépôt ou de produit, ni
chemin réel, ni nom de session, ni clé de ticket, ni hôte. Le 2026-10-06, un chemin réel de dépôt
d'employeur a été trouvé dans une docstring, un test et ce fichier ; il y avait été mis comme
exemple mesuré. Il est retiré de l'arbre, **pas de l'historique** (deux commits le portent), et
un exemple se prend désormais dans un chemin neutre (`<repo>/docs/<name>.html`). Avant d'écrire
une mesure réelle dans un test ou une docstring, la rendre anonyme.

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
| le hook n'enregistre qu'un document d'un dossier suivi, et la règle n'a qu'une copie | `test_register_for_the_hook_takes_only_a_document_of_a_tracked_folder`, `test_every_html_file_is_handed_over_and_annotate_decides` |
| un dossier ne se suit que sous `~` ; un dossier ajouté suit tout `.html`, le workspace garde sa règle `docs/` | `test_a_folder_outside_the_home_directory_or_not_absolute_is_refused`, `test_a_folder_the_user_adds_tracks_every_html_under_it` |
| un rescan ne prend que les N derniers jours, un à la fois, et ne ramène pas un document oublié | `test_a_catch_up_takes_the_recent_window_only`, `test_one_catch_up_at_a_time_and_the_last_one_is_reported`, `test_an_unmanaged_document_stays_out_of_a_catch_up_until_written_again` |
| un document ne se rattache qu'à une conversation que `claude --resume` trouverait ; le rattachement écrit `session_id`, sans toucher au statut ni garder un worktree disparu | `test_attaching_a_document_writes_its_session_and_moves_it_to_that_group`, `test_a_session_this_machine_cannot_resume_is_refused_with_a_400`, `test_an_attach_keeps_the_status_and_never_a_deleted_worktree_folder` |
| la liste des sessions ne propose que des conversations reprenables, jamais un sous-agent, les ouvertes d'abord, bornée sans couper une session ouverte | `test_the_list_offers_only_resumable_conversations_and_never_a_subagent`, `test_open_sessions_come_first_then_the_most_recently_active`, `test_the_list_is_bounded_and_never_cuts_an_open_session` |
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
- **Une session ouverte ne reçoit jamais un onglet de plus** (2026-10-06 : « ça m'a ouvert un
  onglet avec ma session en double »). Le démon lit `~/.claude/sessions/<pid>.json` (le registre
  de `claude agents --json`) et poste dans la boîte de réception de la session (`inbox.py`).
  Le format de la ligne **n'est pas documenté** : capturé le 2026-10-06 sur un vrai
  `SendMessage`, figé par `tests/test_inbox.py`. Le démon ne revendique **aucun** `from-mode` :
  une session en `bypassPermissions` retient donc le message derrière un dialogue d'approbation,
  décision de l'utilisateur (« dialogue à chaque envoi »). Si la boîte refuse, les notes restent
  à envoyer, sans onglet.
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

La liste à jour de ce qui vient, de ce qui est reporté et de ce qui est refusé est `ROADMAP.md` ;
cette section en garde le raisonnement.

Surligner, entourer, barrer à la souris et y attacher une note (sélection `Range` + CSS Custom
Highlight API ; rectangle sur un `<canvas>` superposé ; même ancrage). Pas d'extension Chrome :
l'overlay injecté par le démon couvre le besoin sans installation ; une extension ne redevient
utile que pour annoter des pages que le démon ne sert pas. Détail dans
`docs/etude-reutilisation.html`.
