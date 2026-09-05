# madison-yoga-sync

Pulls the week ahead of yoga classes from six Madison-area studios and writes
them into a shared "Yoga" Google Calendar. Runs weekly via GitHub Actions
(see `.github/workflows/weekly.yml`).

## Studios

| Studio | Script | Source |
|---|---|---|
| Dragonfly Hot Yoga | `dragonfly_yoga_sync.py` | Arketa booking widget |
| Perennial Yoga (Madison) | `perennial_yoga_sync.py` | Mindbody "go" widget (Next.js server action) |
| Sukha Somatics | `sukha_yoga_sync.py` | Momence read-only host API |
| Main Street Yoga Center | `main_street_yoga_sync.py` | WellnessLiving Explore REST API |
| Capital Fitness / Yoga Sangha | `capital_fitness_yoga_sync.py` | Studio's own website schedule page (Mindbody is Cloudflare-blocked) |
| Yoga Co-op of Madison | `yoga_coop_sync.py` | Hardcoded from the term PDF (needs a manual refresh ~4x/year) |

See `FINDINGS.md` for details on each source, known fragility, and filtering logic.

## Setup

```
pip install -r requirements.txt
cp .env.example .env   # fill in credentials
```

Required in `.env`:
- `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`, `GOOGLE_REFRESH_TOKEN` — Google OAuth creds with Calendar scope
- `YOGA_CALENDAR_ID` — the target Google Calendar's ID

Run any script directly: `python dragonfly_yoga_sync.py`. Each is idempotent —
safe to re-run any time.
