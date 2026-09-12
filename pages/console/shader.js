/* Shader Gradient: real WebGL fragment shader background.
   Fullscreen quad + fbm domain warp, dpr capped at 2, offscreen paused,
   prefers-reduced-motion renders one static frame, CSS fallback when no WebGL. */

const VERT = "attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }";

const FRAG = `precision highp float;
uniform vec2 u_res;
uniform float u_time, u_speed, u_blend, u_grain;
uniform vec3 u_c1, u_c2, u_c3;

vec2 hash2(vec2 p){
  p = vec2(dot(p, vec2(127.1, 311.7)), dot(p, vec2(269.5, 183.3)));
  return fract(sin(p) * 43758.5453) * 2.0 - 1.0;
}
float noise(vec2 p){
  vec2 i = floor(p), f = fract(p), u = f * f * (3.0 - 2.0 * f);
  return mix(mix(dot(hash2(i), f), dot(hash2(i + vec2(1.0, 0.0)), f - vec2(1.0, 0.0)), u.x),
             mix(dot(hash2(i + vec2(0.0, 1.0)), f - vec2(0.0, 1.0)),
                 dot(hash2(i + vec2(1.0, 1.0)), f - vec2(1.0, 1.0)), u.x), u.y);
}
float fbm(vec2 p){
  float v = 0.0, a = 0.5;
  for (int i = 0; i < 5; i++){ v += a * noise(p); p *= 2.0; a *= 0.5; }
  return v;
}
void main(){
  vec2 uv = gl_FragCoord.xy / u_res.xy;
  vec2 q = uv;
  q.x *= u_res.x / u_res.y;
  float t = u_time * 0.05 * u_speed;
  float f1 = fbm(q * 1.5 + vec2(t, -t * 0.5));
  float f2 = fbm(q * 2.0 + vec2(f1 * u_blend - t * 0.3, f1 + t * 0.2));
  float f = fbm(q * 1.2 + f2 * (0.6 + u_blend));
  vec3 base = vec3(0.031, 0.035, 0.051);
  vec3 col = base;
  col = mix(col, u_c1, smoothstep(0.15, 0.75, f + 0.35));
  col = mix(col, u_c2, smoothstep(0.3, 0.9, f2 * 0.5 + 0.5) * 0.6);
  col = mix(col, u_c3, smoothstep(0.4, 1.0, f1 * 0.5 + 0.5) * 0.45);
  float vig = smoothstep(1.2, 0.2, length(uv - 0.5));
  col *= 0.55 + 0.6 * vig;
  col += (fract(sin(dot(uv + t, vec2(12.9898, 78.233))) * 43758.5453) - 0.5) * u_grain * 0.12;
  gl_FragColor = vec4(col, 1.0);
}`;

