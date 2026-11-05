// Private-build addition: the update pop-up and system-message pop-ups.

import 'dart:async';

import 'package:flutter/material.dart';

import '../utils/app_logger.dart';
import '../utils/dialogs.dart';
import '../widgets/dialog_action_button.dart';
import 'panel_client.dart';

/// Runs the panel check when the main screen opens, then again every few hours
/// while the app stays open. Pop-ups only appear when the main screen is on top,
/// so nothing interrupts playback.
class PanelPrompts {
  PanelPrompts._();

  static Timer? _timer;
  static bool _busy = false;

  static void start(BuildContext context) {
    if (!PanelClient.isEnabled) return;
    unawaited(_run(context));
  }

  static void stop() {
    _timer?.cancel();
    _timer = null;
  }

  static Future<void> _run(BuildContext context) async {
    if (_busy) return;
    _busy = true;
    Duration next = const Duration(hours: 6);
    try {
      final result = await PanelClient.check();
      if (result != null) {
        next = result.checkInterval;
        if (context.mounted && _onTop(context)) {
          if (result.update case final update?) {
            await showPanelUpdateDialog(context, update, currentVersion: result.currentVersion);
          }
          for (final m in result.messages) {
            if (!context.mounted || !_onTop(context)) break;
            await _showMessage(context, m);
          }
        }
      }
    } catch (e) {
      appLogger.w('Panel prompts failed', error: e);
    } finally {
      _busy = false;
      _timer?.cancel();
      if (context.mounted) _timer = Timer(next, () => _run(context));
    }
  }

  static bool _onTop(BuildContext context) => ModalRoute.of(context)?.isCurrent ?? true;

  static Future<void> _showMessage(BuildContext context, PanelMessage m) async {
    final scheme = Theme.of(context).colorScheme;
    final (IconData icon, Color color) = switch (m.level) {
      'critical' => (Icons.error_outline, scheme.error),
      'warning' => (Icons.warning_amber_rounded, Colors.amber),
      _ => (Icons.campaign_outlined, scheme.primary),
    };
    await showScopedDialog<void>(
      context: context,
      barrierDismissible: m.level != 'critical',
      builder: (ctx) => AlertDialog(
        icon: Icon(icon, color: color, size: 32),
        title: Text(m.title),
        content: m.body.isEmpty
            ? null
            : ConstrainedBox(
                constraints: const BoxConstraints(maxWidth: 520, maxHeight: 360),
                child: SingleChildScrollView(child: Text(m.body)),
              ),
        actions: [
          DialogActionButton(onPressed: () => Navigator.pop(ctx), label: 'OK', isPrimary: true, autofocus: true),
        ],
      ),
    );
    await PanelClient.markMessageSeen(m);
  }
}

Future<void> showPanelUpdateDialog(BuildContext context, PanelUpdate update, {required String currentVersion}) {
  return showScopedDialog<void>(
    context: context,
    barrierDismissible: !update.mandatory,
    builder: (ctx) => PopScope(
      canPop: !update.mandatory,
      child: AlertDialog(
        icon: const Icon(Icons.system_update_alt, size: 32),
        title: Text(update.mandatory ? 'Update required' : 'Update available'),
        content: ConstrainedBox(
          constraints: const BoxConstraints(maxWidth: 520, maxHeight: 380),
          child: SingleChildScrollView(
            child: Column(
              mainAxisSize: MainAxisSize.min,
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Text('Plezy ${update.versionName}', style: Theme.of(ctx).textTheme.titleMedium),
                const SizedBox(height: 4),
                Text(
                  'You have $currentVersion${update.size != null ? ' · ${(update.size! / 1048576).toStringAsFixed(0)} MB download' : ''}',
                  style: Theme.of(ctx).textTheme.bodySmall,
                ),
                if (update.notes.trim().isNotEmpty) ...[
                  const SizedBox(height: 14),
                  Text("What's new", style: Theme.of(ctx).textTheme.labelLarge),
                  const SizedBox(height: 4),
                  Text(update.notes.trim()),
                ],
              ],
            ),
          ),
        ),
        actions: [
          if (!update.mandatory)
            DialogActionButton(
              onPressed: () async {
                await PanelClient.skipUpdate(update);
                if (ctx.mounted) Navigator.pop(ctx);
              },
              label: 'Later',
            ),
          DialogActionButton(
            onPressed: () async {
              final done = await _downloadAndInstall(ctx, update);
              if (done && ctx.mounted && !update.mandatory) Navigator.pop(ctx);
            },
            label: 'Install',
            isPrimary: true,
            autofocus: true,
          ),
        ],
      ),
    ),
  );
}

/// Returns true once Android's installer has opened.
Future<bool> _downloadAndInstall(BuildContext context, PanelUpdate update) async {
  final progress = ValueNotifier<double?>(0);
  final cancel = Completer<void>();
  final messenger = ScaffoldMessenger.maybeOf(context);

  // Progress dialog; "Cancel" stops the download.
  unawaited(
    showScopedDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (ctx) => PopScope(
        canPop: false,
        child: AlertDialog(
          title: const Text('Downloading update'),
          content: ValueListenableBuilder<double?>(
            valueListenable: progress,
            builder: (_, p, _) => Column(
              mainAxisSize: MainAxisSize.min,
              children: [
                LinearProgressIndicator(value: p),
                const SizedBox(height: 10),
                Text(p == null ? 'Downloading…' : '${(p * 100).toStringAsFixed(0)}%'),
              ],
            ),
          ),
          actions: [
            DialogActionButton(
              onPressed: () {
                if (!cancel.isCompleted) cancel.complete();
              },
              label: 'Cancel',
              autofocus: true,
            ),
          ],
        ),
      ),
    ),
  );

  void closeProgress() {
    if (context.mounted) Navigator.of(context).pop();
  }

  try {
    final apk = await PanelClient.download(update, onProgress: (p) => progress.value = p, cancel: cancel);
    closeProgress();
    var outcome = await PanelClient.install(apk);
    if (outcome == PanelInstallOutcome.needsPermission && context.mounted) {
      // Android has opened the "Install unknown apps" screen for Plezy.
      await showScopedDialog<void>(
        context: context,
        builder: (ctx) => AlertDialog(
          title: const Text('One-time permission'),
          content: const Text(
            'Turn on "Allow from this source" for Plezy on the screen that just opened, '
            'then come back here and press Install.',
          ),
          actions: [
            DialogActionButton(onPressed: () => Navigator.pop(ctx), label: 'Install', isPrimary: true, autofocus: true),
          ],
        ),
      );
      outcome = await PanelClient.install(apk);
    }
    if (outcome == PanelInstallOutcome.started) return true;
    messenger?.showSnackBar(
      SnackBar(
        content: Text(
          outcome == PanelInstallOutcome.needsPermission
              ? 'Plezy still needs permission to install updates.'
              : "Couldn't open the installer.",
        ),
      ),
    );
    return false;
  } catch (e) {
    closeProgress();
    if (!PanelClient.isCancelled(e)) {
      appLogger.e('Panel update download failed', error: e);
      messenger?.showSnackBar(SnackBar(content: Text(e is FormatException ? e.message : 'Download failed. Try again later.')));
    }
    return false;
  } finally {
    progress.dispose();
  }
}
