"use client";

import { MoonIcon, SunIcon } from "@/components/icons";

import type { Theme } from "./use-theme";

export function ThemeToggle({
  theme,
  onToggle,
}: Readonly<{
  theme: Theme;
  onToggle: () => void;
}>) {
  const nextTheme = theme === "light" ? "dark" : "light";
  return (
    <button
      aria-label={`Switch to ${nextTheme} theme`}
      className="icon-button theme-toggle"
      onClick={onToggle}
      title={`Current theme: ${theme}`}
      type="button"
    >
      {theme === "light" ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}