function hexToRgb01(hex) {
  return [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
}

function mixHex(a, b, t) {
  const ra = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const rb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  const out = ra.map((v, i) => Math.round(v + (rb[i] - v) * t));
  return `#${out.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

function glow(hex) {
  return mixHex(hex, "#ffffff", 0.28);
}

function compile(gl, type, src) {
  const shader = gl.createShader(type);
  gl.shaderSource(shader, src);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

export function initShaderGradient(options = {}) {
  const canvas = document.getElementById("sg-canvas");
  const fallback = document.getElementById("sg-fallback");
  const reduceMotion = Boolean(
    window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );

  const state = {
    color1: options.color1 || "#7c5cff",
    color2: options.color2 || "#22d3ee",
    color3: options.color3 || "#f472b6",
    speed: Number.isFinite(options.speed) ? options.speed : 1.0,
    blend: Number.isFinite(options.blend) ? options.blend : 0.6,
    grain: Number.isFinite(options.grain) ? options.grain : 0.5,
  };

  let controls = null;
  if (!canvas) {
    return { setColors() {} };
  }

  function applyFallback() {
    const root = document.documentElement.style;
    root.setProperty("--sg-c1", glow(state.color1));
    root.setProperty("--sg-c2", glow(state.color2));
    root.setProperty("--sg-c3", glow(state.color3));
    if (fallback) fallback.hidden = false;
  }

  let gl = null;
  try {
    gl = canvas.getContext("webgl", { antialias: false, alpha: false, powerPreference: "high-performance" });
    if (!gl) {
      gl = canvas.getContext("experimental-webgl");
    }
  } catch (err) {
    gl = null;
  }

  if (!gl) {
    canvas.style.display = "none";
    applyFallback();
    return {
      setColors(c1, c2, c3) {
        state.color1 = c1 || state.color1;
        state.color2 = c2 || state.color2;
        state.color3 = c3 || state.color3;
        applyFallback();
      },
    };
  }

  const vs = compile(gl, gl.VERTEX_SHADER, VERT);
  const fs = compile(gl, gl.FRAGMENT_SHADER, FRAG);
  const program = gl.createProgram();
  if (!vs || !fs || !program) {
    canvas.style.display = "none";
    applyFallback();
    return { setColors() {} };
  }
  gl.attachShader(program, vs);
  gl.attachShader(program, fs);
  gl.linkProgram(program);
  if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
    canvas.style.display = "none";
    applyFallback();
    return { setColors() {} };
  }
  gl.useProgram(program);

  const buffer = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(program, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);

  const uRes = gl.getUniformLocation(program, "u_res");
  const uTime = gl.getUniformLocation(program, "u_time");
  const uSpeed = gl.getUniformLocation(program, "u_speed");
  const uBlend = gl.getUniformLocation(program, "u_blend");
  const uGrain = gl.getUniformLocation(program, "u_grain");
  const uC1 = gl.getUniformLocation(program, "u_c1");
  const uC2 = gl.getUniformLocation(program, "u_c2");
  const uC3 = gl.getUniformLocation(program, "u_c3");

  gl.uniform1f(uSpeed, state.speed);
  gl.uniform1f(uBlend, state.blend);
  gl.uniform1f(uGrain, state.grain);

  function pushColors() {
    gl.uniform3fv(uC1, hexToRgb01(glow(state.color1)));
    gl.uniform3fv(uC2, hexToRgb01(glow(state.color2)));
    gl.uniform3fv(uC3, hexToRgb01(glow(state.color3)));
  }
  pushColors();

  function resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.floor(canvas.clientWidth * dpr));
    const h = Math.max(1, Math.floor(canvas.clientHeight * dpr));
    if (canvas.width !== w || canvas.height !== h) {
      canvas.width = w;
      canvas.height = h;
      gl.viewport(0, 0, w, h);
    }
    gl.uniform2f(uRes, canvas.width, canvas.height);
  }

  let rafId = 0;
  let running = false;

  function renderFrame(timeSeconds) {
    gl.uniform1f(uTime, timeSeconds);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }

  function start() {
    if (running || reduceMotion) return;
    running = true;
    const loop = () => {
      if (!running) return;
      renderFrame(performance.now() / 1000);
      rafId = requestAnimationFrame(loop);
    };
    rafId = requestAnimationFrame(loop);
  }

  function stop() {
    running = false;
    if (rafId) {
      cancelAnimationFrame(rafId);
      rafId = 0;
    }
  }

  resize();
  window.addEventListener("resize", resize, { passive: true });
  if (reduceMotion) {
    renderFrame(0);
  } else if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) start();
          else stop();
        });
      },
      { threshold: 0 }
    );
    io.observe(canvas);
  } else {
    start();
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stop();
    else if (!reduceMotion) start();
  });

  controls = {
    setColors(c1, c2, c3) {
      if (c1) state.color1 = c1;
      if (c2) state.color2 = c2;
      if (c3) state.color3 = c3;
      pushColors();
      if (reduceMotion) renderFrame(0);
    },
    setUniforms(next = {}) {
      if (Number.isFinite(next.speed)) {
        state.speed = next.speed;
        gl.uniform1f(uSpeed, state.speed);
      }
      if (Number.isFinite(next.blend)) {
        state.blend = next.blend;
        gl.uniform1f(uBlend, state.blend);
      }
      if (Number.isFinite(next.grain)) {
        state.grain = next.grain;
        gl.uniform1f(uGrain, state.grain);
      }
    },
  };
  return controls;
}
