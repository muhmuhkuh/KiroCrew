# Fork-Änderungen gegenüber Upstream

Dieses Inventar beschreibt die verbleibenden Anpassungen gegenüber
[kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew).

## Verbleibende Fork-Funktionen

| Funktion | Abgrenzung | Hauptdateien |
|---|---|---|
| **Mistral Vibe** | Zusätzliches ACP-Backend mit Worker-Kit und Effort-Unterstützung; die gemeinsame Backend-Auswahl bleibt Upstream-Infrastruktur. | `src/kiro_crew/agent_sdk/backends.py`, `src/kiro_crew/crewmate_harness.py`, `src/kiro_crew/acp/` |
| **Chat-Backend-Wähler** | Sichtbare Auswahl und Wechsel-Endpoint pro Chat. Upstreams Backend-Pin beim Erstellen einer Session ersetzt diese Oberfläche nicht vollständig. | `src/kiro_crew/dashboard/chat_backend.py`, `website/src/` |
| **Meetings-Systemaudio** | System-/Tab-Audio zusätzlich zum Mikrofon; nicht mit Upstreams Mikrofon-Transkription gleichsetzen. | `website/src/apps/meetings/hooks/useMeetingAudio.ts`, `website/src/apps/meetings/components/MeetingAudioControl.tsx` |
| **Jira im Issue Radar** | Issue-Radar-Integration einschließlich Client und OAuth, nicht bloß eine allgemeine Atlassian-Verbindung. | `src/kiro_crew/apps/builtins/issue_radar/` |

Vibe besitzt automatisierte Tests. Eine statische Inventarisierung ist keine
E2E-Paritätszusage für die verbleibenden Funktionen.

## Gemeinsame Upstream-Funktionen

- **Backend-Auswahl pro Bot und Pi-Anbindung** sind Upstream-Funktionen. Die
  Vibe-Erweiterung bleibt eine zusätzliche Fork-Anpassung.
- **Auto-Improvement für GitLab** verwendet die Upstream-Implementierung:
  gemeinsame Profile-/PR-Infrastruktur, `GitLabMRRecipe`, hostgebundenes `glab`
  und die vorhandene `dashboard.gitlab_hosts`-Allowlist. Es gibt kein separates
  Fork-Profil `gitlab-repo` und keine parallele GitLab-Recipe mehr.
- **Meetings-Backend** ist keine eigenständige Fork-Entwicklung und erhält beim
  Sync keine pauschale `--ours`-Behandlung.
- **Agent-Handler-Komposition** ist gemeinsame Infrastruktur, keine zu bewahrende
  Import-Divergenz.
- **Copilot** ist vorhanden; „Copilot entfernt“ ist kein Fork-Vertrag.
- **Dateiauslieferung** verwendet die vorhandenen Owner-/Scanner-Pfade. Der
  ungenutzte Consent-Store, seine API-Routen und seine Hilfsfunktionen entfallen.
  Vorhandene Dateien im Datenverzeichnis werden dabei nicht gelöscht.

## Sync-Checkliste

1. Upstream in einem isolierten Worktree integrieren, nicht im laufenden Checkout.
2. Nur die oben abgegrenzten Fork-Funktionen gezielt erhalten; keine vollständigen
   Handler oder App-Verzeichnisse mit `--ours` einfrieren.
3. Vibe-Routing und -Tests, Chat-Backend-Auswahl, Meetings-Systemaudio und Jira
   prüfen, bevor eine verbleibende Anpassung entfernt wird.
4. GitLab über den gemeinsamen Upstream-Pfad testen, einschließlich selbst
   gehosteter Hosts und Credential-Isolation.
5. Build und diff-bezogene Tests prüfen; Merge, Push und Aktivierung bleiben
   getrennte, ausdrücklich freizugebende Schritte.
