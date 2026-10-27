import AVFoundation
import AVKit
import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate {
  private var audioRoute: AudioRouteBridge?

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)
    if let registrar = self.registrar(forPlugin: "RemoteSupportAudioRoute") {
      audioRoute = AudioRouteBridge(messenger: registrar.messenger())
      registrar.register(RoutePickerFactory(), withId: "net.lgitech.remotesupport/route_picker")
    }
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }
}

/// Lists and selects call audio devices (lib/meet/audio_route.dart).
///
/// iOS routes call audio through the AVAudioSession: a microphone is chosen
/// with setPreferredInput (a Bluetooth/wired headset mic also brings the call
/// audio to that headset), and the loudspeaker with overrideOutputAudioPort.
/// Output-only devices (AirPods in music mode, AirPlay, car audio) are chosen
/// in the system route picker (RoutePickerFactory below).
final class AudioRouteBridge: NSObject, FlutterStreamHandler {
  private var sink: FlutterEventSink?
  private let method: FlutterMethodChannel
  private let events: FlutterEventChannel

  init(messenger: FlutterBinaryMessenger) {
    method = FlutterMethodChannel(name: "net.lgitech.remotesupport/audio_route", binaryMessenger: messenger)
    events = FlutterEventChannel(name: "net.lgitech.remotesupport/audio_route/events", binaryMessenger: messenger)
    super.init()
    method.setMethodCallHandler { [weak self] call, result in
      self?.handle(call, result: result)
    }
    events.setStreamHandler(self)
    NotificationCenter.default.addObserver(
      self, selector: #selector(routeChanged(_:)),
      name: AVAudioSession.routeChangeNotification, object: nil)
  }

  deinit {
    NotificationCenter.default.removeObserver(self)
  }

  @objc private func routeChanged(_ note: Notification) {
    DispatchQueue.main.async { [weak self] in
      self?.sink?("changed")
    }
  }

  func onListen(withArguments arguments: Any?, eventSink events: @escaping FlutterEventSink) -> FlutterError? {
    sink = events
    return nil
  }

  func onCancel(withArguments arguments: Any?) -> FlutterError? {
    sink = nil
    return nil
  }

  private func handle(_ call: FlutterMethodCall, result: @escaping FlutterResult) {
    let args = call.arguments as? [String: Any]
    let id = args?["id"] as? String ?? ""
    switch call.method {
    case "routes":
      result(routes())
    case "selectInput":
      result(selectInput(uid: id))
    case "selectOutput":
      result(selectOutput(id: id))
    case "clearOutput":
      try? AVAudioSession.sharedInstance().overrideOutputAudioPort(.none)
      result(true)
    default:
      result(FlutterMethodNotImplemented)
    }
  }

  private func typeName(_ port: AVAudioSession.Port) -> String {
    switch port {
    case .builtInMic: return "builtin_mic"
    case .builtInReceiver: return "earpiece"
    case .builtInSpeaker: return "speaker"
    case .bluetoothHFP, .bluetoothA2DP, .bluetoothLE: return "bluetooth"
    case .headphones, .headsetMic: return "wired"
    case .usbAudio: return "usb"
    case .airPlay: return "airplay"
    case .carAudio: return "car"
    default: return "other"
    }
  }

  private var isPhone: Bool { UIDevice.current.userInterfaceIdiom == .phone }

  private func orNull(_ value: String?) -> Any {
    if let value = value { return value }
    return NSNull()
  }

  private func routes() -> [String: Any] {
    let session = AVAudioSession.sharedInstance()
    let available = session.availableInputs ?? []
    let inputs: [[String: Any]] = available.map {
      ["id": $0.uid, "name": $0.portName, "type": typeName($0.portType)]
    }

    var outputs: [[String: Any]] = []
    if isPhone {
      outputs.append(["id": "receiver", "name": "Phone", "type": "earpiece"])
    }
    outputs.append(["id": "speaker", "name": "Speaker", "type": "speaker"])
    // Headsets with a microphone: choosing one prefers its mic, which also
    // moves the call audio to it.
    for port in available where port.portType != .builtInMic {
      outputs.append(["id": "input:" + port.uid, "name": port.portName, "type": typeName(port.portType)])
    }

    var currentId: String?
    if let out = session.currentRoute.outputs.first {
      switch out.portType {
      case .builtInReceiver: currentId = "receiver"
      case .builtInSpeaker: currentId = "speaker"
      default:
        // The output and its mic can have different names (wired:
        // "Headphones" vs "Headset Microphone"), so fall back to the type.
        let headsets = available.filter { $0.portType != .builtInMic }
        if let match = headsets.first(where: { $0.portName == out.portName })
          ?? headsets.first(where: { typeName($0.portType) == typeName(out.portType) }) {
          currentId = "input:" + match.uid
        } else {
          // Output-only device picked in the route picker (A2DP, AirPlay...).
          currentId = "route:" + out.uid
          outputs.append(["id": currentId!, "name": out.portName, "type": typeName(out.portType)])
        }
      }
    }
    return [
      "inputs": inputs,
      "outputs": outputs,
      "input": orNull(session.currentRoute.inputs.first?.uid),
      "output": orNull(currentId),
      "canPickOutput": true,
      "hasRoutePicker": true,
    ]
  }

  private func selectInput(uid: String) -> Bool {
    let session = AVAudioSession.sharedInstance()
    guard let port = session.availableInputs?.first(where: { $0.uid == uid }) else { return false }
    do {
      try session.setPreferredInput(port)
      return true
    } catch {
      return false
    }
  }

  private func selectOutput(id: String) -> Bool {
    let session = AVAudioSession.sharedInstance()
    do {
      if id == "speaker" {
        try session.overrideOutputAudioPort(.speaker)
        return true
      }
      try session.overrideOutputAudioPort(.none)
      if id == "receiver" {
        if let mic = session.availableInputs?.first(where: { $0.portType == .builtInMic }) {
          try session.setPreferredInput(mic)
        }
        return true
      }
      if id.hasPrefix("input:") {
        return selectInput(uid: String(id.dropFirst("input:".count)))
      }
      return true  // "route:..." is already the active route
    } catch {
      return false
    }
  }
}

/// The system audio route picker (AirPods, Bluetooth speakers, AirPlay, car).
final class RoutePickerFactory: NSObject, FlutterPlatformViewFactory {
  func create(withFrame frame: CGRect, viewIdentifier viewId: Int64, arguments args: Any?) -> FlutterPlatformView {
    return RoutePickerPlatformView(frame: frame)
  }

  func createArgsCodec() -> FlutterMessageCodec & NSObjectProtocol {
    return FlutterStandardMessageCodec.sharedInstance()
  }
}

final class RoutePickerPlatformView: NSObject, FlutterPlatformView {
  private let picker: AVRoutePickerView

  init(frame: CGRect) {
    picker = AVRoutePickerView(frame: frame)
    picker.tintColor = .white
    picker.activeTintColor = .systemBlue
    picker.prioritizesVideoDevices = false
    super.init()
  }

  func view() -> UIView {
    return picker
  }
}
