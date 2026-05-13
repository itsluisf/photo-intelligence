# Running Phase 2 as a background service (launchd)

Phase 2 is designed to run for hours or days, killable and restartable at any time.

## Setup

1. Edit `com.photointelligence.phase2.plist` and replace every `REPLACE_ME` with the absolute path to your repo clone (e.g. `/Users/you/code`).

2. Install:

   ```bash
   cp com.photointelligence.phase2.plist ~/Library/LaunchAgents/
   launchctl load ~/Library/LaunchAgents/com.photointelligence.phase2.plist
   ```

3. Verify it's running:

   ```bash
   launchctl list | grep photointelligence
   tail -f /path/to/photo-intelligence/phase2.log
   ```

## Stopping and starting

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.photointelligence.phase2.plist

# Start
launchctl load ~/Library/LaunchAgents/com.photointelligence.phase2.plist
```

## Heads-up

**Assume the worker may be running.** Before stopping or restarting Ollama, check:

```bash
launchctl list | grep photointelligence
ps aux | grep phase2_gemma
```

Phase 2 calls Ollama for every photo, so restarting Ollama mid-flight produces errors. The worker retries, but it's cleaner to unload Phase 2 first.

**Don't `rm` `phase2.log` while the process is running.** The process holds an open file descriptor and will keep writing to the deleted inode. Rotate instead:

```bash
mv phase2.log phase2.log.old
launchctl unload ~/Library/LaunchAgents/com.photointelligence.phase2.plist
launchctl load   ~/Library/LaunchAgents/com.photointelligence.phase2.plist
```
