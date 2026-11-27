import os
import sys
import time
from typing import Any
import json
import urllib.request
import urllib.error
from urllib.parse import urlsplit, urlencode, quote
from email.utils import parsedate_to_datetime

class WorkerError(Exception):
    pass

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

OPEN = urllib.request.build_opener(NoRedirect()).open

def request(method, url, headers=None, payload=None):
    """Bounded retries; never log URLs/bodies (webhook URLs are credentials)."""
    for attempt in range(4):
        req = urllib.request.Request(url, headers=headers or {}, method=method,
            data=None if payload is None else json.dumps(payload).encode())
        try:
            with OPEN(req, timeout=20) as response:
                body = response.read()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            status = exc.code
            retryable = status == 429 or status >= 500 or (status == 403 and
                (exc.headers.get('Retry-After') or exc.headers.get('X-RateLimit-Remaining') == '0'))
            delay = 2 ** attempt
            if retryable:
                value = exc.headers.get('Retry-After')
                try:
                    if value:
                        try: delay = float(value)
                        except ValueError: delay = parsedate_to_datetime(value).timestamp() - time.time()
                    elif exc.headers.get('X-RateLimit-Reset'):
                        delay = float(exc.headers['X-RateLimit-Reset']) - time.time() + 1
                    elif status == 429:
                        delay = float(json.loads(exc.read()).get('retry_after', delay))
                except (ValueError, TypeError, OverflowError):
                    raise WorkerError('Invalid rate-limit response') from None
            exc.close()
            # ponytail: waits over 120s fail closed; retry on the next workflow run.
            if not retryable or attempt == 3 or delay > 120:
                raise WorkerError(f'HTTP {status}; request failed') from None
            time.sleep(max(0, delay))
        except (urllib.error.URLError, TimeoutError, OSError):
            if attempt == 3:
                raise WorkerError('Network request failed') from None
            time.sleep(2 ** attempt)
    raise WorkerError('Request failed')


# --- KONFIGURASI ENVIRONMENT ---
WORKER_ID = os.getenv("WORKER_ID", "worker-1").strip()
MASTER_API_URL = os.getenv("MASTER_API_URL", "").rstrip("/")
MASTER_API_KEY = os.getenv("MASTER_API_KEY", "").strip()
GH_TOKEN = os.getenv("GH_TOKEN", "").strip()

GITHUB_API_BASE = "https://api.github.com"

# Warna Embed Discord
COLOR_COMMIT = 0x3498DB      # Biru
COLOR_RELEASE = 0x2ECC71     # Hijau
COLOR_PRERELEASE = 0xF1C40F  # Kuning
COLOR_PR = 0xE91E63          # Magenta

DEFAULT_IGNORED_KEYWORDS = ["merge branch", "merge pull request"]
BOT_EXACT_NAMES = {"dependabot", "renovate", "github-actions", "dependabot-preview"}


# --- HELPER FORMAT & FILTER ---
def format_bytes(size: int) -> str:
    if size <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB"]
    idx = 0
    val = float(size)
    while val >= 1024 and idx < len(units) - 1:
        val /= 1024.0
        idx += 1
    return f"{val:.1f} {units[idx]}" if idx > 0 else f"{int(val)} B"


def is_bot_account(user_obj: dict[str, Any] | None, author_name: str = "") -> bool:
    if user_obj:
        if str(user_obj.get("type", "")).lower() == "bot":
            return True
        login = str(user_obj.get("login", "")).lower()
        if login.endswith("[bot]") or login in BOT_EXACT_NAMES:
            return True
    name_lower = author_name.lower()
    return name_lower.endswith("[bot]") or name_lower in BOT_EXACT_NAMES


def is_ignored_title(title: str, custom_keywords: list[str]) -> bool:
    first_line = title.splitlines()[0].strip().lower() if title else ""
    keywords = {kw.lower() for kw in (DEFAULT_IGNORED_KEYWORDS + custom_keywords) if kw}
    return any(kw in first_line for kw in keywords)


