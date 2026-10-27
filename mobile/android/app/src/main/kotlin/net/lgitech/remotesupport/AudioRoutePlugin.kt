package net.lgitech.remotesupport

import android.content.Context
import android.media.AudioDeviceCallback
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import io.flutter.plugin.common.BinaryMessenger
import io.flutter.plugin.common.EventChannel
import io.flutter.plugin.common.MethodCall
import io.flutter.plugin.common.MethodChannel

/**
 * Lists and selects call audio devices (lib/meet/audio_route.dart).
 *
 * Outputs are communication devices: earpiece, speaker, wired, USB, Bluetooth
 * (SCO / LE audio). On Android 12+ a choice is applied with
 * AudioManager.setCommunicationDevice; older versions use the speakerphone and
 * Bluetooth SCO switches. Inputs use flutter_webrtc's device ids, so Dart can
 * pass the same id to Helper.selectAudioInput (WebRTC's preferred recording
 * device); choosing a Bluetooth mic also routes the call to that headset,
 * since Bluetooth call audio (SCO) is two-way.
 *
 * Device hot-plug is reported on the event channel so Dart can refresh its
 * lists and re-apply the user's choice (LiveKit's own audio switch may pick a
 * different device when the set of devices changes).
 */
class AudioRoutePlugin(context: Context, messenger: BinaryMessenger) :
  MethodChannel.MethodCallHandler, EventChannel.StreamHandler {

  private val am = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
  private val main = Handler(Looper.getMainLooper())
  private val method = MethodChannel(messenger, "net.lgitech.remotesupport/audio_route")
  private val events = EventChannel(messenger, "net.lgitech.remotesupport/audio_route/events")
  private var sink: EventChannel.EventSink? = null

  private val callback = object : AudioDeviceCallback() {
    override fun onAudioDevicesAdded(addedDevices: Array<out AudioDeviceInfo>) = changed()
    override fun onAudioDevicesRemoved(removedDevices: Array<out AudioDeviceInfo>) = changed()
  }

  // Android 12+: another component (LiveKit's audio switch) changing the
  // communication device doesn't fire the device callback; listen for it so
  // Dart can notice and re-apply the user's choice.
  private val commListener: Any? =
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      AudioManager.OnCommunicationDeviceChangedListener { changed() }
    } else {
      null
    }

  init {
    method.setMethodCallHandler(this)
    events.setStreamHandler(this)
    am.registerAudioDeviceCallback(callback, main)
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      am.addOnCommunicationDeviceChangedListener(
        { main.post(it) },
        commListener as AudioManager.OnCommunicationDeviceChangedListener,
      )
    }
  }

  fun dispose() {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      am.removeOnCommunicationDeviceChangedListener(
        commListener as AudioManager.OnCommunicationDeviceChangedListener,
      )
    }
    am.unregisterAudioDeviceCallback(callback)
    method.setMethodCallHandler(null)
    events.setStreamHandler(null)
  }

  private fun changed() {
    sink?.success("changed")
  }

  override fun onListen(arguments: Any?, events: EventChannel.EventSink?) {
    sink = events
  }

  override fun onCancel(arguments: Any?) {
    sink = null
  }

  override fun onMethodCall(call: MethodCall, result: MethodChannel.Result) {
    try {
      when (call.method) {
        "routes" -> result.success(routes())
        "selectOutput" -> result.success(selectOutput(call.argument<String>("id") ?: ""))
        "selectInput" -> result.success(selectInput(call.argument<String>("id") ?: ""))
        "clearOutput" -> {
          clearOutput()
          result.success(true)
        }
        else -> result.notImplemented()
      }
    } catch (e: Exception) {
      result.error("audio_route", e.message, null)
    }
  }

  // --- listing -------------------------------------------------------------

  private fun typeName(type: Int): String = when (type) {
    AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "earpiece"
    AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "speaker"
    AudioDeviceInfo.TYPE_BUILTIN_MIC -> "builtin_mic"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO, AudioDeviceInfo.TYPE_BLUETOOTH_A2DP -> "bluetooth"
    AudioDeviceInfo.TYPE_WIRED_HEADSET, AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "wired"
    AudioDeviceInfo.TYPE_USB_HEADSET, AudioDeviceInfo.TYPE_USB_DEVICE -> "usb"
    TYPE_BLE_HEADSET, TYPE_BLE_SPEAKER -> "bluetooth"
    else -> "other"
  }

  private fun label(d: AudioDeviceInfo): String {
    val product = d.productName?.toString()?.trim().orEmpty()
    return when (d.type) {
      AudioDeviceInfo.TYPE_BUILTIN_EARPIECE -> "Phone"
      AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> "Speaker"
      AudioDeviceInfo.TYPE_BUILTIN_MIC -> {
        val where = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) d.address else ""
        if (where.isNullOrBlank()) "Phone microphone" else "Phone microphone ($where)"
      }
      AudioDeviceInfo.TYPE_WIRED_HEADSET -> "Wired headset"
      AudioDeviceInfo.TYPE_WIRED_HEADPHONES -> "Wired headphones"
      else -> product.ifEmpty { typeName(d.type).replaceFirstChar { it.uppercase() } }
    }
  }

  /** Same ids as flutter_webrtc's AudioUtils.getAudioDeviceId. */
  private fun webrtcInputId(d: AudioDeviceInfo): String = when (d.type) {
    AudioDeviceInfo.TYPE_BUILTIN_MIC ->
      "microphone-" + (if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) d.address else "")
    AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired-headset"
    AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth"
    else -> d.id.toString()
  }

  private fun outputDevices(): List<AudioDeviceInfo> {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) return am.availableCommunicationDevices
    val wanted = setOf(
      AudioDeviceInfo.TYPE_BUILTIN_EARPIECE,
      AudioDeviceInfo.TYPE_BUILTIN_SPEAKER,
      AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
      AudioDeviceInfo.TYPE_WIRED_HEADSET,
      AudioDeviceInfo.TYPE_WIRED_HEADPHONES,
      AudioDeviceInfo.TYPE_USB_HEADSET,
    )
    return am.getDevices(AudioManager.GET_DEVICES_OUTPUTS).filter { it.type in wanted }
  }

  @Suppress("DEPRECATION")
  private fun currentOutput(devices: List<AudioDeviceInfo>): AudioDeviceInfo? {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) return am.communicationDevice
    val type = when {
      am.isSpeakerphoneOn -> AudioDeviceInfo.TYPE_BUILTIN_SPEAKER
      am.isBluetoothScoOn -> AudioDeviceInfo.TYPE_BLUETOOTH_SCO
      devices.any { it.type == AudioDeviceInfo.TYPE_WIRED_HEADSET } -> AudioDeviceInfo.TYPE_WIRED_HEADSET
      devices.any { it.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES } -> AudioDeviceInfo.TYPE_WIRED_HEADPHONES
      else -> AudioDeviceInfo.TYPE_BUILTIN_EARPIECE
    }
    return devices.firstOrNull { it.type == type }
  }

  private fun routes(): Map<String, Any?> {
    val outs = outputDevices()
    val current = currentOutput(outs)
    val seen = HashSet<String>()
    val outputs = outs.filter { seen.add(typeName(it.type) + "|" + label(it)) }.map {
      mapOf("id" to it.id.toString(), "name" to label(it), "type" to typeName(it.type))
    }
    val inputTypes = setOf(
      AudioDeviceInfo.TYPE_BUILTIN_MIC,
      AudioDeviceInfo.TYPE_BLUETOOTH_SCO,
      AudioDeviceInfo.TYPE_WIRED_HEADSET,
      AudioDeviceInfo.TYPE_USB_HEADSET,
    )
    val seenIn = HashSet<String>()
    val inputs = am.getDevices(AudioManager.GET_DEVICES_INPUTS)
      .filter { it.type in inputTypes && seenIn.add(webrtcInputId(it)) }
      .map { mapOf("id" to webrtcInputId(it), "name" to label(it), "type" to typeName(it.type)) }
    return mapOf(
      "outputs" to outputs,
      "inputs" to inputs,
      "output" to current?.id?.toString(),
      "canPickOutput" to true,
    )
  }

  // --- selection -----------------------------------------------------------

  @Suppress("DEPRECATION")
  private fun selectOutput(id: String): Boolean {
    val device = outputDevices().firstOrNull { it.id.toString() == id } ?: return false
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) return am.setCommunicationDevice(device)
    when (device.type) {
      AudioDeviceInfo.TYPE_BUILTIN_SPEAKER -> {
        stopSco()
        am.isSpeakerphoneOn = true
      }
      AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> {
        am.isSpeakerphoneOn = false
        am.startBluetoothSco()
        am.isBluetoothScoOn = true
      }
      else -> {
        stopSco()
        am.isSpeakerphoneOn = false
      }
    }
    return true
  }

  /** Bluetooth/wired/USB mics carry the call audio too: route output there. */
  private fun selectInput(id: String): Boolean {
    val input = am.getDevices(AudioManager.GET_DEVICES_INPUTS).firstOrNull { webrtcInputId(it) == id }
      ?: return false
    val pairedType = when (input.type) {
      AudioDeviceInfo.TYPE_BLUETOOTH_SCO -> "bluetooth"
      AudioDeviceInfo.TYPE_WIRED_HEADSET -> "wired"
      AudioDeviceInfo.TYPE_USB_HEADSET -> "usb"
      else -> return true
    }
    val out = outputDevices().firstOrNull { typeName(it.type) == pairedType } ?: return true
    return selectOutput(out.id.toString())
  }

  @Suppress("DEPRECATION")
  private fun stopSco() {
    if (am.isBluetoothScoOn) {
      am.isBluetoothScoOn = false
      am.stopBluetoothSco()
    }
  }

  @Suppress("DEPRECATION")
  private fun clearOutput() {
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.S) {
      am.clearCommunicationDevice()
    } else {
      stopSco()
      am.isSpeakerphoneOn = false
    }
  }

  companion object {
    // AudioDeviceInfo.TYPE_BLE_HEADSET / TYPE_BLE_SPEAKER (API 31); literal so
    // the file compiles against older SDKs too.
    private const val TYPE_BLE_HEADSET = 26
    private const val TYPE_BLE_SPEAKER = 27
  }
}
