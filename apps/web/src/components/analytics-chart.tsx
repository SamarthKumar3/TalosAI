"use client";

import dynamic from "next/dynamic";
import type { EChartsOption } from "echarts";
import { useSyncExternalStore } from "react";
import { resolveChartColors } from "@/lib/chart-theme";

const ChartRenderer = dynamic(() => import("./analytics-chart-renderer"), { ssr: false });

function subscribeTheme(listener: () => void) {
  const observer = new MutationObserver(listener);
  observer.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-skin"] });
  return () => observer.disconnect();
}
function themeSnapshot() { return `${document.documentElement.dataset.theme}:${document.documentElement.dataset.skin}`; }
function serverSnapshot() { return ""; }

type AnalyticsChartProps = {
  label: string;
  option: EChartsOption;
  height?: number;
};

export function AnalyticsChart({ label, option, height = 280 }: AnalyticsChartProps) {
  const theme = useSyncExternalStore(subscribeTheme, themeSnapshot, serverSnapshot);
  const colors = theme ? getComputedStyle(document.documentElement) : null;
  const resolved = resolveChartColors(option, (token) => colors?.getPropertyValue(token) ?? "");
  return (
    <div aria-label={label} className="analytics-chart" role="img">
      <ChartRenderer option={{ aria: { enabled: true }, backgroundColor: "transparent", ...resolved }} height={height} />
    </div>
  );
}