# --- KOMUNIKASI REST API TRACKER MASTER ---
def apply_embed_config(embed: dict[str, Any], config: Any, repo: str, message: str = "") -> dict[str, Any]:
    """Apply backend's effective route configuration without trusting its shape."""
    if not isinstance(config, dict):
        return embed
    result = dict(embed)
    # Only presentation fields are accepted; URLs, mentions and components stay worker-owned.
    for field, limit in (("title", 256), ("description", 4096), ("footer", 2048)):
        value = config.get(field)
        if isinstance(value, str) and value.strip():
            # Single-pass interpolation: substituted user content is never re-interpreted.
            import re
            rendered = re.sub(r"\{(repo|message)\}", lambda m: repo if m[1] == "repo" else message, value)[:limit]
            result[field] = {"text": rendered} if field == "footer" else rendered
    color = config.get("color")
    if isinstance(color, str):
        try:
            color = int(color.removeprefix("#"), 16) if len(color.removeprefix("#")) == 6 else None
        except ValueError:
            color = None
    if isinstance(color, int) and not isinstance(color, bool) and 0 <= color <= 0xFFFFFF:
        result["color"] = color
    # Discord limits the combined textual embed payload to 6000 characters.
    other = len(result.get("title") or "") + len((result.get("footer") or {}).get("text") or "")
    if isinstance(result.get("description"), str):
        result["description"] = result["description"][:max(0, 6000 - other)]
    return result


def master_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {MASTER_API_KEY}",
        "Content-Type": "application/json",
        "X-Worker-ID": WORKER_ID,
    }


def fetch_worker_repos() -> list[dict[str, Any]]:
    data = request('GET', f'{MASTER_API_URL}/api/workers/{quote(WORKER_ID, safe="")}/repos', master_headers())
    repos = data if isinstance(data, list) else data.get('repos') if isinstance(data, dict) else None
    if not isinstance(repos, list):
        raise WorkerError('Invalid repository response')
    return repos


def fetch_central_state() -> dict[str, Any]:
    data = request('GET', f'{MASTER_API_URL}/api/state', master_headers())
    if not isinstance(data, dict) or not isinstance(data.get('state'), dict):
        raise WorkerError('Invalid state response')
    return data['state']


def push_central_state(updated_states: dict[str, Any]) -> None:
    request('POST', f'{MASTER_API_URL}/api/state', master_headers(),
        {'worker_id': WORKER_ID, 'updated_states': updated_states})
    print(f'[OK] State/heartbeat tersinkron: {len(updated_states)} kunci.')


# --- DISCORD WEBHOOK SENDER ---
def send_discord_webhook(
    webhook_secret_name: str,
    thread_id: str | None,
    embed: dict[str, Any],
    content: str | None = None,
    components: list[dict[str, Any]] | None = None,
) -> bool:
    webhook_url = os.getenv(webhook_secret_name, "").strip()
    if not webhook_url:
        print(f"[WARN] Secret '{webhook_secret_name}' kosong atau tidak terdaftar di workflow.")
        return False

    query_params: list[str] = []
    if thread_id:
        query_params.append(urlencode({"thread_id": thread_id}))
    if components:
        query_params.append("with_components=true")

    if query_params:
        sep = "&" if "?" in webhook_url else "?"
        webhook_url = f"{webhook_url}{sep}{'&'.join(query_params)}"

    payload: dict[str, Any] = {"embeds": [embed]}
    if content:
        payload["content"] = content
    if components:
        payload["components"] = components

    try:
        if urlsplit(webhook_url).scheme != 'https':
            raise WorkerError('Webhook must use HTTPS')
        request('POST', webhook_url, {'Content-Type': 'application/json'}, payload)
        return True
    except Exception:
        print('[ERROR] Gagal kirim Discord; checkpoint tidak disimpan.')
        return False


def deliver(*args, **kwargs):
    if not send_discord_webhook(*args, **kwargs):
        raise WorkerError('Discord delivery failed')


def github_get(endpoint: str, params: dict[str, Any] | None = None) -> Any:
    headers = {'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28'}
    if GH_TOKEN:
        headers['Authorization'] = f'Bearer {GH_TOKEN}'
    return request('GET', f'{GITHUB_API_BASE}{endpoint}?{urlencode(params or {})}', headers)


def github_pages(endpoint, params=None):
    page = 1
    while True:
        data = github_get(endpoint, dict(params or {}, per_page=100, page=page))
        if not isinstance(data, list):
            raise WorkerError('Invalid GitHub page')
        yield from data
        if len(data) < 100:
            break
        page += 1


