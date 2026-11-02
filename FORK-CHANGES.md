# Fork-Änderungen gegenüber Upstream

Dieses Dokument inventarisiert alle bekannten fork-spezifischen Anpassungen des
Repositorys `kiro-crew-for-pi` gegenüber dem Upstream
[kirodotdev/KiroCrew](https://github.com/kirodotdev/KiroCrew).

Stand: 2026-11-02 · Branch `main` · letzter Sync-Commit `d2ce39a0f3`

---

## Eigenentwicklungen

| Feature | Beschreibung | Status | Hauptdatei(en) / Beleg |
|---|---|---|---|
| **ACP_BACKEND_VIBE** | Mistral Vibe als ACP-Backend: `SelfServedLaunch`-Eintrag, Routing, Label | Erhalten | `src/kiro_crew/agent_sdk/backends.py` (Zeilen 302–3088) |
| **Chat-lokale Backend-Selektion** | Backend pro Chat wählbar (UI + API); nicht in upstream/main | Erhalten | Commit `743507c7f7`, `src/kiro_crew/dashboard/handlers/agents.py` |
| **Meetings-Backend** | Vollständiges Meetings-App-Backend (~9 044 Zeilen, kein Upstream-Äquivalent) | Erhalten | `src/kiro_crew/apps/builtins/meetings/` |
| **Meetings-Frontend Audio** | `MeetingAudioControl.tsx` + `useMeetingAudio.ts` Hooks + Tests | Erhalten | `website/src/apps/meetings/components/MeetingAudioControl.tsx`, `website/src/apps/meetings/hooks/useMeetingAudio.ts`, `website/src/test/` (Commit `7d8677836b`) |
| **Import-Refactoring agents.py** | `_facade_compose` → `agent_admin.compose` und weitere Handler-Anpassungen | Erhalten | `src/kiro_crew/dashboard/handlers/agents.py` |
| **ACP_BACKEND_COPILOT entfernt** | `ACP_BACKEND_COPILOT` aus Re-Exports und Selektion entfernt | Erhalten | `src/kiro_crew/acp_backends.py` (Re-Export-Shim) |

---

## Upstream-Sync-Commits (fork-seitig)

| Commit | Nachricht |
|---|---|
| `d2ce39a0f3` | chore: finalize upstream sync compatibility checks |
| `b4fda851d0` | chore: refresh upstream and remove fork-only sage changes |
| `24b77f2a9d` | chore: sync upstream main and retain public fork boundaries |
| `8e24c807e5` | chore: sync upstream main and retain fork backends |
| `22f92ca889` | NO-ISSUE: integrate upstream main and preserve fork backends |

---

## Prüfbedarf

- [ ] `ACP_BACKEND_VIBE` hat noch keinen automatisierten Test (Key Result `kca5404f7` offen)
- [ ] Meetings-Frontend-Audio in Upstream prüfen (neu, kein PR bekannt)
- [ ] Chat-lokale Backend-Selektion: Upstream-PR oder upstream/main-Äquivalent prüfen

---

*Dieses Dokument wird bei jedem Upstream-Sync aktualisiert.*
