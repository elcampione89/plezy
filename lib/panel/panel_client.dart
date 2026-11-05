// Private-build addition: talks to your Plezy panel for in-app updates and
// system messages. Kept in its own folder so syncing with upstream rarely clashes.
//
// Switched on only when the build is made with --dart-define=PANEL_URL=https://…

import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:crypto/crypto.dart';
import 'package:device_info_plus/device_info_plus.dart';
import 'package:flutter/services.dart';
import 'package:http/http.dart' as http;
import 'package:package_info_plus/package_info_plus.dart';
import 'package:path_provider/path_provider.dart';
import 'package:uuid/uuid.dart';

import '../services/base_shared_preferences_service.dart';
import '../utils/app_logger.dart';

class PanelUpdate {
  final String versionName;
  final int versionCode;
  final String notes;
  final bool mandatory;
  final int? size;
  final String? sha256;
  final Uri url;

  PanelUpdate.fromJson(Map<String, dynamic> j)
    : versionName = j['version_name'] as String? ?? '',
      versionCode = (j['version_code'] as num?)?.toInt() ?? 0,
      notes = j['notes'] as String? ?? '',
      mandatory = j['mandatory'] == true,
      size = (j['size'] as num?)?.toInt(),
      sha256 = j['sha256'] as String?,
      url = Uri.parse(j['url'] as String);
}

class PanelMessage {
  final String id;
  final String rev;
  final String title;
  final String body;
  final String level; // info | warning | critical
  final bool everyLaunch;

  PanelMessage.fromJson(Map<String, dynamic> j)
    : id = j['id'] as String,
      rev = j['rev'] as String? ?? j['id'] as String,
      title = j['title'] as String? ?? '',
      body = j['body'] as String? ?? '',
      level = j['level'] as String? ?? 'info',
      everyLaunch = j['repeat'] == 'every_launch';
}

class PanelCheckResult {
  final PanelUpdate? update;
  final List<PanelMessage> messages;
  final Duration checkInterval;
  final String currentVersion;

  const PanelCheckResult(this.update, this.messages, this.checkInterval, this.currentVersion);
}

/// What happened when we tried to hand the APK to Android's installer.
enum PanelInstallOutcome { started, needsPermission, failed }

class PanelClient {
  PanelClient._();

  static const String panelUrl = String.fromEnvironment('PANEL_URL');
  static bool get isEnabled => panelUrl.isNotEmpty && Platform.isAndroid;

  static const _channel = MethodChannel('plezy_panel/installer');
  static const _keyDeviceId = 'panel_device_id';
  static const _keySeenMessages = 'panel_seen_messages';

  static String? _abi;
  static String? _model;
  static String? _os;

  static Future<String> _deviceId() async {
    final prefs = await BaseSharedPreferencesService.sharedCache();
    var id = prefs.getString(_keyDeviceId);
    if (id == null) {
      id = const Uuid().v4();
      await prefs.setString(_keyDeviceId, id);
    }
    return id;
  }

  static Future<void> _loadDevice() async {
    if (_abi != null) return;
    try {
      final info = await DeviceInfoPlugin().androidInfo;
      _abi = info.supportedAbis.isNotEmpty ? info.supportedAbis.first : '';
      _model = [info.manufacturer, info.model].where((s) => s.isNotEmpty).join(' ');
      _os = 'Android ${info.version.release}';
    } catch (e) {
      _abi = '';
      appLogger.w('Panel: could not read device info', error: e);
    }
  }

