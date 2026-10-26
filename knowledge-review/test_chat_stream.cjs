const assert = require('node:assert/strict');
const {consumeChatStream} = require('./web/static/chat-stream.js');

(async () => {
  for (const status of ['completed', 'waiting_user']) {
    const event = status === 'waiting_user' ? 'clarification' : 'result';
    const value = `event: ${event}\ndata: ${JSON.stringify({turn:{status, control:{question:'选什么？'}}})}\n\nevent: done\ndata: ${JSON.stringify({status})}\n\n`;
    const bytes = new TextEncoder().encode(value);
    // Every UTF-8/SSE boundary, including inside Chinese characters, is exercised.
    for (let split = 0; split <= bytes.length; split++) {
      const response = new Response(new ReadableStream({start(c) {
        c.enqueue(bytes.slice(0, split)); c.enqueue(bytes.slice(split)); c.close();
      }}), {headers:{'Content-Type':'text/event-stream'}});
      const result = await consumeChatStream(response, () => {});
      assert.equal(result.status, status);
    }
  }
  await assert.rejects(consumeChatStream(new Response('event: clarification\ndata: {}\n\n',
    {headers:{'Content-Type':'text/event-stream'}}), () => {}));
  console.log('Chat result/clarification stream boundaries passed.');
})().catch(error => {console.error(error); process.exitCode = 1;});