# --- POLLING & 4-LAYER FILTERING PER REPOSITORI ---
def process_repository(cfg: dict[str, Any], central_state: dict[str, Any]) -> dict[str, Any] | None:
    repo = cfg["repo"]
    branch = cfg.get("branch")
    state_key = f"{repo}#{branch}" if branch else repo

    route = cfg.get("discord_route") if isinstance(cfg.get("discord_route"), dict) else {}
    webhook_secret = route.get("webhook_secret_name") or cfg.get("webhook_secret", "DISCORD_WEBHOOK_SHARED")
    thread_id = None if cfg.get("thread_mode") == "none" else cfg.get("thread_id") if cfg.get("thread_mode") == "override" else route.get("default_thread_id") or cfg.get("thread_id") if route else cfg.get("thread_id")
    embed_config = route.get("embed_config") if isinstance(route.get("embed_config"), dict) else {}
    ignored_keywords = cfg.get("ignored_keywords") or []
    ignore_prerelease = bool(cfg.get("ignore_prerelease", False))
    mention_on_prerelease = bool(cfg.get("mention_on_prerelease", False))
    asset_extensions = [ext.lower() for ext in (cfg.get("asset_extensions") or [])]

    raw_mention = cfg.get("release_mention") or []
    if isinstance(raw_mention, str):
        mention_text = raw_mention.strip()
    else:
        mention_text = " ".join(str(m).strip() for m in raw_mention if str(m).strip())

    is_new_baseline = state_key not in central_state
    existing_state = central_state.get(state_key) or {}

    repo_state: dict[str, Any] = {
        "last_commit": existing_state.get("last_commit"),
        "last_release_id": existing_state.get("last_release_id"),
        "last_pr_number": existing_state.get("last_pr_number"),
    }
    state_modified = is_new_baseline

    # 1. POLLING COMMITS
    commit_params: dict[str, Any] = {"per_page": 10}
    if branch:
        commit_params["sha"] = branch

    commits = list(github_pages(f"/repos/{repo}/commits", commit_params))
    if commits:
        latest_sha = commits[0]["sha"]
        last_seen_sha = repo_state["last_commit"]

        if is_new_baseline:
            repo_state["last_commit"] = latest_sha
            state_modified = True
        elif latest_sha != last_seen_sha:
            new_commits = []
            for c in commits:
                if c["sha"] == last_seen_sha:
                    break
                new_commits.append(c)

            for c in reversed(new_commits):
                author_obj = c.get("author")
                commit_info = c.get("commit", {})
                author_name = commit_info.get("author", {}).get("name", "Unknown")
                message = commit_info.get("message", "")
                title = message.splitlines()[0] if message else "(no message)"

                if is_bot_account(author_obj, author_name):
                    continue
                if is_ignored_title(title, ignored_keywords):
                    continue

                embed = {
                    "title": f"[{repo}] New Commit: {title[:200]}",
                    "url": c.get("html_url"),
                    "color": COLOR_COMMIT,
                    "description": f"**Branch:** `{branch or 'default'}`\n**SHA:** `{c['sha'][:7]}`",
                    "footer": {"text": f"Author: {author_name}"},
                    "timestamp": commit_info.get("author", {}).get("date"),
                }
                deliver(webhook_secret, thread_id, apply_embed_config(embed, embed_config, repo, title))

            repo_state["last_commit"] = latest_sha
            state_modified = True

    # 2. POLLING RELEASES
    releases = list(github_pages(f"/repos/{repo}/releases"))
    published_releases = [r for r in releases if not r.get("draft", False)]

    if published_releases:
        latest_rel = published_releases[0]
        latest_rel_id = int(latest_rel["id"])
        last_seen_rel_id = repo_state["last_release_id"]

        if is_new_baseline:
            repo_state["last_release_id"] = latest_rel_id
            state_modified = True
        elif latest_rel_id != last_seen_rel_id:
            new_releases = []
            for release in published_releases:
                if int(release['id']) == last_seen_rel_id:
                    break
                new_releases.append(release)
            for latest_rel in reversed(new_releases):
                is_pre = bool(latest_rel.get("prerelease", False))
                if not (is_pre and ignore_prerelease):
                    tag_name = latest_rel.get("tag_name", "")
                    rel_name = latest_rel.get("name") or tag_name
                    body = latest_rel.get("body") or "Tidak ada catatan rilis."
                    if len(body) > 1500:
                        body = body[:1497] + "..."
    
                    assets = latest_rel.get("assets", [])
                    if asset_extensions:
                        filtered_assets = [
                            a for a in assets
                            if any(a["name"].lower().endswith(ext) for ext in asset_extensions)
                        ]
                    else:
                        filtered_assets = assets
    
                    buttons = []
                    for asset in filtered_assets[:4]:
                        size_str = format_bytes(int(asset.get("size", 0)))
                        label = f"📦 {asset['name']} ({size_str})"
                        buttons.append({
                            "type": 2,
                            "style": 5,
                            "label": label[:80],
                            "url": asset["browser_download_url"],
                        })
    
                    buttons.append({
                        "type": 2,
                        "style": 5,
                        "label": "📜 View Changelog",
                        "url": latest_rel.get("html_url"),
                    })
    
                    embed = {
                        "title": f"[{repo}] {'Pre-release' if is_pre else 'Stable Release'}: {rel_name}",
                        "url": latest_rel.get("html_url"),
                        "description": body,
                        "color": COLOR_PRERELEASE if is_pre else COLOR_RELEASE,
                        "footer": {"text": f"Tag: {tag_name}"},
                        "timestamp": latest_rel.get("published_at"),
                    }
    
                    active_mention = mention_text if (not is_pre or mention_on_prerelease) else None
                    deliver(
                        webhook_secret,
                        thread_id,
                        apply_embed_config(embed, embed_config, repo, rel_name),
                        content=active_mention or None,
                        components=[{"type": 1, "components": buttons}],
                    )
    
            repo_state['last_release_id'] = latest_rel_id
            state_modified = True

    # 3. POLLING PULL REQUESTS
    pulls = list(github_pages(
        f"/repos/{repo}/pulls",
        params={"state": "all", "sort": "created", "direction": "desc", "per_page": 10},
    ))

    if pulls:
        latest_pr = pulls[0]
        latest_pr_num = int(latest_pr["number"])
        last_seen_pr_num = repo_state["last_pr_number"]

        if is_new_baseline:
            repo_state["last_pr_number"] = latest_pr_num
            state_modified = True
        else:
            baseline_pr = int(last_seen_pr_num) if last_seen_pr_num is not None else 0
            if latest_pr_num > baseline_pr:
                new_prs = [p for p in pulls if int(p["number"]) > baseline_pr]
                for pr in reversed(new_prs):
                    user_obj = pr.get("user")
                    author_login = user_obj.get("login", "Unknown") if user_obj else "Unknown"
                    pr_title = pr.get("title", "")

                    if is_bot_account(user_obj, author_login):
                        continue
                    if is_ignored_title(pr_title, ignored_keywords):
                        continue

                    base_ref = pr.get("base", {}).get("ref", "main")
                    head_ref = pr.get("head", {}).get("ref", "")
                    embed = {
                        "title": f"[{repo}] PR #{pr['number']}: {pr_title[:200]}",
                        "url": pr.get("html_url"),
                        "color": COLOR_PR,
                        "description": f"**Target:** `{base_ref}` ← `{head_ref}`",
                        "footer": {"text": f"Opened by: {author_login}"},
                        "timestamp": pr.get("created_at"),
                    }
                    deliver(webhook_secret, thread_id, apply_embed_config(embed, embed_config, repo, pr_title))

                repo_state["last_pr_number"] = latest_pr_num
                state_modified = True

    if is_new_baseline:
        print(f"[BASELINE] {state_key} dicatat sebagai baseline awal tanpa notifikasi.")

    return {state_key: repo_state} if state_modified else None


