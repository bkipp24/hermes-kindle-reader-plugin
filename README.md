# Hermes Kindle Reader Plugin

A Hermes Agent + Desktop plugin for continuous buffered playback of browser-document read-aloud sessions using Kokoro TTS.

## Install with a remote Hermes backend

In Hermes Desktop, open **Settings → Plugins → Install from Git** and enter:

`https://github.com/bkipp24/hermes-kindle-reader-plugin.git`

Select both **Agent** and **Desktop** targets. The desktop half must be installed locally because a remote backend cannot provide desktop plugin files directly.

## Runtime data

The agent-side queue reads `plugin-data/kindle-reader/playlist.json` and its referenced MP3 files from `HERMES_HOME`. Audio and book content are not stored in this repository.
