<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { fetchEventSource } from "@microsoft/fetch-event-source";
import {
  AlertTriangle,
  Bot,
  Check,
  CircleCheck,
  CircleDot,
  Copy,
  LoaderCircle,
  PanelLeftClose,
  PanelLeftOpen,
  Send,
  Square,
} from "@lucide/vue";
import { marked } from "marked";
import DOMPurify from "dompurify";
import KlineChart from "./components/KlineChart.vue";

marked.setOptions({ breaks: true, gfm: true });

const agentDefinitions = [
  {
    id: "fundamental",
    name: "基本面 Agent",
    tone: "badge-success",
    accent: "agent-fundamental",
  },
];

const agents = reactive(
  Object.fromEntries(
    agentDefinitions.map((agent) => [
      agent.id,
      {
        progress: [],
        status: "idle",
        updatedAt: "",
      },
    ])
  )
);

const userInput = ref("帮我看看茅台(600519)这只股票值得投资吗");
const isRunning = ref(false);
const runPhase = ref("idle");
const errorMessage = ref("");
const health = ref(null);
const sidebarCollapsed = ref(false);
const activeConversationId = ref("welcome");
const conversations = ref([
  {
    id: "welcome",
    title: "新的投资咨询",
    status: "ready",
    time: new Date().toLocaleTimeString(),
  },
]);
const archivedTurns = ref([]);
const currentPrompt = ref("");
const currentBlocks = ref([]);
const copiedAgentId = ref("");

let runAbortController = null;
let archivedTurnCounter = 0;
let renderedToolCallIds = new Set();

const canSend = computed(() => userInput.value.trim().length > 0 && !isRunning.value);

const activeConversation = computed(() => {
  return conversations.value.find((item) => item.id === activeConversationId.value) || conversations.value[0];
});

const statusBadgeText = computed(() => {
  if (isRunning.value) return runPhase.value;
  if (runPhase.value === "offline") return "offline";
  return "ready";
});

const statusBadgeClass = computed(() => {
  if (isRunning.value) return "badge-primary";
  if (runPhase.value === "offline") return "badge-warning";
  return "badge-neutral";
});

function renderMarkdown(value) {
  if (!value) return "";
  return DOMPurify.sanitize(marked.parse(value));
}

function statusLabel(status) {
  if (status === "waiting") return "等待";
  if (status === "streaming") return "输出中";
  if (status === "done") return "完成";
  if (status === "error") return "异常";
  return "空闲";
}

function statusClass(status) {
  if (status === "streaming") return "badge-primary";
  if (status === "done") return "badge-success";
  if (status === "error") return "badge-error";
  if (status === "waiting") return "badge-ghost";
  return "badge-neutral";
}

function statusIcon(status) {
  if (status === "streaming") return LoaderCircle;
  if (status === "done") return CircleCheck;
  if (status === "error") return AlertTriangle;
  return CircleDot;
}

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || `Request failed: ${response.status}`);
  return body;
}

async function loadHealth() {
  health.value = await requestJson("/api/health");
}

function upsertConversation(title, status = "running") {
  const id = crypto.randomUUID();
  conversations.value.unshift({
    id,
    title,
    status,
    time: new Date().toLocaleTimeString(),
  });
  activeConversationId.value = id;
  return id;
}

function updateActiveConversationStatus(status) {
  const item = conversations.value.find((conversation) => conversation.id === activeConversationId.value);
  if (!item) return;
  item.status = status;
  item.time = new Date().toLocaleTimeString();
}

function prepareActiveConversation(prompt) {
  const item = conversations.value.find((conversation) => conversation.id === activeConversationId.value);
  if (!item) {
    return upsertConversation(prompt);
  }

  if (item.id === "welcome" && item.title === "新的投资咨询") {
    item.title = prompt;
  }
  item.status = "running";
  item.time = new Date().toLocaleTimeString();
  return item.id;
}

function abortRunRequest() {
  if (runAbortController) {
    runAbortController.abort();
    runAbortController = null;
  }
}

