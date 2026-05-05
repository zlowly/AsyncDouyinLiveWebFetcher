# AsyncDouyinLiveWebFetcher

Async douyin live stream danmaku fetcher. Python 3.12+, no packaging (run via `python src/main.py`).

## Quick start

```bash
conda activate AsyncDouyinLiveWebFetcher
pip install -r requirements.txt
```

## Run

| Mode | Command |
|------|---------|
| Watch (SSE) | `python src/main.py` |
| Single room | `python src/main.py -r <room_id>` |

## Lint & format (Ruff only — no black, isort, flake8, mypy)

```bash
ruff check src/          # lint
ruff check --fix src/    # lint + autofix
ruff format src/         # format
```

Config: line-length=79, double quotes everywhere. `ruff format` required before checkin.

## No tests, no CI, no pre-commit, no type checking

No test framework, no test directory, no CI workflows, no pre-commit hooks, no mypy/pyright.

## Architecture

```
main.py → DoyinLiveRoom (liveroom.py) → DouyinChatWebSocketClient (websocket.py) → protobuf/douyin.py
```

Standalone tools in `src/`: `ass.py` (ASS subtitles), `process_log.py` (chat extractor), `extract_and_visualize_log_data.py` (audience metrics).

## Key quirks

- **Watch mode** requires a local [ntfy.sh](https://ntfy.sh/) server at `http://localhost:10380/mytopic/sse` (hardcoded in `main.py:193`). Without it, watch mode will crash.
- **`__ac_nonce` cookie** is hardcoded in `liveroom.py` — may expire, causing connection failures.
- **`sign.js`** is obfuscated Douyin `byted_acrawler` signing code executed via `mini-racer` (V8). May break when Douyin updates their signature algorithm.
- **`protobuf/readme.md`** is outdated (references `betterproto==2.0.0b6`; installed is `>=1.2.5`). Regenerate via: `protoc -I . --python_betterproto_out=. douyin.proto` from `src/protobuf/`.
- **Logs** accumulate in `logs/` — both app-level and per-room stats. Not cleaned automatically.
- **`requests`** in `requirements.txt` is vestigial (original sync project). The app uses `aiohttp`.
