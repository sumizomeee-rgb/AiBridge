import { Buffer } from "node:buffer";

const WASM_URL = "https://fe-static.deepseek.com/chat/static/sha3_wasm_bg.7b9ca65ddd.wasm";

async function readInput() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  return JSON.parse(Buffer.concat(chunks).toString("utf8"));
}

function writeString(exports, value) {
  const bytes = new TextEncoder().encode(value);
  const pointer = exports.__wbindgen_export_0(bytes.length, 1);
  new Uint8Array(exports.memory.buffer).set(bytes, pointer);
  return [pointer, bytes.length];
}

async function solve(challenge) {
  if (challenge.algorithm !== "DeepSeekHashV1") {
    throw new Error(`不支持的 PoW 算法：${challenge.algorithm}`);
  }
  const response = await fetch(WASM_URL);
  if (!response.ok) throw new Error(`下载官网 PoW 模块失败（HTTP ${response.status}）`);
  const { instance } = await WebAssembly.instantiate(await response.arrayBuffer(), { wbg: {} });
  const exports = instance.exports;
  const resultPointer = exports.__wbindgen_add_to_stack_pointer(-16);
  try {
    const [challengePointer, challengeLength] = writeString(exports, challenge.challenge);
    const prefix = `${challenge.salt}_${challenge.expire_at}_`;
    const [prefixPointer, prefixLength] = writeString(exports, prefix);
    exports.wasm_solve(
      resultPointer,
      challengePointer,
      challengeLength,
      prefixPointer,
      prefixLength,
      Number(challenge.difficulty),
    );
    const view = new DataView(exports.memory.buffer);
    if (view.getInt32(resultPointer, true) === 0) throw new Error("官网 PoW 模块没有找到答案");
    return Math.trunc(view.getFloat64(resultPointer + 8, true));
  } finally {
    exports.__wbindgen_add_to_stack_pointer(16);
  }
}

try {
  const challenge = await readInput();
  const answer = await solve(challenge);
  const payload = {
    algorithm: challenge.algorithm,
    challenge: challenge.challenge,
    salt: challenge.salt,
    answer,
    signature: challenge.signature,
    target_path: challenge.target_path,
  };
  process.stdout.write(Buffer.from(JSON.stringify(payload)).toString("base64"));
} catch (error) {
  process.stderr.write(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
}
