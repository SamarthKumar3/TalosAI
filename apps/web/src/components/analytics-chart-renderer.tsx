"use client";

import ReactEChartsCore from "echarts-for-react/lib/core";
import * as echarts from "echarts/core";
import { LineChart, PieChart } from "echarts/charts";
import { AriaComponent, GridComponent, LegendComponent, TooltipComponent } from "echarts/components";
import { SVGRenderer } from "echarts/renderers";
import type { EChartsOption } from "echarts";

echarts.use([LineChart, PieChart, AriaComponent, GridComponent, LegendComponent, TooltipComponent, SVGRenderer]);

export default function AnalyticsChartRenderer({ option, height }: { option: EChartsOption; height: number }) {
  return <ReactEChartsCore echarts={echarts} lazyUpdate notMerge option={option} opts={{ renderer: "svg" }} style={{ height }} />;
}
