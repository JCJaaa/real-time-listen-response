import AVFoundation
import CoreMedia
import Foundation
import ScreenCaptureKit

@available(macOS 13.0, *)
final class AudioOutputHandler: NSObject, SCStreamOutput, SCStreamDelegate {
    private let stdout = FileHandle.standardOutput

    func stream(
        _ stream: SCStream,
        didOutputSampleBuffer sampleBuffer: CMSampleBuffer,
        of type: SCStreamOutputType
    ) {
        guard type == .audio else { return }
        guard CMSampleBufferDataIsReady(sampleBuffer) else { return }

        var blockBuffer: CMBlockBuffer?
        var audioBufferList = AudioBufferList()
        let status = CMSampleBufferGetAudioBufferListWithRetainedBlockBuffer(
            sampleBuffer,
            bufferListSizeNeededOut: nil,
            bufferListOut: &audioBufferList,
            bufferListSize: MemoryLayout<AudioBufferList>.size,
            blockBufferAllocator: nil,
            blockBufferMemoryAllocator: nil,
            flags: 0,
            blockBufferOut: &blockBuffer
        )
        guard status == noErr else { return }

        let buffer = audioBufferList.mBuffers
        guard let rawPtr = buffer.mData, buffer.mDataByteSize > 0 else { return }

        let sampleCount = Int(buffer.mDataByteSize) / MemoryLayout<Float>.size
        let floatPtr = rawPtr.bindMemory(to: Float.self, capacity: sampleCount)

        var pcm16 = [Int16](repeating: 0, count: sampleCount)
        for i in 0..<sampleCount {
            let clamped = max(-1.0 as Float, min(1.0 as Float, floatPtr[i]))
            pcm16[i] = Int16(clamped * 32767.0)
        }

        pcm16.withUnsafeBytes { bytes in
            stdout.write(Data(bytes))
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        fputs("SCStream stopped with error: \(error.localizedDescription)\n", stderr)
        exit(1)
    }
}

@available(macOS 13.0, *)
@main
struct AudioTapCLI {
    static func main() async {
        let args = CommandLine.arguments
        var appFilter: String? = nil
        var listApps = false

        var i = 1
        while i < args.count {
            switch args[i] {
            case "--app":
                if i + 1 < args.count {
                    appFilter = args[i + 1]
                    i += 1
                }
            case "--list-apps":
                listApps = true
            default:
                break
            }
            i += 1
        }

        do {
            let content = try await SCShareableContent.excludingDesktopWindows(
                false,
                onScreenWindowsOnly: false
            )

            if listApps {
                for app in content.applications {
                    print("\(app.applicationName) (\(app.bundleIdentifier))")
                }
                return
            }

            guard let display = content.displays.first else {
                fputs("Error: No display found for ScreenCaptureKit.\n", stderr)
                exit(1)
            }

            let filter: SCContentFilter
            if let query = appFilter, !query.isEmpty {
                let matched = content.applications.filter {
                    $0.applicationName.localizedCaseInsensitiveContains(query)
                        || $0.bundleIdentifier.localizedCaseInsensitiveContains(query)
                }
                if matched.isEmpty {
                    fputs(
                        "Warning: App '\(query)' not found; falling back to all system audio.\n",
                        stderr
                    )
                    filter = SCContentFilter(
                        display: display,
                        excludingApplications: [],
                        exceptingWindows: []
                    )
                } else {
                    let names = matched.map { $0.applicationName }.joined(separator: ", ")
                    fputs("Capturing audio from app(s): \(names)\n", stderr)
                    filter = SCContentFilter(
                        display: display,
                        including: matched,
                        exceptingWindows: []
                    )
                }
            } else {
                fputs("Capturing all macOS system audio.\n", stderr)
                filter = SCContentFilter(
                    display: display,
                    excludingApplications: [],
                    exceptingWindows: []
                )
            }

            let config = SCStreamConfiguration()
            config.capturesAudio = true
            config.excludesCurrentProcessAudio = true
            config.sampleRate = 16000
            config.channelCount = 1
            config.width = 2
            config.height = 2
            config.minimumFrameInterval = CMTime(value: 1, timescale: 1)

            let handler = AudioOutputHandler()
            let stream = SCStream(filter: filter, configuration: config, delegate: handler)
            try stream.addStreamOutput(
                handler,
                type: .audio,
                sampleHandlerQueue: DispatchQueue(label: "com.teams.audiotap")
            )
            try await stream.startCapture()

            // Keep alive until terminated by parent process.
            while true {
                try await Task.sleep(nanoseconds: 1_000_000_000)
            }
        } catch {
            fputs(
                "ScreenCaptureKit error: \(error.localizedDescription)\n"
                + "Make sure 'Screen & System Audio Recording' permission is granted in System Settings.\n",
                stderr
            )
            exit(1)
        }
    }
}
