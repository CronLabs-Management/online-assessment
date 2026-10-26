// Browser-side camera checks. Only event metadata and occasional evidence images leave the browser.
// No continuous video is uploaded or recorded.
import { FilesetResolver, FaceLandmarker } from "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/+esm";

const WASM_BASE = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm";
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

const SAMPLE_MS = 250;
const READY_FRAMES = 12;            // about 3 seconds of a single stable face before entry
const HEAD_TURN_THRESHOLD = 0.35;   // normalised yaw; larger means the head is clearly turned away
const HEAD_TURN_SECONDS = 5;
const MULTI_FACE_SECONDS = 2;
const MULTI_FACE_COOLDOWN_MS = 20000;
const CAMERA_STALL_SECONDS = 5;

function yawOf(landmarks) {
  const nose = landmarks[1];
  const leftEye = landmarks[263];
  const rightEye = landmarks[33];
  const mid = (leftEye.x + rightEye.x) / 2;
  const span = Math.abs(leftEye.x - rightEye.x) || 1e-6;
  return (nose.x - mid) / span;
}

export class Proctor {
  constructor({ video, missingFaceSeconds = 5, onEvent, onStatus, onReadyChange }) {
    this.video = video;
    this.missingFaceSeconds = missingFaceSeconds;
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.onReadyChange = onReadyChange;
    this.stream = null;
    this.landmarker = null;
    this.timer = null;
    this.readyRequired = false;
    this.readyCount = 0;
    this.reportedReady = false;
    this.missingSince = null;
    this.turnSince = null;
    this.multiSince = null;
    this.lastMultiEvent = 0;
    this.lastVideoTime = -1;
    this.stalledSince = null;
    this.cameraLostReported = false;
    this.trackEndedHandler = null;
  }

  get running() {
    return this.timer !== null;
  }

  async start() {
    if (this.running) return;
    // Release any stream from an earlier attempt (for example, after Retry) so the camera light goes off.
    this.stop();
    this.onStatus("Requesting camera access…");
    try {
      this.stream = await navigator.mediaDevices.getUserMedia({
        video: { width: 640, height: 480, facingMode: "user" },
        audio: false,
      });
    } catch (err) {
      const reason = err && err.name === "NotAllowedError" ? "permission denied" : (err && err.name) || "unavailable";
      this.onStatus(`Camera ${reason}. Allow camera access for this site, then press Retry.`);
      this.onEvent("camera_revoked", 0, { reason });
      throw err;
    }

    this.video.srcObject = this.stream;
    try {
      await this.video.play();
    } catch (err) {
      this.stop();
      this.onStatus("The camera could not start. Press Retry.");
      throw err;
    }

    const track = this.stream.getVideoTracks()[0];
    this.trackEndedHandler = () => this.onEvent("camera_revoked", 0, { reason: "track_ended" });
    track.addEventListener("ended", this.trackEndedHandler);

    this.onStatus("Loading face check…");
    try {
      const fileset = await FilesetResolver.forVisionTasks(WASM_BASE);
      this.landmarker = await this.createLandmarker(fileset);
    } catch (err) {
      // The face check is an aid, not the candidate's fault. Tell them what to do in plain words,
      // record it for the reviewer, and release the camera.
      this.stop();
      this.onEvent("ai_unavailable", 0, { stage: "model_load" });
      this.onStatus("The face check could not start on this device. Check your connection, then press Retry.");
      throw err;
    }

    this.timer = setInterval(() => this.sample(), SAMPLE_MS);
    this.onStatus("Camera ready");
  }

  async createLandmarker(fileset) {
    const options = { runningMode: "VIDEO", numFaces: 2 };
    try {
      return await FaceLandmarker.createFromOptions(fileset, {
        ...options,
        baseOptions: { modelAssetPath: MODEL_URL, delegate: "GPU" },
      });
    } catch (_) {
      return FaceLandmarker.createFromOptions(fileset, {
        ...options,
        baseOptions: { modelAssetPath: MODEL_URL, delegate: "CPU" },
      });
    }
  }

  beginReadinessCheck() {
    this.readyRequired = true;
    this.readyCount = 0;
    this.reportedReady = false;
  }

  sample() {
    const video = this.video;
    if (!this.landmarker || video.readyState < 2) return;
    const now = Date.now();

    // A frozen stream is treated as a camera interruption, not as a face.
    if (video.currentTime === this.lastVideoTime) {
      if (this.stalledSince === null) this.stalledSince = now;
      if (!this.cameraLostReported && now - this.stalledSince > CAMERA_STALL_SECONDS * 1000) {
        this.cameraLostReported = true;
        this.onEvent("camera_lost", now - this.stalledSince, {});
      }
      return;
    }
    this.lastVideoTime = video.currentTime;
    this.stalledSince = null;
    this.cameraLostReported = false;

    let result;
    try {
      result = this.landmarker.detectForVideo(video, now);
    } catch (_) {
      return;
    }
    const faces = result.faceLandmarks || [];
    const count = faces.length;

    if (count === 0) {
      if (this.missingSince === null) this.missingSince = now;
    } else if (this.missingSince !== null) {
      const duration = now - this.missingSince;
      this.missingSince = null;
      if (duration >= this.missingFaceSeconds * 1000) {
        this.onEvent("face_missing", duration, {});
      }
    }

    if (count > 1) {
      if (this.multiSince === null) this.multiSince = now;
      if (now - this.multiSince >= MULTI_FACE_SECONDS * 1000 && now - this.lastMultiEvent > MULTI_FACE_COOLDOWN_MS) {
        this.lastMultiEvent = now;
        this.onEvent("multiple_faces", now - this.multiSince, { faces: count });
      }
    } else {
      this.multiSince = null;
    }

    if (count === 1) {
      const turned = Math.abs(yawOf(faces[0])) > HEAD_TURN_THRESHOLD;
      if (turned && this.turnSince === null) this.turnSince = now;
      if (!turned && this.turnSince !== null) {
        const duration = now - this.turnSince;
        this.turnSince = null;
        if (duration >= HEAD_TURN_SECONDS * 1000) this.onEvent("head_turn", duration, {});
      }
    } else {
      this.turnSince = null;
    }

    if (this.readyRequired && !this.reportedReady) {
      this.readyCount = count === 1 && this.missingSince === null ? this.readyCount + 1 : 0;
      if (this.readyCount >= READY_FRAMES) {
        this.reportedReady = true;
        this.readyRequired = false;
        this.onReadyChange(true);
      }
    }
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }
}