  /// Ask the panel for a newer build and any live messages. Never throws.
  static Future<PanelCheckResult?> check() async {
    if (!isEnabled) return null;
    try {
      await _loadDevice();
      final pkg = await PackageInfo.fromPlatform();
      final uri = Uri.parse('$panelUrl/api/v1/check').replace(
        queryParameters: {
          'vc': pkg.buildNumber,
          'vn': pkg.version,
          'abi': _abi ?? '',
          'device': await _deviceId(),
          'model': _model ?? '',
          'os': _os ?? '',
        },
      );
      final res = await http.get(uri).timeout(const Duration(seconds: 15));
      if (res.statusCode != 200) {
        appLogger.w('Panel check returned HTTP ${res.statusCode}');
        return null;
      }
      final data = jsonDecode(res.body) as Map<String, dynamic>;
      final updateJson = data['update'] as Map<String, dynamic>?;
      var update = updateJson == null ? null : PanelUpdate.fromJson(updateJson);
      final prefs = await BaseSharedPreferencesService.sharedCache();

      // "Later" hides an optional update until the app is next opened.
      if (update != null && !update.mandatory && _laterThisSession == update.versionCode) {
        update = null;
      }

      final seen = prefs.getStringList(_keySeenMessages) ?? const <String>[];
      final shownThisSession = _sessionShown;
      final messages = [
        for (final m in (data['messages'] as List? ?? const []))
          PanelMessage.fromJson(m as Map<String, dynamic>),
      ].where((m) => m.everyLaunch ? !shownThisSession.contains(m.rev) : !seen.contains(m.rev)).toList();

      final hours = (data['check_interval_hours'] as num?)?.toInt() ?? 6;
      return PanelCheckResult(update, messages, Duration(hours: hours.clamp(1, 168)), pkg.version);
    } catch (e) {
      appLogger.w('Panel check failed', error: e);
      return null;
    }
  }

  // "Every launch" messages show once per app start, not on every periodic re-check.
  static final Set<String> _sessionShown = {};

  static Future<void> markMessageSeen(PanelMessage m) async {
    _sessionShown.add(m.rev);
    if (m.everyLaunch) return;
    final prefs = await BaseSharedPreferencesService.sharedCache();
    final seen = [...(prefs.getStringList(_keySeenMessages) ?? const <String>[]), m.rev];
    // Keep the list small; old revisions never come back.
    await prefs.setStringList(_keySeenMessages, seen.length > 200 ? seen.sublist(seen.length - 200) : seen);
  }

  static int? _laterThisSession;

  static Future<void> skipUpdate(PanelUpdate u) async {
    _laterThisSession = u.versionCode;
  }

  /// Download the APK, reporting progress from 0 to 1 (null when the size is unknown).
  static Future<File> download(PanelUpdate u, {void Function(double? progress)? onProgress, Completer<void>? cancel}) async {
    final dir = await getTemporaryDirectory();
    final file = File('${dir.path}/plezy-update.apk');
    if (await file.exists()) await file.delete();

    final client = http.Client();
    try {
      final res = await client.send(http.Request('GET', u.url)).timeout(const Duration(seconds: 30));
      if (res.statusCode != 200) throw HttpException('Download failed (HTTP ${res.statusCode})');
      final total = res.contentLength ?? u.size;
      var received = 0;
      final sink = file.openWrite();
      final digestSink = _DigestSink();
      final hasher = sha256.startChunkedConversion(digestSink);
      try {
        await for (final chunk in res.stream.timeout(const Duration(seconds: 60))) {
          if (cancel?.isCompleted ?? false) throw const _Cancelled();
          sink.add(chunk);
          hasher.add(chunk);
          received += chunk.length;
          onProgress?.call(total != null && total > 0 ? received / total : null);
        }
      } finally {
        await sink.close();
        hasher.close();
      }
      if (u.sha256 != null && digestSink.value?.toString() != u.sha256!.toLowerCase()) {
        await file.delete();
        throw const FormatException('The download was damaged. Please try again.');
      }
      return file;
    } on _Cancelled {
      if (await file.exists()) await file.delete();
      rethrow;
    } finally {
      client.close();
    }
  }

  static Future<PanelInstallOutcome> install(File apk) async {
    try {
      final result = await _channel.invokeMethod<String>('install', {'path': apk.path});
      return switch (result) {
        'started' => PanelInstallOutcome.started,
        'needs_permission' => PanelInstallOutcome.needsPermission,
        _ => PanelInstallOutcome.failed,
      };
    } catch (e) {
      appLogger.e('Panel: could not open the installer', error: e);
      return PanelInstallOutcome.failed;
    }
  }

  static bool isCancelled(Object e) => e is _Cancelled;
}

class _Cancelled implements Exception {
  const _Cancelled();
}

class _DigestSink implements Sink<Digest> {
  Digest? value;
  @override
  void add(Digest data) => value = data;
  @override
  void close() {}
}
