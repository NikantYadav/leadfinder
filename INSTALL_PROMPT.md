# Install prompt for Enfolo Lead Finder

Paste the text below into Claude Code, started from the folder where the project should go (for example `~/Desktop`).

Before you need it: Claude Code installed and logged in, Python 3.10+, git. Docker is optional but recommended.

---

```
Install the Enfolo Lead Finder for me and start its web UI. Do these steps in order and stop to tell me if one fails:

1. Clone the repository: git clone <REPO_URL> leads   then cd into leads.
   (If the folder already exists, git pull in it instead.)
2. Check prerequisites: python3 --version (needs 3.10+), git, and `claude --version`.
   Check whether Docker is installed and running. If it isn't, tell me that free OpenSERP search
   will be off and that I can add TINYFISH_API_KEY or SERPER_API_KEY to .env instead. Don't install Docker yourself.
3. Read install.sh, then run it in the background: ./install.sh
   It creates the Python venv in ./env, installs requirements.txt and Playwright Chromium,
   creates .env from .env.example, starts the OpenSERP Docker container, runs the core tests,
   and starts the web UI on http://127.0.0.1:8787.
4. Wait until http://127.0.0.1:8787 responds (curl -s -o /dev/null -w '%{http_code}'), then open it in my browser
   (xdg-open on Linux, open on macOS).
5. Tell me: whether everything installed, any warnings, and which optional API keys in .env I may want to fill in
   (TINYFISH_API_KEY first; email keys like PROSPEO/HUNTER for email finding). Never print or commit .env contents.

To start it again later: cd leads && ./lf ui
```
