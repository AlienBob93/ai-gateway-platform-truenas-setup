#!/usr/bin/env python3
"""Review a GitHub pull request through the AI gateway (OpenAI-compatible).

Modified for the ai-gateway-platform switch: Ollama-native /api/chat -> gateway /v1.
Requires AIGW_KEY (pr-review team key); OLLAMA_URL now means the gateway base URL.

Posts (or updates) a single pull request comment containing the review.

Dependencies: Python 3.8+ standard library only.

Configuration is read from the environment:

    GH_TOKEN        GitHub token with pull-requests write (github.token)
    REPO            owner/repo holding the pull request
    PR_NUMBER       pull request number
    OLLAMA_URL      base URL of the Ollama API
    MODEL           Ollama model tag            (default: qwen3.8:27b)
    NUM_CTX         context window to request    (default: 65536)
    CHUNK_BYTES     max bytes per diff chunk     (default: 120000)
    MAX_CHUNKS      max chunks to review         (default: 12)
    REQUEST_TIMEOUT per-model-call timeout, sec  (default: 1800)
    GITHUB_API_URL  GitHub API base             (default: https://api.github.com)
"""
import json
import os
import sys
import urllib.error
import urllib.request

MARKER = "<!-- ollama-review -->"

SYSTEM_PROMPT = (
    "You are a meticulous senior software engineer performing a pull request "
    "review.\n"
    "Focus on correctness, bugs, security issues, race conditions, error "
    "handling, performance and maintainability. Prefer concrete, actionable "
    "findings.\n"
    "For every issue, give: a severity (critical, major, minor or nit), the "
    "file and line, a short explanation, and a suggested fix. Do not restate "
    "the diff.\n"
    "If a chunk looks fine, say so in one sentence. Respond in GitHub-flavored "
    "Markdown."
)


def log(msg):
    print(msg, flush=True)


def die(msg):
    sys.stderr.write("::error::%s\n" % msg)
    sys.exit(1)


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        die("%s is required" % name)
    return value


GH_TOKEN = env("GH_TOKEN", required=True)
REPO = env("REPO", required=True)
PR_NUMBER = env("PR_NUMBER", required=True)
OLLAMA_URL = env("AIGW_URL", env("OLLAMA_URL", required=True)).rstrip("/")
AIGW_KEY = env("AIGW_KEY", required=True)
MODEL = env("MODEL", "qwen3.8-27b")
NUM_CTX = int(env("NUM_CTX", "65536"))
CHUNK_BYTES = int(env("CHUNK_BYTES", "120000"))
MAX_CHUNKS = int(env("MAX_CHUNKS", "12"))
REQUEST_TIMEOUT = int(env("REQUEST_TIMEOUT", "1800"))
GITHUB_API = env("GITHUB_API_URL", "https://api.github.com").rstrip("/")


