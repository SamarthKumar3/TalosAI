import type { EChartsOption } from "echarts";

// SVG chart attributes cannot resolve CSS var() colors themselves.
export function resolveChartColors(option: EChartsOption, resolve: (token: string) => string): EChartsOption {
  function visit(value: unknown): unknown {
    if (typeof value === "string") {
      const token = /^var\((--[\w-]+)\)$/.exec(value)?.[1];
      return token ? resolve(token).trim() || value : value;
    }
    if (Array.isArray(value)) return value.map(visit);
    if (value && typeof value === "object") {
      return Object.fromEntries(Object.entries(value).map(([key, child]) => [key, visit(child)]));
    }
    return value;
  }
  return visit(option) as EChartsOption;
}
