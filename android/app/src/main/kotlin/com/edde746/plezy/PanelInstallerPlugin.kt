package com.edde746.plezy

// Private-build addition: hands a downloaded update APK to Android's installer.

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.provider.Settings
import androidx.core.content.FileProvider
import io.flutter.embedding.engine.plugins.FlutterPlugin
import io.flutter.embedding.engine.plugins.activity.ActivityAware
import io.flutter.embedding.engine.plugins.activity.ActivityPluginBinding
import io.flutter.plugin.common.MethodChannel
import java.io.File

class PanelInstallerPlugin : FlutterPlugin, ActivityAware {
  private var channel: MethodChannel? = null
  private var activity: Activity? = null

  override fun onAttachedToEngine(binding: FlutterPlugin.FlutterPluginBinding) {
    channel = MethodChannel(binding.binaryMessenger, "plezy_panel/installer").also { ch ->
      ch.setMethodCallHandler { call, result ->
        if (call.method != "install") return@setMethodCallHandler result.notImplemented()
        val path = call.argument<String>("path")
        val act = activity
        if (path == null || act == null) return@setMethodCallHandler result.success("failed")
        result.success(install(act, File(path)))
      }
    }
  }

  private fun install(act: Activity, apk: File): String {
    if (!apk.exists()) return "failed"
    // Android 8+ asks once per app before it may install updates.
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O && !act.packageManager.canRequestPackageInstalls()) {
      return try {
        act.startActivity(
          Intent(Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES, Uri.parse("package:${act.packageName}"))
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
        )
        "needs_permission"
      } catch (_: Exception) {
        // Some TV builds of Android hide that screen; try the installer anyway.
        launchInstaller(act, apk)
      }
    }
    return launchInstaller(act, apk)
  }

  private fun launchInstaller(act: Activity, apk: File): String = try {
    val uri = FileProvider.getUriForFile(act, "${act.packageName}.fileprovider", apk)
    act.startActivity(
      Intent(Intent.ACTION_VIEW)
        .setDataAndType(uri, "application/vnd.android.package-archive")
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK),
    )
    "started"
  } catch (_: Exception) {
    "failed"
  }

  override fun onDetachedFromEngine(binding: FlutterPlugin.FlutterPluginBinding) {
    channel?.setMethodCallHandler(null)
    channel = null
  }

  override fun onAttachedToActivity(binding: ActivityPluginBinding) { activity = binding.activity }
  override fun onDetachedFromActivityForConfigChanges() { activity = null }
  override fun onReattachedToActivityForConfigChanges(binding: ActivityPluginBinding) { activity = binding.activity }
  override fun onDetachedFromActivity() { activity = null }
}
