# Public Distribution Safety

AI Memory OS separates reusable source code from private runtime memory.

## Recommended separation

Source repository:

    AI-Memory-System/

Private runtime:

    AI-Memory/
      projects/
      sessions/
      checkpoints/
      artifacts/

Private runtime data should never be committed to the source repository.

## Promotion safety

Rehearsal requires an explicit root:

    python3 tools/promote.py --rehearse --root /path/to/AI-Memory

Live promotion requires the target twice:

    python3 tools/promote.py \
      --live \
      --root /path/to/AI-Memory \
      --confirm-live-root /path/to/AI-Memory

A mismatch fails closed.

## Testing

Tests should use:

- temporary HOME
- temporary AI_MEMORY_ROOT
- throwaway Git repositories
- fake/local cloud-storage directories
- no real LaunchAgent mutation

Never point tests at an existing production memory root.