function cloneBlocks(blocks) {
  return JSON.parse(JSON.stringify(blocks));
}

function blocksTextContent(blocks) {
  return blocks
    .filter((block) => block.type === "text")
    .map((block) => block.content)
    .join("")
    .trim();
}

function appendTextBlock(content) {
  if (!content) return;

  const lastBlock = currentBlocks.value[currentBlocks.value.length - 1];
  if (lastBlock?.type === "text") {
    lastBlock.content += content;
  } else {
    currentBlocks.value.push({
      id: crypto.randomUUID(),
      type: "text",
      content,
    });
  }
  scrollToLatest();
}

function appendKlineBlock(data) {
  if (!data?.option) return;

  const toolCallKey = data.tool_call_id || `${data.code || "unknown"}:${data.latest?.date || data.count || ""}`;
  if (toolCallKey && renderedToolCallIds.has(toolCallKey)) return;
  if (toolCallKey) renderedToolCallIds.add(toolCallKey);

  currentBlocks.value.push({
    id: crypto.randomUUID(),
    type: "kline",
    toolCallId: data.tool_call_id || "",
    option: data.option,
    meta: {
      code: data.code,
      count: data.count,
      latest: data.latest,
    },
  });
  scrollToLatest();
}

function appendKlineErrorBlock(message, data = {}) {
  const toolCallKey = data.tool_call_id ? `error:${data.tool_call_id}` : "";
  if (toolCallKey && renderedToolCallIds.has(toolCallKey)) return;
  if (toolCallKey) renderedToolCallIds.add(toolCallKey);

  currentBlocks.value.push({
    id: crypto.randomUUID(),
    type: "kline_error",
    toolCallId: data.tool_call_id || "",
    message: message || "K 线数据加载失败。",
  });
  scrollToLatest();
}

function resetAgentPanels() {
  for (const definition of agentDefinitions) {
    agents[definition.id].progress = [];
    agents[definition.id].status = "waiting";
    agents[definition.id].updatedAt = "";
  }
  copiedAgentId.value = "";
}

function archiveCurrentTurn() {
  const prompt = currentPrompt.value.trim();
  const blocks = cloneBlocks(currentBlocks.value);
  if (!prompt && !blocks.length) return;

  archivedTurns.value.push({
    id: `turn-${Date.now()}-${archivedTurnCounter}`,
    prompt,
    blocks,
    time: new Date().toLocaleTimeString(),
  });
  archivedTurnCounter += 1;
}

function scrollToLatest() {
  nextTick(() => {
    window.scrollTo({
      top: document.documentElement.scrollHeight,
      behavior: isRunning.value ? "auto" : "smooth",
    });
  });
}

function setAgentStatus(agentId, status) {
  if (!agents[agentId]) return;
  agents[agentId].status = status;
  agents[agentId].updatedAt = new Date().toLocaleTimeString();
  scrollToLatest();
}

function appendAgentContent(agentId, content) {
  if (!agents[agentId] || !content) return;
  agents[agentId].status = "streaming";
  agents[agentId].updatedAt = new Date().toLocaleTimeString();
  appendTextBlock(content);
}

function appendAgentProgress(agentId, message) {
  if (!agents[agentId] || !message) return;
  agents[agentId].progress.push({
    id: crypto.randomUUID(),
    message,
  });
  agents[agentId].status = "streaming";
  agents[agentId].updatedAt = new Date().toLocaleTimeString();
  scrollToLatest();
}

function isLatestProgress(agentId, progressId) {
  const progress = agents[agentId]?.progress || [];
  return progress.length > 0 && progress[progress.length - 1].id === progressId;
}

function finishPendingAgents() {
  for (const definition of agentDefinitions) {
    const panel = agents[definition.id];
    if (panel.status === "waiting" || panel.status === "streaming") {
      panel.status = currentBlocks.value.length || panel.progress.length ? "done" : "idle";
      panel.updatedAt = panel.updatedAt || new Date().toLocaleTimeString();
    }
  }
}

