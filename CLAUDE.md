# annotate-workflow

Successeur du pont papier de `../remarkable-sync`, sans tablette. **Le besoin** : quand une
session Claude Code produit un document HTML, son lecteur l'annote dans son navigateur, et les
annotations reviennent toutes seules à la session qui l'a produit (ou à une session neuve sur ce
document). Rester simple : faciliter l'échange entre l'utilisateur et la session, via un document.

```
session Claude Code ── Write/Edit docs/x.html
        │  hook PostToolUse  hooks/register-on-write.py
        ▼
annotate register x.html --session <id> --cwd <dépôt>
        │  ~/.local/share/annotate/registry.json
        ▼
démon `annotate serve` (127.0.0.1:8765, service systemd utilisateur)
        │  GET /docs/<id> : le HTML du disque + overlay (annotate.js)
        ▼
navigateur Windows ── Alt+clic → épingle + note ── PUT /api/docs/<id>/annotations
        │  « Send to session » (barre, pastille ou `annotate send <id>`)
        ▼
claude -p --resume <session> --permission-mode acceptEdits   (en arrière-plan)
        └─► la session corrige x.html sur place ; un rechargement montre la nouvelle version
```

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
uv run annotate list | open <id> | send <id> | new-session <id> | forget <id> [--delete]
uv run annotate serve                    # le démon, au premier plan
uv run annotate service                  # écrit ~/.config/systemd/user/annotate.service
uv run annotate tray [--start|--once|--uninstall]   # pastille Windows + raccourci Démarrage
uv run annotate status                   # le démon en une ligne
```

Configuration : `~/.config/annotate/config.toml` (`port`, `send_timeout`, `claude_bin`),
surchargeable par `ANNOTATE_PORT`, `ANNOTATE_SEND_TIMEOUT`, `ANNOTATE_CLAUDE` ; données sous
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
| aucun test ne lance `claude`, `ssh`, `cmd.exe`, `powershell.exe`, `wsl.exe`, `systemctl`…, ni n'émet un vrai signal | fixtures autouse de `tests/conftest.py`, armement vérifié par `tests/test_suite_guards.py` |
| le démon ne bloque jamais sur une session | `Sender.dispatch` rend la main avant la fin ; `test_dispatch_returns_before_the_session_ends…` |
| `send` n'envoie que les annotations jamais envoyées ; le serveur fait autorité sur `sent_at` | `test_send_resumes_the_session_with_only_the_unsent_notes`, `test_a_stale_browser_copy_cannot_unsend_a_note` |
| une annotation dont l'ancre ne résout plus n'est jamais perdue (barre « orphelines », prompt « Anchor: lost ») | `test_a_lost_anchor_is_still_sent_with_its_quote`, `test_browser.py` |
| `/docs/<id>/files/` ne sert rien hors du dossier du document | `test_relative_files_are_served_and_nothing_outside_the_folder` |
| aucune valeur de déploiement en dur (home, distribution, IP, port hors `config.DEFAULT_PORT`) | `test_no_deployment_value_is_written_in_the_code`, avec son témoin |
| `kill_group` ne signale jamais le pid 0 ou 1 (copié de remarkable-sync, voir sa docstring) | `test_kill_group_refuses_pids_zero_and_one` |

Les six propriétés centrales sont éprouvées par `tools/mutate.py`, qui casse chacune sur une
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
- **Un envoi qui échoue est défait, pas rejoué** : le lot (identifié par son `sent_at`) redevient
  envoyable, et c'est l'utilisateur qui reclique. Un démon qui redémarre pendant une session
  défait de même le lot resté `sent` (`sender.recover_stale`).
- Les sessions lancées par annotate portent `ANNOTATE_SESSION=1` ; le hook les ignore, sans quoi
  la session qui répond ré-enregistrerait le document qu'elle corrige.

## Phase 2 — étudiée, pas codée

Surligner, entourer, barrer à la souris et y attacher une note (sélection `Range` + CSS Custom
Highlight API ; rectangle sur un `<canvas>` superposé ; même ancrage). Pas d'extension Chrome :
l'overlay injecté par le démon couvre le besoin sans installation ; une extension ne redevient
utile que pour annoter des pages que le démon ne sert pas. Détail dans
`docs/etude-reutilisation.html`.
