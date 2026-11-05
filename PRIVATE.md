# Private Plezy build

This fork adds an in-app updater and system messages, controlled from your own panel.
Everything else is the official Plezy, kept up to date automatically.

## What's added

| Where | What |
| --- | --- |
| `lib/panel/` | Checks the panel on launch (and every few hours), shows the update and message pop-ups, downloads and installs the APK |
| `android/.../PanelInstallerPlugin.kt` | Opens Android's installer for the downloaded APK |
| `lib/screens/main_screen.dart` | 2 lines: starts the panel check |
| `PlezyActivity.kt`, `AndroidManifest.xml` | 1 line each: registers the installer, install permission |
| `panel/` | The panel itself (Docker) |
| `.github/workflows/private-*.yml` | Build APKs, build the panel image, daily sync with official Plezy |

The panel check only runs in builds made with `--dart-define=PANEL_URL=…`.

## How an update reaches your friends

1. Every morning **Private sync with Plezy** checks for a new official release and merges it.
2. If there was one, **Private Android build** builds signed arm32 (Firestick) and arm64 APKs,
   attaches them to a GitHub release in this fork and sends them to the panel.
3. In the panel, open **Builds** and press **Publish** (or turn on auto-publish in **Settings**).
4. Next time Plezy opens on a friend's Firestick, it shows the update pop-up. They press **Install**.

To build straight away, run **Private Android build** from the Actions tab.

## GitHub settings (Settings > Secrets and variables > Actions)

Variables:
- `PANEL_URL`: the panel's address, e.g. `https://plezy.daemonhub.tech`

Secrets:
- `PANEL_CI_TOKEN`: same value as `CI_TOKEN` in the panel's `.env`
- `ANDROID_KEYSTORE_BASE64`, `ANDROID_STORE_PASSWORD`, `ANDROID_KEY_PASSWORD`, `ANDROID_KEY_ALIAS`: your signing key.
  **Keep a backup of the key.** If it's lost, every friend has to uninstall and reinstall.
- `SYNC_TOKEN`: a fine-grained GitHub token for this repo with **Contents: Read and write** and
  **Workflows: Read and write**. Without it, syncs that touch workflow files fail.

In the Actions tab, disable the official workflows (**Build**, **CI**, **Update packages**). They need
the developer's own secrets and would only fail here.

## Panel on the server

```sh
mkdir -p /opt/vader-apps/plezy-panel && cd /opt/vader-apps/plezy-panel
# put docker-compose.yml and .env here (from the panel/ folder; .env.example shows what goes in it)
docker compose pull && docker compose up -d
```

Point your reverse proxy at `127.0.0.1:3020`. Update the panel with the same `pull` and `up -d`.

If the image is private on GitHub, either make it public (Packages > plezy-panel > Package settings)
or run `docker login ghcr.io` once with a `read:packages` token.

## If a sync fails

The official release changed one of the files listed above in a way that clashes. The workflow log
lists the files; merge by hand, or ask Claude to do it.
