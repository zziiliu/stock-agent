<script setup>
import { nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { CandlestickChart } from "@lucide/vue";
import * as echarts from "echarts";

const props = defineProps({
  option: {
    type: Object,
    required: true,
  },
  meta: {
    type: Object,
    default: null,
  },
});

const chartEl = ref(null);
let chart = null;

function renderChart() {
  nextTick(() => {
    if (!chartEl.value || !props.option) return;

    if (!chart) {
      chart = echarts.init(chartEl.value);
    }

    chart.setOption(props.option, true);
    chart.resize();
  });
}

function resizeChart() {
  chart?.resize();
}

onMounted(() => {
  renderChart();
  window.addEventListener("resize", resizeChart);
});

watch(
  () => props.option,
  () => renderChart(),
  { deep: true }
);

onBeforeUnmount(() => {
  window.removeEventListener("resize", resizeChart);
  if (chart) {
    chart.dispose();
    chart = null;
  }
});
</script>

<template>
  <section class="kline-section">
    <div class="kline-head">
      <div class="flex items-center gap-2">
        <CandlestickChart :size="18" class="text-primary" />
        <h3>近一个月 K 线</h3>
      </div>
      <span v-if="meta" class="kline-meta">
        {{ meta.code }} · {{ meta.count }} 条
      </span>
    </div>
    <div ref="chartEl" class="kline-chart"></div>
  </section>
</template>
