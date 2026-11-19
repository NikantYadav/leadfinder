# Install prompt for Enfolo Lead Finder

Paste the text below into Claude Code, started from the folder where the project should go (for example `~/Desktop`).

Before you need it: Claude Code installed and logged in, Python 3.10+, git. Docker is optional but recommended.

---

```
Install the Enfolo Lead Finder for me, wait while I fill in my API keys, then start its web UI.
Do these steps in order and stop to tell me if one fails:

1. Clone the repository: git clone https://github.com/NikantYadav/leadfinder leads   then cd into leads.
   (If the folder already exists, git pull in it instead.)
2. Check prerequisites: python3 --version (needs 3.10+), git, and `claude --version`.
   Check whether Docker is installed and running. If it isn't, tell me that free OpenSERP search
   will be off and that I should add TINYFISH_API_KEY or SERPER_API_KEY to .env instead. Don't install Docker yourself.
3. Read install.sh, then run it: ./install.sh
   It creates the Python venv in ./env, installs requirements.txt and Playwright Chromium,
   creates .env from .env.example, starts the OpenSERP Docker container and runs the core tests.
   It does NOT start the UI.
4. STOP and ask me to fill in .env. Give me the full path to .env and list the keys, grouped by what
   they're for (from the comments in .env.example): search (TINYFISH_API_KEY recommended first, SERPER_API_KEY),
   pages (FIRECRAWL_API_KEY), LinkedIn (APIFY_TOKEN, BRIGHTDATA_API_KEY), emails (PROSPEO, HUNTER, APOLLO,
   TOMBA, BETTERCONTACT). All are optional. Then wait until I reply that I'm done.
   Never print .env contents or my key values back to me, and never commit .env.
5. After I reply, check which keys are set without showing their values
   (e.g. grep -cE '^[A-Z_]+=.+' style checks per key name) and tell me which ones are filled and which are empty.
6. Start the web UI in the background: ./lf ui
   Wait until http://127.0.0.1:8787 responds (curl -s -o /dev/null -w '%{http_code}'), then open it in my browser
   (xdg-open on Linux, open on macOS).
7. Tell me: what installed, any warnings, and that next time I can start it with: cd leads && ./lf ui
   (keys can be changed in .env any time; restart the UI after changing them).
```
