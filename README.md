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
   read LinkedIn posts about the shows (authors, commenters, reactors) → read exhibitors' customer pages →
   search LinkedIn → search the web → check profiles → find emails → prepare results. A checklist shows progress
   (usually 20-40 min); you can close the tab.
3. Results appear on the same page: each person with LinkedIn, email and why they fit (click a person for all
   evidence and sources). Set status and notes as you reach out; **Download Excel** any time.
- **Edit & re-run** changes the description; **Run again** retries anything that didn't finish.
- **Exhibitor lists that need you.** Some show sites only show their exhibitors after a search or in a browser. When
  such a list is missing, the run pauses before people search and asks, per show: paste a link to the list
  (a2z / ExpoFP / Map Your Show / Swapcard directory, a PDF, or any page listing the companies), or **Copy prompt for a
  browser agent**. That prompt is for a Claude agent with a browser (e.g. Claude in Chrome) that collects the list with
  you; you log in, solve checks and click where it asks. It runs `./lf import-exhibitors` itself or gives you JSON to
  paste back. You can also skip a show or **Continue** without it. CLI: `./lf help-shows --industry <slug>
  [--prompt <show_id>]`, `./lf read-list`, `./lf import-exhibitors`.
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
| Pages and PDFs | Direct download, then OpenSERP's renderer | `TINYFISH_API_KEY` (TinyFetch, 1,000 URLs/day), `FIRECRAWL_API_KEY` (last fallback; PDFs raw for 1 credit) |
| Exhibitor-list finder, browser-only lists | - | `FIRECRAWL_API_KEY`: `/map` of a show's site (1 credit) finds its list / directory / PDF; a scrolling render (1 credit) reads lists that only load in a browser; `./lf site-map` |
| Bot-checked sites, event apps | `./lf capture <url>` opens a visible browser **you** drive (log in, scroll); it saves what you see | none |

**Phase 2 (optional keys; free credits are spent only on your top-scored leads):**
| Step | Command | Providers |
|---|---|---|
| Check the person is in India and still at the company | `./lf verify-profiles --industry X --top 50` | Apify ($5/month free) or Bright Data |
| Find company websites | `./lf resolve-domains --industry X` | Hunter's domain finder (free), otherwise a search guess (TinySearch first) |
| Find work emails | `./lf enrich-emails --industry X --top 30` | Prospeo → Hunter → Apollo → Tomba → BetterContact; stops at the first verified or catch-all email |
| LinkedIn posts about a show | `./lf posts-api --show 3 --query '"IMTEX 2025"'` | Apify |
| **Search LinkedIn directly** (by company + function + seniority + India) | `./lf linkedin-people --industry X --icp C` | Deepline (free, 5,000 results; first choice), then Apify, AI Ark, Bright Data Business Search |
| Booth visitors tagged in exhibitors' LinkedIn posts | `./lf company-posts --show 3` | Bright Data |
| Fix a wrong company website / LinkedIn page | `./lf set-company --name "X" --linkedin-url URL` | |
| See remaining credits | `./lf credits` | |

Searches that can't run yet wait in a queue: `./lf run-queue` continues them later, and `./lf queue-links` turns
them into clickable Google links.

## CLI (Claude uses this; you can too)
`./lf -h`. Common commands: `stats`, `frequent --min 3`, `run-queue`, `queue`, `export`, `providers`.
After a parser change, `./lf reparse` re-reads every saved search result (no new searches) and shows what would
change; `--apply` writes it. It cleans titles, locations and headline-shaped company names, takes the job dated
"– Present" as the current role, and flags people whose excerpt shows they moved ("now at X") or only a past job at
the company ("left X?"). Flagged people drop out of the results list and the email step; nothing is deleted.
Tests: `./env/bin/python tests/test_core.py` (also test_pipeline, test_firecrawl, test_events, test_keys, test_phase2,
test_showposts).

Show posts (`attendees` step, `./lf show-posts --show <id>`): LinkedIn posts about up to 3 core shows from the last
2 years (relevance-sorted, so older editions are reachable; a post goes to whichever of the search's shows it names and
was written within 60 days of), then the commenters and reactors of the best posts. Sonnet reads each person's headline and own
words and decides which persona group they fit (or none), their current job and employer, and whether they say they
visited or exhibited (`leadfinder/judge.py`; answers cached; keyword titles decide only when Sonnet can't be reached). A comment about meeting / visiting, or a post about visiting, makes a person **seen at a show**; a
plain like or comment is **engaged with a show post**: weaker, it passes only buyer groups' company rules and never
adds the company to the pool. About $0.50-0.80 of Apify credit per show (options `attendee_post_shows`,
`attendee_post_usd`, `attendee_post_days`); everything read is cached, so a re-run pays only for new posts. The older
sherwood actor runs only with option `attendee_sherwood`.

Exhibitors' customers (`customers` step, `./lf customers`): for buyer personas only, up to 20 exhibitors' websites
per run (option `customer_companies`) are checked for a clients / customers / case-studies page; the customers named
there join the company pool as **buys from an exhibitor** (indirect: not proof of attending). Pages are fetched
directly (free); a Firecrawl map (1 credit) only when the homepage shows no such link. Sites are never read twice.

Firecrawl budget (free plan: 1,000 credits/month per key, no rollover; repeat `FIRECRAWL_API_KEY=` on another line
of `.env` to add a key). A pipeline run uses at most 40 credits (option `firecrawl_credits`): up to 8 show-site maps
plus reads, and up to 12 company "events page" maps by Claude. Automatic steps stop when a key pool would drop below
`FIRECRAWL_RESERVE` (default 150), which is kept for your own "Read this link" / `./lf fetch` use. Not used on
purpose: `/search` (2 credits per 10 results; TinySearch is free), JSON extraction (+4 credits a page), `/crawl`,
`/extract`, and the "enhanced" anti-bot proxy.

## Layout
`leadfinder/` code · `templates/icp_schema.json` persona JSON shape (no content; personas come from your description) · `.claude/skills/find-leads/SKILL.md`
the step-by-step playbook Claude follows · `data/leads.db` everything found · `data/raw/` downloaded pages
(untrusted text) · `data/work/<industry>/` the rows Claude extracted · `output/` Excel files.
