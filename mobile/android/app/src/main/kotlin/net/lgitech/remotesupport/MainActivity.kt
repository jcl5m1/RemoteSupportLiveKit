package net.lgitech.remotesupport

import io.flutter.embedding.android.FlutterActivity
import io.flutter.embedding.engine.FlutterEngine

class MainActivity : FlutterActivity() {
  private var audioRoute: AudioRoutePlugin? = null

  override fun configureFlutterEngine(flutterEngine: FlutterEngine) {
    super.configureFlutterEngine(flutterEngine)
    audioRoute = AudioRoutePlugin(applicationContext, flutterEngine.dartExecutor.binaryMessenger)
  }

  override fun cleanUpFlutterEngine(flutterEngine: FlutterEngine) {
    audioRoute?.dispose()
    audioRoute = null
    super.cleanUpFlutterEngine(flutterEngine)
  }
}
