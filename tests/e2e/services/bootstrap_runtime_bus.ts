/** 从冻结 runtime 的真实描述符初始化本次 bus；不制造 lifecycle 成功消息。 */
import { writeFileSync } from "node:fs";
import {
  BUS_CHANNEL,
  PROTOCOL_DESCRIPTORS,
  registerBusProtocols,
  resolveAgentBusConfig,
} from "/opt/agent-runtime/src/agent-bus.ts";

const config = resolveAgentBusConfig();
if (!config || config.url !== "http://agent-bus:7470" || config.channel !== BUS_CHANNEL) {
  throw new Error("runtime bus bootstrap 只允许本次 agent-bus 与已注入 token");
}

async function request(path: string, body?: unknown, allowMissing = false): Promise<any> {
  const response = await fetch(config!.url + path, {
    method: body === undefined ? "GET" : "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${config!.token}`,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
    signal: AbortSignal.timeout(15000),
  });
  if (allowMissing && response.status === 404) return null;
  if (!response.ok) {
    // 不打印返回 body 或请求头，避免上游错误把凭证写入日志。
    throw new Error(`runtime bus bootstrap ${path} HTTP ${response.status}`);
  }
  return await response.json();
}

const identity = await request("/v1/agents/whoami");
if (identity.agent_id !== "mcp-gateway") {
  throw new Error("runtime bus token 不是本次 bootstrap 的 mcp-gateway 身份");
}
const registered = await registerBusProtocols();
if (registered < PROTOCOL_DESCRIPTORS.length) {
  throw new Error("runtime bus protocol 初始化不完整");
}
const channelPath = `/v1/channels/${BUS_CHANNEL}`;
if (await request(channelPath, undefined, true) === null) {
  await request("/v1/channels", {
    channel_id: BUS_CHANNEL,
    delivery_mode: "fanout",
    visibility: "public",
    refs_required: false,
  });
}
const channel = await request(channelPath);
if (channel.channel_id !== BUS_CHANNEL || channel.visibility !== "public" ||
    channel.delivery_mode !== "fanout" || channel.refs_required !== false || channel.closed_at) {
  throw new Error("runtime bus channel 初始化不一致");
}
const bootstrapId = crypto.randomUUID();
const published = await request(`/v1/channels/${BUS_CHANNEL}/publish`, {
  kind: "message",
  payload: { purpose: "Docker E2E runtime bus bootstrap", bootstrap_id: bootstrapId },
  idempotency_key: `e2e-bootstrap:${bootstrapId}`,
});
const read = await request(
  `/v1/channels/${BUS_CHANNEL}/messages?after_seq=${published.channel_seq - 1}&limit=1`,
);
if (read.messages?.[0]?.message_id !== published.message_id ||
    read.messages[0].sender_agent_id !== identity.agent_id ||
    read.messages[0].payload.bootstrap_id !== bootstrapId) {
  throw new Error("runtime bus bootstrap 消息未能真实回读");
}
const report = {
  service: "agent-bus",
  runtime_bootstrap: "passed",
  agent_id: identity.agent_id,
  channel: BUS_CHANNEL,
  registered_protocols: registered,
  lifecycle_kinds: PROTOCOL_DESCRIPTORS.filter((item) => item.kind.startsWith("agent.run."))
    .map((item) => item.kind),
  probe_message_id: published.message_id,
  lifecycle_verified: false,
};
writeFileSync("/state/runtime-bus-bootstrap.json", JSON.stringify(report, null, 2) + "\n");
console.log(JSON.stringify(report));