function cancelRun() {
  if (!isRunning.value) return;

  abortRunRequest();
  isRunning.value = false;
  runPhase.value = "cancelled";
  updateActiveConversationStatus("cancelled");
  finishPendingAgents();
}

async function copyTextContent(content, copyKey) {
  const text = content?.trim();
  if (!text) return;

  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = text;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }

    copiedAgentId.value = copyKey;
    window.setTimeout(() => {
      if (copiedAgentId.value === copyKey) {
        copiedAgentId.value = "";
      }
    }, 1600);
  } catch (error) {
    errorMessage.value = "复制失败，请手动选择文本复制。";
  }
}

async function copyAgentReply(agentId) {
  if (!agents[agentId]) return;
  const content = blocksTextContent(currentBlocks.value);
  if (!content) return;

  await copyTextContent(content, agentId);
}

async function runAnalysis() {
  const prompt = userInput.value.trim();
  if (!prompt || isRunning.value) return;

  archiveCurrentTurn();
  abortRunRequest();
  resetAgentPanels();
  currentBlocks.value = [];
  renderedToolCallIds = new Set();
  errorMessage.value = "";
  runPhase.value = "starting";
  isRunning.value = true;
  currentPrompt.value = prompt;
  const conversationId = prepareActiveConversation(prompt);
  userInput.value = "";
  scrollToLatest();

  const controller = new AbortController();
  runAbortController = controller;
  let streamCompleted = false;

  try {
    await fetchEventSource("/api/run/fundamental/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Accept: "text/event-stream",
      },
      body: JSON.stringify({
        command: prompt,
        timeout_seconds: 1800,
        agent_mode: "fundamental",
        conversation_id: conversationId,
      }),
      signal: controller.signal,
      openWhenHidden: true,
      async onopen(response) {
        const contentType = response.headers.get("content-type") || "";
        if (!response.ok || !contentType.includes("text/event-stream")) {
          const body = await response.json().catch(() => ({}));
          throw new Error(body.detail || `Request failed: ${response.status}`);
        }
        runPhase.value = "connected";
      },
      onmessage(event) {
        const data = event.data ? JSON.parse(event.data) : {};

        if (event.event === "status") {
          runPhase.value = data.message || "running";
          return;
        }

        if (event.event === "agent_status") {
          setAgentStatus(data.agent || "fundamental", data.status || "streaming");
          return;
        }

        if (event.event === "agent_progress") {
          appendAgentProgress(data.agent || "fundamental", data.message || "");
          return;
        }

        if (event.event === "token") {
          appendAgentContent(data.agent || "fundamental", data.content || "");
          return;
        }

        if (event.event === "kline") {
          appendKlineBlock(data);
          return;
        }

        if (event.event === "kline_state") {
          return;
        }

        if (event.event === "kline_error") {
          appendKlineErrorBlock(data.message, data);
          return;
        }

        if (event.event === "error") {
          errorMessage.value = data.message || "Agent run failed";
          isRunning.value = false;
          runPhase.value = "failed";
          updateActiveConversationStatus("failed");
          for (const definition of agentDefinitions) {
            if (agents[definition.id].status !== "done") {
              agents[definition.id].status = "error";
            }
          }
          streamCompleted = true;
          throw new Error(errorMessage.value);
        }

        if (event.event === "done") {
          streamCompleted = true;
          isRunning.value = false;
          runPhase.value = data.ok ? "done" : "failed";
          finishPendingAgents();
          updateActiveConversationStatus(data.ok ? "done" : "failed");
          runAbortController = null;
        }
      },
      onclose() {
        if (!streamCompleted && !controller.signal.aborted) {
          isRunning.value = false;
          runPhase.value = "closed";
          updateActiveConversationStatus("closed");
          finishPendingAgents();
        }
        if (runAbortController === controller) {
          runAbortController = null;
        }
      },
      onerror(error) {
        if (controller.signal.aborted) {
          throw error;
        }

        errorMessage.value = error.message || "SSE stream error";
        isRunning.value = false;
        runPhase.value = "failed";
        updateActiveConversationStatus("failed");
        for (const definition of agentDefinitions) {
          if (agents[definition.id].status !== "done") {
            agents[definition.id].status = "error";
          }
        }
        if (runAbortController === controller) {
          runAbortController = null;
        }
        throw error;
      },
    });
  } catch (error) {
    if (controller.signal.aborted) {
      return;
    }
    errorMessage.value = error.message;
    isRunning.value = false;
    runPhase.value = "failed";
    updateActiveConversationStatus("failed");
    finishPendingAgents();
  }
}

