# Hermes Kindle Reader Plugin

A Hermes Agent + Desktop plugin for continuous Kindle read-aloud with Kokoro TTS. Version 0.2 stores an independent queue, cursor, and playback checkpoint for every book.

## Install with a remote Hermes backend

In Hermes Desktop, open **Settings → Plugins → Install from Git** and enter:

`https://github.com/bkipp24/hermes-kindle-reader-plugin.git`

Select both **Agent** and **Desktop** targets. The desktop half must be installed locally because a remote backend cannot provide desktop plugin files directly.

## Use

1. Open a book in Kindle Cloud Reader.
2. In the Kindle Reader pane, choose **Start Current Book** (or **Switch book**).
3. The bound chat detects the current ASIN/title without turning a page.
4. Confirm a book switch in the pane when prompted, then press Play/Resume.

Queues, screen cursors, playback offsets, and generation requests survive Hermes/Desktop restarts. Completed audio is pruned conservatively while two recent chunks are retained for recovery. Automatic low-buffer work remains pinned to the chat chosen by Start/Resume.

## Agent workflow commands

The desktop creates durable requests through the plugin API. The agent claims and completes them with `hermes kindle-reader`:

- `state` / `claim --request-id …`
- `report-detection --request-id … --source-id … --title … --cursor-hash sha256:…`
- `append --request-id … --book-id … --cursor-hash … --next-cursor-hash … --audio-file … --transcript-file … --duration … --label …`
- `fail --request-id … --error …`

## Runtime data

Profile-scoped state lives under `plugin-data/kindle-reader/`:

- `catalog.json` tracks the active book and pending switches.
- `books/<stable-key>/` contains metadata, queue, cursor, playback checkpoint, and chunks.
- `requests/` contains idempotent detect/fill jobs and leases.

Legacy `playlist.json` queues remain readable during migration and are never deleted automatically.