def gh_request(path, method="GET", accept="application/vnd.github+json",
               body=None):
    headers = {
        "Authorization": "Bearer %s" % GH_TOKEN,
        "Accept": accept,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ai-review",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(GITHUB_API + path, data=data,
                                 headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def fetch_diff():
    """Return the unified diff for the pull request."""
    try:
        raw = gh_request("/repos/%s/pulls/%s" % (REPO, PR_NUMBER),
                         accept="application/vnd.github.v3.diff")
        return raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        if exc.code not in (406, 415, 422):
            raise
        log("::warning::diff media type unavailable; using the files API")

    parts = []
    page = 1
    while True:
        arr = json.loads(gh_request(
            "/repos/%s/pulls/%s/files?per_page=100&page=%d"
            % (REPO, PR_NUMBER, page)))
        if not arr:
            break
        for item in arr:
            name = item["filename"]
            patch = item.get("patch") or "(no textual patch available)"
            parts.append("diff --git a/%s b/%s\n%s\n" % (name, name, patch))
        if len(arr) < 100:
            break
        page += 1
    return "".join(parts)


def chunk_diff(text, max_bytes):
    """Split a diff at file boundaries into chunks no larger than max_bytes.

    A single file larger than max_bytes becomes its own truncated chunk."""
    chunks = []
    current = []
    size = 0
    for line in text.splitlines(keepends=True):
        if line.startswith("diff --git ") and current and size >= max_bytes:
            chunks.append("".join(current))
            current = []
            size = 0
        current.append(line)
        size += len(line.encode("utf-8"))
    if current:
        chunks.append("".join(current))
    return [c if len(c.encode("utf-8")) <= max_bytes
            else c.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")
            for c in chunks]


def ollama_reachable():
    try:
        req = urllib.request.Request(
            OLLAMA_URL + "/v1/models",
            headers={"Authorization": "Bearer %s" % AIGW_KEY}, method="GET")
        urllib.request.urlopen(req, timeout=20).read()
        return True
    except Exception:
        return False


def review_chunk(chunk):
    payload = json.dumps({
        "model": MODEL,
        "stream": False,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user",
             "content": "Review the following unified diff:\n\n" + chunk},
        ],
    }).encode("utf-8")
    req = urllib.request.Request(
        OLLAMA_URL + "/v1/chat/completions", data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer %s" % AIGW_KEY}, method="POST")
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read())
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content", "")


def upsert_comment(body):
    comments = json.loads(gh_request(
        "/repos/%s/issues/%s/comments?per_page=100" % (REPO, PR_NUMBER)))
    comment_id = next(
        (c["id"] for c in comments if c.get("body", "").startswith(MARKER)),
        None)
    if comment_id:
        log("Updating review comment %s" % comment_id)
        gh_request("/repos/%s/issues/comments/%s" % (REPO, comment_id),
                   method="PATCH", body={"body": body})
    else:
        log("Creating review comment")
        gh_request("/repos/%s/issues/%s/comments" % (REPO, PR_NUMBER),
                   method="POST", body={"body": body})


def main():
    log("Gateway review: repo=%s pr=#%s model=%s num_ctx=%d"
        % (REPO, PR_NUMBER, MODEL, NUM_CTX))

    if not ollama_reachable():
        die("Cannot reach the AI gateway at %s (check the server and network)" % OLLAMA_URL)

    diff = fetch_diff()
    if not diff.strip():
        log("Empty diff; nothing to review.")
        return

    chunks = chunk_diff(diff, CHUNK_BYTES)
    total = len(chunks)
    log("Diff size: %d bytes; %d chunk(s)" % (len(diff.encode()), total))

    sections = []
    reviewed = 0
    for chunk in chunks:
        if reviewed >= MAX_CHUNKS:
            sections.append(
                "\n_Reached the review limit of %d chunk(s); remaining changes "
                "were skipped._\n" % MAX_CHUNKS)
            break
        reviewed += 1
        log("-> chunk %d/%d (%d bytes)"
            % (reviewed, total, len(chunk.encode())))
        try:
            content = review_chunk(chunk)
        except Exception as exc:  # noqa: BLE001 - report and continue
            log("::warning::model call failed for chunk %d: %s" % (reviewed, exc))
            sections.append("\n### Chunk %d/%d\n\n_Model call failed for this "
                            "chunk._\n" % (reviewed, total))
            continue
        if content:
            sections.append("\n### Chunk %d/%d\n\n%s\n"
                            % (reviewed, total, content))

    if not any(s.strip() for s in sections):
        log("::warning::model produced no review output")
        return

    limit_note = ("_Only the first %d of %d diff chunk(s) were reviewed._"
                  % (reviewed, total)) if total > reviewed else ""
    body = "\n".join([
        MARKER,
        "## 🤖 Ollama review",
        "",
        "**Model:** `%s` · **Context:** `%d` · **Chunks:** %d/%d"
        % (MODEL, NUM_CTX, reviewed, total),
        limit_note,
        "",
        "".join(sections),
    ])

    upsert_comment(body)

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write(body + "\n")

    log("Done.")


if __name__ == "__main__":
    main()
