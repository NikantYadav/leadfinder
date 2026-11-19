# Enfolo Lead Finder

Finds the people you describe (e.g. buyers who walk trade shows, sellers at companies exhibiting at 3+ shows) for
Enfolo's market validation. Every lead carries an evidence link and a confidence level. Output: `output/leads_<industry>.xlsx`.

## Use it
In Claude Code, from this folder:
```
/find-leads machine tools
/find-leads "packaging machinery" --geo "India + export markets"
```
Claude drafts the personas for that industry and **waits for your approval**. It then finds shows, collects
exhibitors and visitor/buyer signals, queues the people searches and exports the Excel file. Running it again
on the same industry continues where it stopped.

## Web interface (the easy way)
```
./lf ui            # then open http://127.0.0.1:8787  (--port to change)
```
1. Click **New search** and describe who you want to talk to, e.g. *"Purchase heads at auto-component makers who
   visit machine-tool trade shows"*. Pick the region and how many emails to find.
2. Click **Find people**. One pipeline does everything: understand the persona → find shows, exhibitors and buyers →
   search LinkedIn → search the web → check profiles → find emails → prepare results. A checklist shows progress
   (usually 20-40 min); you can close the tab.
3. Results appear on the same page: each person with LinkedIn, email and why they fit (click a person for all
   evidence and sources). Set status and notes as you reach out; **Download Excel** any time.
- **Edit & re-run** changes the description; **Run again** retries anything that didn't finish.
- Claude runs use **Sonnet 5 at low effort** by default ("Standard"); choose **Deep research** for Opus, high effort.
  Runs use your Claude plan, limited to this project's `./lf` tool, reading files, writing under `data/work/` and
  web search.
- Runs on this laptop only (localhost).

## The Excel file
| Sheet | What's in it |
|---|---|
| Leads | One row per person: ICP, score 0-100, confidence (confirmed / likely / inferred), evidence link, reason |
| Companies | Every company, with ICP fit, shows attended in a 12-month window, visitor signals, and one-click LinkedIn/Google people searches |
| Tracker | **Edit this one**: status, contact dates, interview, pain confirmed, notes. Your edits are read back before every re-export, and the old file is backed up to `output/backups/` |
| Evidence / Shows | Every source behind every claim |

## Free data sources (add keys to `.env`; see `.env.example`)
| Need | Works with no key | Free upgrades |
|---|---|---|
| People search (LinkedIn via Google) | OpenSERP in Docker (free, but Google captchas after quick bursts, so it is paced) | `TINYFISH_API_KEY` (TinySearch, 12,000 free/day, used first), `SERPER_API_KEY` (2,500 free once) |
| Pages and PDFs | Direct download, then OpenSERP's renderer | `TINYFISH_API_KEY` (TinyFetch, 1,000 URLs/day), `FIRECRAWL_API_KEY` (1,000 pages/month) |
| Bot-checked sites, event apps | `./lf capture <url>` opens a visible browser **you** drive (log in, scroll); it saves what you see | none |

**Phase 2 (optional keys; free credits are spent only on your top-scored leads):**
| Step | Command | Providers |
|---|---|---|
| Check the person is in India and still at the company | `./lf verify-profiles --industry X --top 50` | Apify ($5/month free) or Bright Data |
| Find company websites | `./lf resolve-domains --industry X` | Hunter's domain finder (free), otherwise a search guess (TinySearch first) |
| Find work emails | `./lf enrich-emails --industry X --top 30` | Prospeo → Hunter → Apollo → Tomba → BetterContact; stops at the first verified or catch-all email |
| LinkedIn posts about a show | `./lf posts-api --show 3 --query '"IMTEX 2025"'` | Apify |
| **Search LinkedIn directly** (by company + function + seniority + India) | `./lf linkedin-people --industry X --icp C` | Apify (no login) or Bright Data Business Search |
| Booth visitors tagged in exhibitors' LinkedIn posts | `./lf company-posts --show 3` | Bright Data |
| Fix a wrong company website / LinkedIn page | `./lf set-company --name "X" --linkedin-url URL` | |
| See remaining credits | `./lf credits` | |

Searches that can't run yet wait in a queue: `./lf run-queue` continues them later, and `./lf queue-links` turns
them into clickable Google links.

## CLI (Claude uses this; you can too)
`./lf -h`. Common commands: `stats`, `frequent --min 3`, `run-queue`, `queue`, `export`, `providers`.
Tests: `./env/bin/python tests/test_core.py`.

## Layout
`leadfinder/` code · `templates/icp_schema.json` persona JSON shape (no content; personas come from your description) · `.claude/skills/find-leads/SKILL.md`
the step-by-step playbook Claude follows · `data/leads.db` everything found · `data/raw/` downloaded pages
(untrusted text) · `data/work/<industry>/` the rows Claude extracted · `output/` Excel files.
