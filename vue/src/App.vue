<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import {
  AlertTriangle,
  Bot,
  CandlestickChart,
  Check,
  CircleCheck,
  CircleDot,
  Copy,
  LoaderCircle,
  PanelLeftClose,
  PanelLeftOpen,
  Send,
} from "@lucide/vue";
import * as echarts from "echarts";
import { marked } from "marked";
import DOMPurify from "dompurify";

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
        content: "",
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
const copiedAgentId = ref("");
const klineOption = ref(null);
const klineMeta = ref(null);
const klineError = ref("");
const klineChartEl = ref(null);

let runEventSource = null;
let klineChart = null;
let archivedTurnCounter = 0;

function asElement(value) {
  const candidate = Array.isArray(value) ? value[0] : value;
  if (!candidate) return null;
  return candidate instanceof HTMLElement ? candidate : candidate.$el || null;
}

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

function closeRunEventSource() {
  if (runEventSource) {
    runEventSource.close();
    runEventSource = null;
  }
}

function disposeKlineChart() {
  if (klineChart) {
    klineChart.dispose();
    klineChart = null;
  }
}

function resetAgentPanels() {
  for (const definition of agentDefinitions) {
    agents[definition.id].content = "";
    agents[definition.id].progress = [];
    agents[definition.id].status = "waiting";
    agents[definition.id].updatedAt = "";
  }
  klineOption.value = null;
  klineMeta.value = null;
  klineError.value = "";
  copiedAgentId.value = "";
  disposeKlineChart();
}

function archiveCurrentTurn() {
  const prompt = currentPrompt.value.trim();
  const content = agents.fundamental?.content?.trim() || "";
  if (!prompt && !content) return;

  archivedTurns.value.push({
    id: `turn-${Date.now()}-${archivedTurnCounter}`,
    prompt,
    content,
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
  agents[agentId].content += content;
  agents[agentId].status = "streaming";
  agents[agentId].updatedAt = new Date().toLocaleTimeString();
  scrollToLatest();
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
      panel.status = panel.content || panel.progress.length || klineOption.value ? "done" : "idle";
      panel.updatedAt = panel.updatedAt || new Date().toLocaleTimeString();
    }
  }
}

function renderKlineChart() {
  nextTick(() => {
    const chartEl = asElement(klineChartEl.value);
    if (!chartEl || !klineOption.value) return;
    if (!klineChart) {
      klineChart = echarts.init(chartEl);
    }
    klineChart.setOption(klineOption.value, true);
    klineChart.resize();
    scrollToLatest();
  });
}

function resizeKlineChart() {
  klineChart?.resize();
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
  const content = agents[agentId]?.content?.trim();
  if (!content) return;

  await copyTextContent(content, agentId);
}

async function runAnalysis() {
  const prompt = userInput.value.trim();
  if (!prompt || isRunning.value) return;

  archiveCurrentTurn();
  closeRunEventSource();
  resetAgentPanels();
  errorMessage.value = "";
  runPhase.value = "starting";
  isRunning.value = true;
  currentPrompt.value = prompt;
  const conversationId = prepareActiveConversation(prompt);
  userInput.value = "";
  scrollToLatest();

  try {
    const body = await requestJson("/api/run/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: prompt,
        timeout_seconds: 1800,
        agent_mode: "fundamental",
        conversation_id: conversationId,
      }),
    });

    runPhase.value = "connected";
    runEventSource = new EventSource(`/api/run/stream/${body.run_id}`);

    runEventSource.addEventListener("status", (event) => {
      const data = JSON.parse(event.data);
      runPhase.value = data.message || "running";
    });

    runEventSource.addEventListener("agent_status", (event) => {
      const data = JSON.parse(event.data);
      setAgentStatus(data.agent, data.status);
    });

    runEventSource.addEventListener("agent_delta", (event) => {
      const data = JSON.parse(event.data);
      appendAgentContent(data.agent, data.content || "");
    });

    runEventSource.addEventListener("agent_progress", (event) => {
      const data = JSON.parse(event.data);
      appendAgentProgress(data.agent, data.message || "");
    });

    runEventSource.addEventListener("kline", (event) => {
      const data = JSON.parse(event.data);
      klineOption.value = data.option || null;
      klineMeta.value = {
        code: data.code,
        count: data.count,
        latest: data.latest,
      };
      klineError.value = "";
      renderKlineChart();
    });

    runEventSource.addEventListener("kline_error", (event) => {
      const data = JSON.parse(event.data);
      klineError.value = data.message || "K 线数据加载失败。";
      scrollToLatest();
    });

    runEventSource.addEventListener("done", (event) => {
      const data = JSON.parse(event.data);
      isRunning.value = false;
      runPhase.value = data.ok ? "done" : "failed";
      finishPendingAgents();
      updateActiveConversationStatus(data.ok ? "done" : "failed");
      closeRunEventSource();
    });

    runEventSource.addEventListener("server_error", (event) => {
      const data = JSON.parse(event.data);
      errorMessage.value = data.message || "Agent run failed";
      isRunning.value = false;
      runPhase.value = "failed";
      updateActiveConversationStatus("failed");
      for (const definition of agentDefinitions) {
        if (agents[definition.id].status !== "done") {
          agents[definition.id].status = "error";
        }
      }
      closeRunEventSource();
    });

    runEventSource.addEventListener("error", () => {
      if (!isRunning.value || runEventSource?.readyState === EventSource.CLOSED) {
        return;
      }

      if (runEventSource?.readyState === EventSource.CONNECTING) {
        runPhase.value = "reconnecting";
        return;
      }

      if (runEventSource?.readyState === EventSource.OPEN) {
        return;
      }

      runPhase.value = "connection_check";
    });
  } catch (error) {
    errorMessage.value = error.message;
    isRunning.value = false;
    runPhase.value = "failed";
    updateActiveConversationStatus("failed");
    finishPendingAgents();
  }
}