def main() -> None:
    if not MASTER_API_URL or not MASTER_API_KEY:
        print("[FATAL] MASTER_API_URL dan MASTER_API_KEY wajib diatur di GitHub Secrets.")
        sys.exit(1)

    parsed = urlsplit(MASTER_API_URL)
    if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise WorkerError('MASTER_API_URL wajib HTTPS tanpa kredensial/query/fragment')
    print(f"[START] Menjalankan worker '{WORKER_ID}'...")
    repos = fetch_worker_repos()
    if not repos:
        print(f"[INFO] Tidak ada repositori aktif yang ditugaskan ke '{WORKER_ID}'.")
        push_central_state({})
        return

    central_state = fetch_central_state()
    updated_states: dict[str, Any] = {}

    failed = False
    for idx, repo_cfg in enumerate(repos):
        repo_name = repo_cfg.get("repo", "unknown")
        try:
            delta = process_repository(repo_cfg, central_state)
            if delta:
                updated_states.update(delta)
        except Exception as exc:
            failed = True
            print(f"[ERROR] Gagal memproses repositori ({type(exc).__name__}).")

        if idx < len(repos) - 1:
            time.sleep(1)

    push_central_state(updated_states)
    if failed:
        raise WorkerError("Satu atau lebih repositori gagal")
    print(f"[DONE] Worker '{WORKER_ID}' selesai memproses {len(repos)} repositori.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"[FATAL] Worker gagal ({type(exc).__name__}); periksa konfigurasi/layanan.")
        sys.exit(1)