onMounted(async () => {
  try {
    await loadHealth();
  } catch (error) {
    runPhase.value = "offline";
  }
});

onBeforeUnmount(() => {
  abortRunRequest();
});
</script>

<template>
  <main class="app-shell bg-base-200 text-base-content" :class="{ 'sidebar-collapsed': sidebarCollapsed }">
    <aside class="app-sidebar">
      <button
        class="btn btn-ghost btn-square sidebar-toggle"
        type="button"
        title="折叠侧边栏"
        @click="sidebarCollapsed = !sidebarCollapsed"
      >
        <PanelLeftOpen v-if="sidebarCollapsed" :size="19" />
        <PanelLeftClose v-else :size="19" />
      </button>

      <div v-if="!sidebarCollapsed" class="sidebar-body">
        <div class="sidebar-brand">
          <Bot :size="20" class="text-primary" />
          <span>Finance Chat</span>
        </div>
        <div class="sidebar-list">
          <button
            v-for="item in conversations"
            :key="item.id"
            class="sidebar-item"
            :class="{ active: item.id === activeConversationId }"
            type="button"
            @click="activeConversationId = item.id"
          >
            <span class="sidebar-title">{{ item.title }}</span>
            <span class="sidebar-meta">{{ item.status }} · {{ item.time }}</span>
          </button>
        </div>
      </div>
    </aside>

    <section class="app-main">
      <div class="chat-content">
        <header class="navbar app-navbar">
          <div class="flex-1 min-w-0">
            <Bot :size="24" class="text-primary shrink-0" />
            <div class="min-w-0">
              <h1 class="truncate text-xl font-bold leading-tight">{{ activeConversation?.title }}</h1>
            </div>
          </div>
          <div class="flex-none gap-2">
            <div class="badge" :class="statusBadgeClass">
              {{ statusBadgeText }}
            </div>
          </div>
        </header>

        <section v-if="errorMessage" class="alert alert-error app-alert">
          <AlertTriangle :size="18" />
          <span>{{ errorMessage }}</span>
        </section>

        <div v-if="archivedTurns.length" class="turn-history">
          <section v-for="turn in archivedTurns" :key="turn.id" class="turn-block">
            <section v-if="turn.prompt" class="user-message-row user-message-row-archived">
              <div class="user-message">
                {{ turn.prompt }}
              </div>
            </section>

            <article v-if="turn.blocks?.length" class="agent-card agent-card-archived agent-fundamental">
              <div class="agent-card-head">
                <div class="flex min-w-0 items-center gap-2">
                  <span class="badge badge-sm badge-success"></span>
                  <h2 class="truncate text-base font-bold">基本面 Agent</h2>
                </div>
              </div>

              <div class="agent-response-card">
                <div class="agent-output">
                  <template v-for="block in turn.blocks" :key="block.id">
                    <div v-if="block.type === 'text'" class="agent-final-frame">
                      <div
                        class="agent-final markdown-body"
                        v-html="renderMarkdown(block.content)"
                      ></div>
                      <button
                        class="btn btn-ghost btn-square btn-sm agent-copy-button"
                        type="button"
                        title="复制回复"
                        aria-label="复制回复"
                        @click="copyTextContent(blocksTextContent(turn.blocks), 'archive-' + turn.id)"
                      >
                        <Check v-if="copiedAgentId === 'archive-' + turn.id" :size="16" />
                        <Copy v-else :size="16" />
                      </button>
                    </div>

                    <KlineChart
                      v-else-if="block.type === 'kline'"
                      :option="block.option"
                      :meta="block.meta"
                    />

                    <div v-else-if="block.type === 'kline_error'" class="kline-error">
                      {{ block.message }}
                    </div>
                  </template>

                  <div class="agent-response-time">{{ turn.time }}</div>
                </div>
              </div>
            </article>
          </section>
        </div>

        <section v-if="currentPrompt" class="user-message-row">
          <div class="user-message">
            {{ currentPrompt }}
          </div>
        </section>

        <section class="agent-grid">
          <article
            v-for="definition in agentDefinitions"
            :key="definition.id"
            class="agent-card"
            :class="definition.accent"
          >
            <div class="agent-card-head">
              <div class="flex min-w-0 items-center gap-2">
                <span class="badge badge-sm" :class="definition.tone"></span>
                <h2 class="truncate text-base font-bold">{{ definition.name }}</h2>
              </div>
              <div class="badge gap-1" :class="statusClass(agents[definition.id].status)">
                <component
                  :is="statusIcon(agents[definition.id].status)"
                  :class="{ spin: agents[definition.id].status === 'streaming' }"
                  :size="13"
                />
                {{ statusLabel(agents[definition.id].status) }}
              </div>
            </div>

            <div
              class="agent-response-card"
              :class="{
                'agent-response-card-empty':
                  !agents[definition.id].progress.length &&
                  !currentBlocks.length,
              }"
            >
              <div class="agent-output">
                <div v-if="agents[definition.id].progress.length" class="agent-progress-list">
                  <div
                    v-for="item in agents[definition.id].progress"
                    :key="item.id"
                    class="agent-progress-line"
                  >
                    <span>{{ item.message }}</span>
                    <LoaderCircle
                      v-if="
                        agents[definition.id].status === 'streaming' &&
                        isLatestProgress(definition.id, item.id)
                      "
                      class="spin"
                      :size="14"
                    />
                  </div>
                </div>

                <template v-for="block in currentBlocks" :key="block.id">
                  <div v-if="block.type === 'text'" class="agent-final-frame">
                    <div
                      class="agent-final markdown-body"
                      v-html="renderMarkdown(block.content)"
                    ></div>
                    <button
                      class="btn btn-ghost btn-square btn-sm agent-copy-button"
                      type="button"
                      title="复制回复"
                      aria-label="复制回复"
                      @click="copyAgentReply(definition.id)"
                    >
                      <Check v-if="copiedAgentId === definition.id" :size="16" />
                      <Copy v-else :size="16" />
                    </button>
                  </div>

                  <KlineChart
                    v-else-if="block.type === 'kline'"
                    :option="block.option"
                    :meta="block.meta"
                  />

                  <div v-else-if="block.type === 'kline_error'" class="kline-error">
                    {{ block.message }}
                  </div>
                </template>

                <div
                  v-if="
                    !agents[definition.id].progress.length &&
                    !currentBlocks.length
                  "
                  class="agent-empty"
                >
                  等待输出
                </div>
                <div class="output-sentinel"></div>
                <div class="agent-response-time">
                  {{ agents[definition.id].updatedAt || "未更新" }}
                </div>
              </div>
            </div>
          </article>
        </section>
      </div>

      <footer class="composer">
        <div class="join composer-box bg-base-100 shadow-lg">
          <textarea
            v-model="userInput"
            class="textarea join-item composer-input"
            :disabled="isRunning"
            spellcheck="false"
            @keydown.enter.exact.prevent="runAnalysis"
          ></textarea>
          <button
            class="btn btn-primary join-item composer-send"
            :class="{ 'btn-error': isRunning }"
            :disabled="!isRunning && !canSend"
            type="button"
            :title="isRunning ? '取消生成' : '发送'"
            @click="isRunning ? cancelRun() : runAnalysis()"
          >
            <Square v-if="isRunning" :size="18" />
            <Send v-else :size="18" />
          </button>
        </div>
      </footer>
    </section>
  </main>
</template>
