// 模拟 Extension 的 WebSocket 客户端，验证 Server 全链路
import WebSocket from 'ws';

const ws = new WebSocket('ws://localhost:1337');

ws.on('open', () => {
  console.log('[Mock] Connected to server');
});

ws.on('message', (raw) => {
  const msg = JSON.parse(raw.toString());

  if (msg.type === 'ping') {
    ws.send(JSON.stringify({ type: 'pong' }));
    return;
  }

  if (msg.type === 'task') {
    console.log('[Mock] Received task:', msg.task_id);
    // 模拟 AI 回复：分 3 个 chunk 返回
    const chunks = ['Hello', ' from', ' mock!'];
    let i = 0;
    const iv = setInterval(() => {
      if (i < chunks.length) {
        ws.send(JSON.stringify({ type: 'chunk', task_id: msg.task_id, delta: chunks[i] }));
        i++;
      } else {
        clearInterval(iv);
        ws.send(JSON.stringify({ type: 'done', task_id: msg.task_id }));
        console.log('[Mock] Task done');
      }
    }, 200);
  }
});