onMounted(async () => {
  window.addEventListener("resize", resizeKlineChart);
  try {
    await loadHealth();
  } catch (error) {
    runPhase.value = "offline";
  }
});

onBeforeUnmount(() => {
  closeRunEventSource();
  disposeKlineChart();
  window.removeEventListener("resize", resizeKlineChart);
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

            <article v-if="turn.content" class="agent-card agent-card-archived agent-fundamental">
              <div class="agent-card-head">
                <div class="flex min-w-0 items-center gap-2">
                  <span class="badge badge-sm badge-success"></span>
                  <h2 class="truncate text-base font-bold">基本面 Agent</h2>
                </div>
              </div>

              <div class="agent-response-card">
                <div class="agent-output">
                  <div class="agent-final-frame">
                    <div
                      class="agent-final markdown-body"
                      v-html="renderMarkdown(turn.content)"
                    ></div>
                    <button
                      class="btn btn-ghost btn-square btn-sm agent-copy-button"
                      type="button"
                      title="复制回复"
                      aria-label="复制回复"
                      @click="copyTextContent(turn.content, 'archive-' + turn.id)"
                    >
                      <Check v-if="copiedAgentId === 'archive-' + turn.id" :size="16" />
                      <Copy v-else :size="16" />
                    </button>
                  </div>
                </div>
              </div>

              <div class="agent-card-foot">
                <span>{{ turn.time }}</span>
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
                  !agents[definition.id].content &&
                  !klineOption &&
                  !klineError,
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

                <div v-if="agents[definition.id].content" class="agent-final-frame">
                  <div
                    class="agent-final markdown-body"
                    v-html="renderMarkdown(agents[definition.id].content)"
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

                <section v-if="klineOption || klineError" class="kline-section">
                  <div class="kline-head">
                    <div class="flex items-center gap-2">
                      <CandlestickChart :size="18" class="text-primary" />
                      <h3>近一个月 K 线</h3>
                    </div>
                    <span v-if="klineMeta" class="kline-meta">
                      {{ klineMeta.code }} · {{ klineMeta.count }} 条
                    </span>
                  </div>
                  <div v-if="klineOption" ref="klineChartEl" class="kline-chart"></div>
                  <div v-else class="kline-error">{{ klineError }}</div>
                </section>

                <div
                  v-if="
                    !agents[definition.id].progress.length &&
                    !agents[definition.id].content &&
                    !klineOption &&
                    !klineError
                  "
                  class="agent-empty"
                >
                  等待输出
                </div>
                <div class="output-sentinel"></div>
              </div>
            </div>

            <div class="agent-card-foot">
              <span>{{ agents[definition.id].updatedAt || "未更新" }}</span>
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
            :disabled="!canSend"
            type="button"
            @click="runAnalysis"
          >
            <LoaderCircle v-if="isRunning" class="spin" :size="18" />
            <Send v-else :size="18" />
          </button>
        </div>
      </footer>
    </section>
  </main>
</template>
