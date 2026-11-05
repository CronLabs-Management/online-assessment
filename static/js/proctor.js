// Browser-side camera checks. Only event metadata and occasional evidence images leave the browser.
// No continuous video is uploaded or recorded.

const WASM_BASE = "https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/wasm";
const MODEL_URL =
  "https://storage.googleapis.com/mediapipe-models/face_landmarker/face_landmarker/float16/1/face_landmarker.task";

const SAMPLE_MS = 600;
const READY_FRAMES = 5;             // about 3 seconds of a single stable face before entry
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
  constructor({ video, missingFaceSeconds = 5, onEvent, onStatus, onReadyChange, onFaceStatus }) {
    this.video = video;
    this.missingFaceSeconds = missingFaceSeconds;
    this.onEvent = onEvent;
    this.onStatus = onStatus;
    this.onReadyChange = onReadyChange;
    this.onFaceStatus = onFaceStatus || (() => {});
    this.stream = null;
    this.landmarker = null;
    this.timer = null;
    this.readyRequired = false;
    this.readyCount = 0;
    this.reportedReady = false;
    this.missingSince = null;
    this.missingReported = false;
    this.turnSince = null;
    this.multiSince = null;
    this.lastMultiEvent = 0;
    this.lastVideoTime = -1;
    this.stalledSince = null;
    this.cameraLostReported = false;
    this.trackEndedHandler = null;

    // Optical canvas for real-time presence, darkness, and covered camera verification
    this.analysisCanvas = document.createElement("canvas");
    this.analysisCanvas.width = 64;
    this.analysisCanvas.height = 48;
    this.samplingInProgress = false;

    // Native browser FaceDetector (Chromium GPU accelerated, local, instant)
    this.nativeDetector = null;
    if (typeof window !== "undefined" && typeof window.FaceDetector !== "undefined") {
      try {
        this.nativeDetector = new window.FaceDetector({ fastMode: true, maxDetectedFaces: 2 });
      } catch (_) {
        this.nativeDetector = null;
      }
    }
  }

  get running() {
    return this.timer !== null;
  }

  async start() {
    if (this.running || this.starting) return;
    this.starting = true;
    try {
      // Release any stream from an earlier attempt so the camera resets cleanly
      this.stop();
      this.onStatus("Requesting camera access…");

      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        this.onStatus("Camera API not supported in this browser. Please use Chrome, Edge, or Firefox.");
        this.onEvent("camera_revoked", 0, { reason: "unsupported" });
        const err = new Error("getUserMedia not supported");
        throw err;
      }

      try {
        this.stream = await navigator.mediaDevices.getUserMedia({
          video: { width: 640, height: 480, facingMode: "user" },
          audio: false,
        });
      } catch (err) {
        const reason = err && err.name === "NotAllowedError" ? "permission denied" : (err && err.name) || "unavailable";
        this.onStatus(`Camera ${reason}. Allow camera access in your browser address bar, then press Retry.`);
        this.onEvent("camera_revoked", 0, { reason });
        throw err;
      }

      // Configure video attributes required by modern browser autoplay policies
      this.video.muted = true;
      this.video.defaultMuted = true;
      this.video.playsInline = true;
      this.video.setAttribute("muted", "");
      this.video.setAttribute("playsinline", "");
      this.video.setAttribute("autoplay", "");
      this.video.srcObject = this.stream;

      // Safely await video element metadata
      await new Promise((resolve) => {
        if (this.video.readyState >= 1) {
          resolve();
        } else {
          const onLoaded = () => {
            this.video.removeEventListener("loadeddata", onLoaded);
            this.video.removeEventListener("loadedmetadata", onLoaded);
            resolve();
          };
          this.video.addEventListener("loadeddata", onLoaded);
          this.video.addEventListener("loadedmetadata", onLoaded);
          setTimeout(resolve, 800);
        }
      });

      try {
        await this.video.play();
      } catch (err) {
        console.warn("video.play() warning:", err);
        if (this.video.paused) {
          try {
            await this.video.play();
          } catch (_) {}
        }
      }

      const track = this.stream.getVideoTracks()[0];
      if (track) {
        this.trackEndedHandler = () => this.onEvent("camera_revoked", 0, { reason: "track_ended" });
        track.addEventListener("ended", this.trackEndedHandler);
      }

      if (!this.timer) {
        this.timer = setInterval(() => this.sample(), SAMPLE_MS);
      }
      this.onStatus("Camera active");

      // Asynchronously load FaceLandmarker in background without blocking or killing the stream
      this.loadLandmarker();
    } finally {
      this.starting = false;
    }
  }

  async loadLandmarker() {
    try {
      const vision = await import("https://cdn.jsdelivr.net/npm/@mediapipe/tasks-vision@0.10.14/vision_bundle.mjs").catch(() => null);
      if (!vision) {
        console.warn("FaceLandmarker bundle unavailable; continuing with native/optical detection.");
        return;
      }
      const { FilesetResolver, FaceLandmarker } = vision;
      if (!FilesetResolver || !FaceLandmarker) return;
      const fileset = await FilesetResolver.forVisionTasks(WASM_BASE);
      this.landmarker = await this.createLandmarker(fileset, FaceLandmarker);
    } catch (err) {
      console.warn("FaceLandmarker unavailable, continuing with live stream:", err);
      this.landmarker = null;
      this.onEvent("ai_unavailable", 0, { stage: "model_load" });
    }
  }

  async createLandmarker(fileset, FaceLandmarker) {
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

  confirmReady() {
    if (!this.reportedReady) {
      this.reportedReady = true;
      this.readyRequired = false;
      this.onReadyChange(true);
    }
  }

  checkOpticalPresence(video) {
    try {
      const ctx = this.analysisCanvas.getContext("2d", { willReadFrequently: true });
      if (!ctx) return { ok: true };
      ctx.drawImage(video, 0, 0, 64, 48);
      const img = ctx.getImageData(0, 0, 64, 48).data;
      let totalLum = 0;
      const totalPixels = img.length / 4;
      for (let i = 0; i < img.length; i += 4) {
        totalLum += (img[i] * 0.299 + img[i + 1] * 0.587 + img[i + 2] * 0.114);
      }
      const meanLum = totalLum / totalPixels;
      // Camera covered by hand/tape/dark room
      if (meanLum < 16) return { ok: false, reason: "camera_covered" };
      // Camera blinded by intense glare/light
      if (meanLum > 245) return { ok: false, reason: "camera_blinded" };

      // Compute standard deviation / texture variance
      let varianceSum = 0;
      for (let i = 0; i < img.length; i += 4) {
        const lum = (img[i] * 0.299 + img[i + 1] * 0.587 + img[i + 2] * 0.114);
        const diff = lum - meanLum;
        varianceSum += diff * diff;
      }
      const variance = Math.sqrt(varianceSum / totalPixels);
      // Uniform/blank image (finger or object directly covering camera) has nearly 0 variance
      if (variance < 6.0) return { ok: false, reason: "camera_covered" };

      return { ok: true, meanLum, variance };
    } catch (_) {
      return { ok: true };
    }
  }

  async sample() {
    if (this.samplingInProgress) return;
    const video = this.video;
    if (!this.stream || video.readyState < 2) return;
    this.samplingInProgress = true;

    try {
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

      // Optical verification: detect covered camera, black lens, or blinding glare
      const optical = this.checkOpticalPresence(video);
      if (!optical.ok) {
        if (this.missingSince === null) this.missingSince = now;
        this.onFaceStatus({ status: "missing", count: 0, missingMs: now - this.missingSince });
        if (!this.missingReported && now - this.missingSince >= this.missingFaceSeconds * 1000) {
          this.missingReported = true;
          this.onEvent("face_missing", now - this.missingSince, { reason: optical.reason });
        }
        return;
      }

      let faces = null;
      if (this.landmarker) {
        try {
          const result = this.landmarker.detectForVideo(video, now);
          faces = result.faceLandmarks || [];
        } catch (_) {
          faces = null;
        }
      } else if (this.nativeDetector) {
        try {
          const detected = await this.nativeDetector.detect(video);
          faces = (detected || []).map(() => ({}));
        } catch (_) {
          faces = null;
        }
      }

      // Optical face classifier fallback if AI landmarker is loading or unavailable
      if (faces === null) {
        faces = this.classifyOpticalFace();
      }

      if (faces !== null) {
        const count = faces.length;

        if (count === 0) {
          if (this.missingSince === null) this.missingSince = now;
          this.onFaceStatus({ status: "missing", count: 0, missingMs: now - this.missingSince });
          if (!this.missingReported && now - this.missingSince >= this.missingFaceSeconds * 1000) {
            this.missingReported = true;
            this.onEvent("face_missing", now - this.missingSince, {});
          }
        } else if (this.missingSince !== null) {
          const duration = now - this.missingSince;
          this.missingSince = null;
          this.missingReported = false;
          if (duration >= this.missingFaceSeconds * 1000) {
            this.onEvent("face_missing", duration, {});
          }
        }

        if (count > 1) {
          if (this.multiSince === null) this.multiSince = now;
          this.onFaceStatus({ status: "multiple", count });
          if (now - this.multiSince >= MULTI_FACE_SECONDS * 1000 && now - this.lastMultiEvent > MULTI_FACE_COOLDOWN_MS) {
            this.lastMultiEvent = now;
            this.onEvent("multiple_faces", now - this.multiSince, { faces: count });
          }
        } else {
          this.multiSince = null;
        }

        if (count === 1) {
          let turned = false;
          if (faces[0] && faces[0].isTurned !== undefined) {
            turned = faces[0].isTurned;
          } else if (Array.isArray(faces[0]) && faces[0].length > 263) {
            turned = Math.abs(yawOf(faces[0])) > HEAD_TURN_THRESHOLD;
          }
          if (turned) {
            if (this.turnSince === null) this.turnSince = now;
            this.onFaceStatus({ status: "turned", count: 1 });
          } else {
            this.onFaceStatus({ status: "ok", count: 1 });
          }
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
      } else {
        // Optical check passed, camera feed is active and clear
        this.onFaceStatus({ status: "ok", count: 1 });
        if (this.readyRequired && !this.reportedReady) {
          this.readyCount += 1;
          if (this.readyCount >= READY_FRAMES) {
            this.reportedReady = true;
            this.readyRequired = false;
            this.onReadyChange(true);
          }
        }
      }
    } finally {
      this.samplingInProgress = false;
    }
  }

  classifyOpticalFace() {
    try {
      const ctx = this.analysisCanvas.getContext("2d", { willReadFrequently: true });
      if (!ctx) return [{ isTurned: false }];
      if (this.video && this.video.readyState >= 2) {
        ctx.drawImage(this.video, 0, 0, 64, 48);
      }
      const img = ctx.getImageData(0, 0, 64, 48).data;

      let skinPixels = 0;
      let centerSkinPixels = 0;
      let sumX = 0;

      for (let y = 0; y < 48; y++) {
        for (let x = 0; x < 64; x++) {
          const idx = (y * 64 + x) * 4;
          const r = img[idx];
          const g = img[idx + 1];
          const b = img[idx + 2];

          // YCbCr & RGB human skin chrominance
          const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
          const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
          const isSkin = (r > 42 && g > 28 && b > 15 && r > g && (r - g) >= 6) ||
                         (cb >= 77 && cb <= 132 && cr >= 132 && cr <= 176);

          if (isSkin) {
            skinPixels++;
            if (x >= 14 && x <= 50 && y >= 6 && y <= 42) {
              centerSkinPixels++;
              sumX += x;
            }
          }
        }
      }

      const totalCenterPixels = (50 - 14 + 1) * (42 - 6 + 1); // 1369
      const centerRatio = centerSkinPixels / totalCenterPixels;

      // If under 6% of center has skin tones, candidate face is not present / out of frame
      if (centerRatio < 0.06) {
        return [];
      }

      const avgX = centerSkinPixels > 0 ? (sumX / centerSkinPixels) : 32;
      const yawOffset = (avgX - 32) / 16;
      // Face is turned or partially out of frame if off-center or low center coverage
      const isTurned = Math.abs(yawOffset) > 0.38 || centerRatio < 0.13;

      return [{ isTurned, yaw: yawOffset, partial: centerRatio < 0.13 }];
    } catch (_) {
      return [{ isTurned: false }];
    }
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.timer = null;
    if (this.stream) this.stream.getTracks().forEach((track) => track.stop());
    this.stream = null;
  }
}
