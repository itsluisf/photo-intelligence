# Configuration

All runtime knobs live in `config.ini` (copy from `config.example.ini`).

## Paths

| Key | Description |
|---|---|
| `PHOTOS_LIBRARY` | Absolute path to `Photos Library.photoslibrary` |
| `DB_PATH` | Where to write `photos_meta.db` |
| `LOG_PATH` | Where Phase 2 writes its log |

## Model

| Key | Default | Notes |
|---|---|---|
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama API endpoint |
| `OLLAMA_MODEL` | `gemma4:e4b` | See "Swapping models" below |
| `NUM_CTX` | `16384` | Context window. Don't go below 16k |
| `NUM_PREDICT` | `400` | Max output tokens. 600+ caused truncation in testing |
| `TEMPERATURE` | `0.3` | Lower = more deterministic descriptions |

## Throughput

| Key | Default | Notes |
|---|---|---|
| `BATCH_SIZE` | `1` | Photos per Ollama call. Stay at 1 for vision models |
| `MAX_RETRIES` | `3` | Per-photo retry count before marking as errored |
| `SLEEP_BETWEEN` | `0` | Throttle in seconds (raise if thermals are a concern) |

## Swapping models

`gemma4:e2b` is the obvious next thing to try if you want speed:

```ini
OLLAMA_MODEL = gemma4:e2b
```

In testing on M2: e2b ran around 17.8s/photo vs e4b at ~13s/photo, which was surprising — the smaller model wasn't faster end-to-end, likely because of overhead dominating compared to compute. Worth retesting on your hardware.

To use a non-Gemma vision model, you'll likely need to adjust the prompt in `src/phase2_gemma.py` — different models want different JSON schemas.

## Tuning for your hardware

- **16GB unified memory (M1/M2):** defaults are good. Don't run other heavy apps.
- **32GB+:** you can try `gemma4:e4b` with larger `NUM_CTX` (up to 32768).
- **8GB:** try `gemma4:e2b` and lower `NUM_CTX` to 8192. Expect swapping.
