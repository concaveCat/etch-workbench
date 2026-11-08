/**
 * Raw WebGL2 baseline for the benchmark report.
 *
 * Draws the same triangle count with a minimal indexed pipeline (position
 * attribute, one directional light, derivative normals) and no vtk.js. It is
 * the floor a software rasteriser can reach on this machine, so the report can
 * separate "viewer overhead" from "SwiftShader is slow".
 */

const VERTEX = `#version 300 es
in vec3 position;
uniform mat4 mvp;
out vec3 vPosition;
void main() {
  vPosition = position;
  gl_Position = mvp * vec4(position, 1.0);
}`;

const FRAGMENT = `#version 300 es
precision highp float;
in vec3 vPosition;
out vec4 outColor;
void main() {
  vec3 normal = normalize(cross(dFdx(vPosition), dFdy(vPosition)));
  float diffuse = max(0.0, dot(normal, normalize(vec3(0.35, 0.4, 1.0))));
  outColor = vec4(vec3(0.25, 0.45, 0.85) * (0.35 + 0.65 * diffuse), 1.0);
}`;

function heightAt(x: number, y: number): number {
  return 0.55 * Math.sin(x * 1.4) * Math.cos(y * 1.1) + 0.3 * Math.sin((x + y) * 1.9);
}

export type BaselineResult = { triangles: number; width: number; height: number; avgMs: number };

export function webglBaseline(width: number, height: number, triangles: number, frames = 8): BaselineResult {
  const grid = Math.max(8, Math.round(Math.sqrt(triangles / 2)));
  const side = grid + 1;
  const positions = new Float32Array(side * side * 3);
  for (let j = 0; j < side; j++) {
    for (let i = 0; i < side; i++) {
      const x = -4 + (8 * i) / grid;
      const y = -4 + (8 * j) / grid;
      const index = (j * side + i) * 3;
      positions[index] = x;
      positions[index + 1] = y;
      positions[index + 2] = heightAt(x, y);
    }
  }
  const indices = new Uint32Array(grid * grid * 6);
  let write = 0;
  for (let j = 0; j < grid; j++) {
    for (let i = 0; i < grid; i++) {
      const a = j * side + i;
      const b = a + 1;
      const c = a + side;
      const d = c + 1;
      indices[write++] = a;
      indices[write++] = c;
      indices[write++] = b;
      indices[write++] = b;
      indices[write++] = c;
      indices[write++] = d;
    }
  }

  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(width));
  canvas.height = Math.max(1, Math.round(height));
  const gl = canvas.getContext('webgl2', { antialias: false, alpha: false, depth: true });
  if (!gl) return { triangles: 0, width, height, avgMs: 0 };

  const compile = (type: number, source: string): WebGLShader => {
    const shader = gl.createShader(type)!;
    gl.shaderSource(shader, source);
    gl.compileShader(shader);
    return shader;
  };
  const program = gl.createProgram()!;
  gl.attachShader(program, compile(gl.VERTEX_SHADER, VERTEX));
  gl.attachShader(program, compile(gl.FRAGMENT_SHADER, FRAGMENT));
  gl.linkProgram(program);
  gl.useProgram(program);

  gl.bindBuffer(gl.ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ARRAY_BUFFER, positions, gl.STATIC_DRAW);
  gl.bindBuffer(gl.ELEMENT_ARRAY_BUFFER, gl.createBuffer());
  gl.bufferData(gl.ELEMENT_ARRAY_BUFFER, indices, gl.STATIC_DRAW);
  const location = gl.getAttribLocation(program, 'position');
  gl.enableVertexAttribArray(location);
  gl.vertexAttribPointer(location, 3, gl.FLOAT, false, 0, 0);
  gl.uniformMatrix4fv(gl.getUniformLocation(program, 'mvp'), false, new Float32Array([
    0.22, 0.06, 0, 0,
    0, 0.19, 0, 0,
    0.18, 0.18, 0.3, 0,
    0, 0, 0, 1,
  ]));
  gl.enable(gl.DEPTH_TEST);
  gl.viewport(0, 0, canvas.width, canvas.height);
  const pixel = new Uint8Array(4);
  const draw = (): void => {
    gl.clearColor(0.96, 0.97, 0.97, 1);
    gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
    gl.drawElements(gl.TRIANGLES, indices.length, gl.UNSIGNED_INT, 0);
    // Forces the pipeline to finish, exactly like the vtk frame measurement.
    gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, pixel);
  };
  for (let i = 0; i < 3; i++) draw();
  const started = performance.now();
  for (let i = 0; i < frames; i++) draw();
  const avgMs = (performance.now() - started) / frames;
  return { triangles: indices.length / 3, width: canvas.width, height: canvas.height, avgMs };
}
