# Agent Inbox

Alle wartenden Claude-Code-Sessions an einem Ort, nach Hebel gerankt. Die Antwort geht direkt aus dem Browser an die fragende Session.
Dazu ein Statistik-Tab über die eigene KI-Nutzung (Claude Code, OpenCode, Codex).
Idee: [Mark Burazin, «I rank my decisions by how much work each answer unblocks»](https://www.linkedin.com/pulse/i-rank-my-decisions-how-much-work-each-answer-unblocks-mark-burazin-sb6df/).

macOS, Python 3.9+, nur Standardbibliothek. Liest ausschliesslich lokale Dateien, bindet nur auf `127.0.0.1`, schickt nichts nach aussen.

```sh
python3 server.py --open   # http://127.0.0.1:8787
./install.sh               # optional: Autostart via launchd
```

## Setup durch einen Agent

Für Claude Code o.ä., wörtlich ausführbar. Ziel: Server läuft dauerhaft, Inbox erreichbar.

1. Voraussetzungen prüfen: `python3 --version` (≥ 3.9) und `command -v claude`. Fehlt `claude`, funktioniert alles ausser «Antworten senden».
2. Optional prüfen, ob `gh auth status` ok ist. Dann zeigen Karten offene PRs + CI-Status.
3. Repo klonen, z.B. nach `~/Tools/agent-inbox`, und dort `./install.sh` ausführen.
   Das Skript schreibt `~/Library/LaunchAgents/local.agent-inbox.plist` mit dem aktuellen `PATH` und startet den Dienst.
   `install.sh` aus der Shell ausführen, in der `claude` und `gh` im `PATH` sind, sonst fehlen sie dem Dienst.
4. Testen: `curl -s 127.0.0.1:8787/api/inbox` liefert JSON mit `items`. Mit laufenden Claude-Sessions ist die Liste nicht leer.
   Fehler stehen in `~/.local/state/agent-inbox/server.log`.
5. Gruppen für die Statistik: den User nach seiner Ordnerstruktur fragen (z.B. `~/Projects/Kunden/…`, `~/Projects/Privat/…`)
   und `~/.config/agent-inbox/config.json` schreiben (siehe unten). Ohne Config gibt es keine Gruppen, nur Projekte.
   Die Datei wird bei jedem Abruf neu gelesen, ein Neustart ist nicht nötig.
6. Port belegt? `AGENT_INBOX_PORT=8790 ./install.sh`. Dann die Variable zusätzlich in die Plist unter `EnvironmentVariables` eintragen, oder `server.py` direkt starten.
7. Dem User die URL nennen. Erster Statistik-Build dauert je nach Transcript-Menge ein paar Sekunden bis Minuten.

Entfernen: `./install.sh uninstall`. State liegt in `~/.local/state/agent-inbox/` und darf gelöscht werden.

## Konfiguration

`~/.config/agent-inbox/config.json`, optional:

```json
{
  "groups": {
    "Kunden": "Kunde",
    "Intern": "Intern",
    "Privat": "Privat"
  }
}
```

Schlüssel = Ordnername irgendwo im Arbeitsverzeichnis der Session, Wert = Gruppe. Das Projekt ist der Ordner direkt darunter.
`~/Projects/Kunden/acme/api` → Gruppe «Kunde», Projekt «acme». Sessions ausserhalb dieser Ordner landen in «Andere».

Umgebungsvariable `AGENT_INBOX_PORT` (Default 8787).

## Quellen (nur gelesen)
- `~/.claude/sessions/*.json`: lebende Sessions, `busy`/`idle`
- `~/.claude/projects/*/<sessionId>.jsonl`: letzte Agent-Nachricht, offene AskUserQuestion, `away_summary` als Headline
- `gh pr list --head <branch>`: offener PR + CI-Status (5 min Cache), optional
- Hook-Events unter `~/.local/state/busybar-agents/events/*.json`, optional. Erkennen Permission-Prompts (Zustand «blockiert»).
  Format pro Datei: `{"agent": "claude", "pid": 123, "state": "waiting", "event": "PermissionRequest", "updated": 1730000000}`.
  Ohne diese Events fehlt nur der Zustand «blockiert».

Eigener State: `~/.local/state/agent-inbox/` (`parked.json`, `stats-index.json`, Log). Parken endet automatisch bei neuer Aktivität.

## Score
Gewichte in `server.py`, jede Karte zeigt ihre Rechnung: blockiert +3, Frage +2, offener PR +2, CI rot +1, Alter 0.1/h (max. 24 h), geparkt ×0.5.

## Rückweg
`claude -p --model haiku` ruft das offizielle `SendMessage`-Tool auf (~8 s). Beim Empfänger kommt das als Peer-Nachricht von `agent-inbox-*` an.
Kostet pro Antwort einen kurzen Haiku-Call.
Bewusste Grenzen: Permission-Prompts und offene Auswahl-Dialoge gehen nur im Terminal, weil Peer-Nachrichten nicht als Freigabe gelten.
POST-Endpoints brauchen das Token aus der Seite (CSRF).

## Statistik-Tab (`#stats`)
Quellen: Claude-Code-Transcripts (inkl. Subagents), Codex (`~/.codex/sessions`), OpenCode (`~/.local/share/opencode/opencode.db`, read-only; Tabelle `session_message`, ältere aus `message`).
Fehlende Quellen werden übersprungen.
`stats.py` indexiert inkrementell nach `~/.local/state/agent-inbox/stats-index.json` (alle 10 min, nur Summen, keine Inhalte).
Claude Code löscht Transcripts nach `cleanupPeriodDays` (Default 30). Der Index behält die Summen, darum wächst die Historie ab dem ersten Start.
Vergleiche mit der Vorperiode erscheinen erst, wenn diese vollständig im Index liegt.

- USD: API-Listenpreise inkl. Cache (Preistabelle in `stats.py`). Mit Abo ist das ein Vergleichswert, nicht die Rechnung.
  OpenCode liefert eigene Kosten, Codex (GPT) bleibt ohne Preis.
- «Gespart»: Cache-Reads zum normalen Input-Preis minus Read-Preis.
- Wartezeit: Ende einer Agent-Antwort bis zum nächsten Prompt. Pausen über 8 h zählen nicht.
- Parser geändert? `INDEX_VERSION` erhöhen: vorhandene Dateien werden neu gelesen, Summen gelöschter Transcripts bleiben.

## Betrieb

```sh
launchctl kickstart -k gui/$(id -u)/local.agent-inbox   # nach Code-Änderungen neu starten
./install.sh uninstall                                  # Autostart entfernen
```

## Lizenz
MIT
